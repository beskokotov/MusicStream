"""Read metadata and the normal guest playback redirect; never fetch CDN audio."""

import asyncio
import re
import time
from contextlib import suppress
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from musicstream.audio_providers.base import ResolutionDiagnostics, SourceDocument
from musicstream.audio_providers.hitmos import CDN_HOSTS, STREAM_PREFIXES, inspect_player_url, stream_format, valid_stream_url
from musicstream.audio_providers.transport import SourceFailure
from musicstream.public_rate_limit import ClientRateLimiter


def allowed_search_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        query = params.get("q", [])
        return (parsed.scheme == "https" and parsed.hostname == "ru.hitmox.org"
                and parsed.port in (None, 443) and not parsed.username and not parsed.password
                and not parsed.fragment and parsed.path == "/search" and set(params) == {"q"}
                and len(query) == 1 and 0 < len(query[0].strip()) <= 401
                and not any(ord(char) < 32 for char in query[0]))
    except ValueError:
        return False


def allowed_document(url: str, song_id: str, search_url: str | None = None) -> bool:
    if search_url and url == search_url and allowed_search_url(search_url):
        return True
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname == "ru.hitmox.org"
                and parsed.port in (None, 443) and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment
                and parsed.path in {"/robots.txt", f"/song/{song_id}"})
    except ValueError:
        return False


def robots_allow_song(text: str, url: str) -> bool:
    if not any(line.lower().strip().startswith("user-agent:") for line in text.splitlines()):
        return False
    policy = RobotFileParser()
    policy.parse(text.splitlines())
    if not policy.can_fetch("MusicStream", url):
        return False
    parsed = urlsplit(url)
    path = parsed.path + ("?" + parsed.query if parsed.query else "")
    for line in text.splitlines():
        field, _, value = line.partition(":")
        value = value.split("#", 1)[0].strip()
        if field.strip().lower() == "disallow" and ("*" in value or "$" in value):
            anchored = value.endswith("$")
            pattern = re.escape(value.removesuffix("$")).replace(r"\*", ".*")
            if re.search("^" + pattern + ("$" if anchored else ""), path):
                return False
    return True


def request_action(url, resource_type, method, frame_id, main_frame_id, song_id, player_url, started, search_url=None):
    """Decision for every CDP request, including requests following redirects."""
    if method != "GET" or frame_id != main_frame_id:
        return "block"
    if resource_type == "Document" and allowed_document(url, song_id, search_url):
        return "document"
    if resource_type != "Media":
        return "block"
    if player_url and not started and url == player_url and valid_stream_url(url, song_id):
        if urlsplit(url).hostname == "ru.hitmox.org":
            return "player"
    return "block"


def redirect_target(status: int, headers: list[dict], source_url: str, song_id: str) -> str | None:
    if status not in {301, 302, 303, 307, 308}:
        return None
    locations = [header["value"] for header in headers if header["name"].lower() == "location"]
    if len(locations) != 1:
        return None
    try:
        target = urljoin(source_url, locations[0])
        if valid_stream_url(target, song_id) and urlsplit(target).hostname in CDN_HOSTS:
            return target
    except ValueError:
        pass
    return None


class HitmosBrowserReader:
    MAX_BYTES = 262144

    def __init__(self):
        self._gate = asyncio.Semaphore(1)
        self._budget = ClientRateLimiter(limit=5)

    async def read_song(self, song_id: str, *, metadata_only=False) -> SourceDocument:
        return await self._read(song_id, metadata_only=metadata_only)

    async def read_search(self, query: str) -> SourceDocument:
        search_url = "https://ru.hitmox.org/search?" + urlencode({"q": query})
        if not allowed_search_url(search_url):
            raise SourceFailure("invalid_search_query", stage="source_search")
        return await self._read("", search_url=search_url, metadata_only=True)

    async def _read(self, song_id: str, *, search_url=None, metadata_only=False) -> SourceDocument:
        started_at = time.monotonic()
        diagnostics = ResolutionDiagnostics()

        def result(html, stream_url=None, reason=None):
            diagnostics.elapsed_ms = round((time.monotonic() - started_at) * 1000)
            return SourceDocument(html, stream_url, reason, diagnostics)

        if not search_url and not re.fullmatch(r"[0-9]{1,12}", song_id):
            raise SourceFailure("invalid_song_id")
        if self._budget.check("browser") is not None:
            raise SourceFailure("request_budget_exhausted", stage="source_metadata", diagnostics=diagnostics)
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise SourceFailure("browser_not_installed", stage="source_metadata", diagnostics=diagnostics) from exc
        async with self._gate:
            try:
                async with async_playwright() as runtime:
                    browser = await runtime.chromium.launch(channel="chrome", headless=True, timeout=5000)
                    try:
                        context = await browser.new_context(java_script_enabled=False, accept_downloads=False,
                                                            user_agent="MusicStream/0.1.0")
                        page = await context.new_page()
                        cdp = await context.new_cdp_session(page)
                        await cdp.send("Network.enable")
                        await cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})
                        main_frame = (await cdp.send("Page.getFrameTree"))["frameTree"]["frame"]["id"]
                        resolution = asyncio.get_running_loop().create_future()
                        state = {"player_url": None, "started": False, "player_request_id": None, "reason": None}
                        received = [0]
                        oversized = [False]
                        documents = set()

                        def track_request(event):
                            if event.get("type") == "Document":
                                documents.add(event["requestId"])

                        def count_bytes(event):
                            if event["requestId"] not in documents:
                                return
                            received[0] += event.get("dataLength", 0)
                            if received[0] > self.MAX_BYTES and not oversized[0]:
                                oversized[0] = True
                                asyncio.create_task(page.close())

                        async def paused(event):
                            request = event["request"]
                            try:
                                if "responseStatusCode" in event or "responseErrorReason" in event:
                                    if event["resourceType"] == "Document" and allowed_document(request["url"], song_id, search_url):
                                        if urlsplit(request["url"]).path == "/robots.txt":
                                            diagnostics.robots_status = event.get("responseStatusCode")
                                        else:
                                            diagnostics.metadata_status = event.get("responseStatusCode")
                                    # Inspect headers before receiving any response body or
                                    # following a redirect. Even a first-hop audio response
                                    # is aborted: this service resolves URLs, not audio bytes.
                                    if event["requestId"] == state["player_request_id"]:
                                        diagnostics.redirect_status = event.get("responseStatusCode")
                                        locations = [h["value"] for h in event.get("responseHeaders", []) if h["name"].lower() == "location"]
                                        if len(locations) == 1:
                                            try:
                                                target_url = urljoin(request["url"], locations[0])
                                                host = urlsplit(target_url).hostname
                                                diagnostics.redirect_host = host if host in STREAM_PREFIXES else "unapproved"
                                                diagnostics.redirect_format = stream_format(target_url)
                                            except ValueError:
                                                diagnostics.redirect_host = "unapproved"
                                        target = redirect_target(event.get("responseStatusCode", 0),
                                                                 event.get("responseHeaders", []), request["url"], song_id)
                                        diagnostics.redirect_valid = bool(target)
                                        if not target:
                                            status = event.get("responseStatusCode", 0)
                                            state["reason"] = "stream_redirect_not_allowed" if status in {301,302,303,307,308} else "stream_response_unexpected"
                                        if not resolution.done():
                                            resolution.set_result(target)
                                    elif (event["resourceType"] == "Document" and event.get("responseStatusCode") == 200
                                          and allowed_document(request["url"], song_id, search_url)):
                                        await cdp.send("Fetch.continueResponse", {"requestId": event["requestId"]})
                                        return
                                    await cdp.send("Fetch.failRequest", {"requestId": event["requestId"], "errorReason": "Aborted"})
                                    return
                                action = request_action(request["url"], event["resourceType"], request["method"],
                                                        event.get("frameId"), main_frame, song_id,
                                                        state["player_url"], state["started"], search_url)
                                if action in {"document", "player"}:
                                    if action == "player":
                                        state["started"] = True
                                        state["player_request_id"] = event["requestId"]
                                    # Keep the browser's normal headers and guest session.
                                    await cdp.send("Fetch.continueRequest", {"requestId": event["requestId"], "interceptResponse": True})
                                else:
                                    if event["resourceType"] == "Media" and state["started"] and not resolution.done():
                                        state["reason"] = "stream_redirect_not_allowed"
                                        resolution.set_result(None)
                                    await cdp.send("Fetch.failRequest", {"requestId": event["requestId"], "errorReason": "Aborted"})
                            except Exception:
                                if not resolution.done():
                                    resolution.set_result(None)

                        cdp.on("Network.requestWillBeSent", track_request)
                        cdp.on("Network.dataReceived", count_bytes)
                        cdp.on("Fetch.requestPaused", paused)
                        await cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})

                        async def read(path):
                            received[0] = 0
                            response = await page.goto("https://ru.hitmox.org" + path,
                                                       wait_until="domcontentloaded", timeout=7000)
                            if path == "/robots.txt":
                                diagnostics.robots_status = response.status if response else None
                            else:
                                diagnostics.metadata_status = response.status if response else None
                            if not response or response.status != 200 or response.url != "https://ru.hitmox.org" + path:
                                raise SourceFailure("automated_access_denied")
                            body = await response.body()
                            if oversized[0] or len(body) > self.MAX_BYTES:
                                raise SourceFailure("response_too_large")
                            return body.decode("utf-8", errors="strict")

                        robots = await read("/robots.txt")
                        target_url = search_url or f"https://ru.hitmox.org/song/{song_id}"
                        if not robots_allow_song(robots, target_url):
                            raise SourceFailure("robots_disallowed")
                        target = urlsplit(target_url)
                        html = await read(target.path + ("?" + target.query if target.query else ""))
                        if metadata_only:
                            return result(html)
                        player_url, inspection = inspect_player_url(html, song_id)
                        diagnostics.player_format = inspection.player_format
                        diagnostics.player_relative = inspection.player_relative
                        diagnostics.player_valid = inspection.player_valid
                        if not player_url:
                            return result(html, reason="stream_not_found")
                        if urlsplit(player_url).hostname in CDN_HOSTS:
                            return result(html, player_url)
                        state["player_url"] = player_url
                        # A normal metadata request gets the source-issued redirect.
                        # The redirect response is aborted before a CDN request.
                        await page.evaluate("""url => {
                            const audio = document.createElement('audio');
                            audio.preload = 'metadata'; audio.src = url;
                            document.body.append(audio); audio.load();
                        }""", player_url)
                        try:
                            stream_url = await asyncio.wait_for(asyncio.shield(resolution), timeout=3)
                        except TimeoutError:
                            stream_url = None
                            state["reason"] = "stream_redirect_unavailable"
                        with suppress(Exception):
                            await page.evaluate("document.querySelectorAll('audio').forEach(a => {a.pause();a.removeAttribute('src');a.load();})")
                        return result(html, stream_url, state["reason"])
                    finally:
                        await browser.close()
            except SourceFailure as exc:
                diagnostics.elapsed_ms = round((time.monotonic() - started_at) * 1000)
                exc.diagnostics = diagnostics
                exc.stage = "source_search" if search_url else exc.stage or "source_metadata"
                raise
            except Exception as exc:
                diagnostics.elapsed_ms = round((time.monotonic() - started_at) * 1000)
                stage = "source_search" if search_url else "stream_resolution" if diagnostics.metadata_status == 200 else "source_metadata"
                reason = "robots_unavailable" if diagnostics.robots_status not in (None, 200) else "source_http_error" if diagnostics.metadata_status not in (None, 200) else "browser_unavailable"
                raise SourceFailure(reason, stage=stage, diagnostics=diagnostics) from exc
