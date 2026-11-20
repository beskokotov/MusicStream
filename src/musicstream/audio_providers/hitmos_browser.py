"""Read metadata and the normal guest playback redirect; never fetch CDN audio."""

import asyncio
import hashlib
import ipaddress
import logging
import re
import socket
import time
from contextlib import suppress
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from musicstream.audio_providers.base import ResolutionDiagnostics, SourceDocument
from musicstream.audio_providers.hitmos import inspect_player_url, stream_format, valid_hitmos_player_url, valid_stream_url
from musicstream.audio_providers.transport import SourceFailure
from musicstream.audio_providers.hitmos_stream import retry_after_seconds
from musicstream.audio_providers.hitmos_proxy_config import configured_hitmos_proxy
from musicstream.public_rate_limit import ClientRateLimiter

logger = logging.getLogger(__name__)

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
                and parsed.path == f"/song/{song_id}")
    except ValueError:
        return False


def document_kind(url: str, song_id: str, search_url: str | None = None) -> str:
    """Return a non-sensitive route label for local CDP diagnostics."""
    if search_url and url == search_url:
        return "search"
    try:
        path = urlsplit(url).path
    except ValueError:
        return "other"
    if path == f"/song/{song_id}":
        return "song"
    if path == "/robots.txt":
        return "robots"
    return "other"


def request_action(url, resource_type, method, frame_id, main_frame_id, song_id, player_url, started,
                  search_url=None, network_frame_id=None):
    """Decision for every CDP request, including requests following redirects."""
    # Fetch.requestPaused.frameId can be absent in some Chromium/CDP event
    # sequences. Only use the matching Network event as a fallback; never infer
    # that an unknown frame is the main frame from its URL or resource type.
    initiating_frame_id = frame_id if frame_id is not None else network_frame_id
    if method != "GET" or initiating_frame_id != main_frame_id:
        return "block"
    if resource_type == "Document" and allowed_document(url, song_id, search_url):
        return "document"
    if resource_type != "Media":
        return "block"
    if player_url and not started and url == player_url and valid_hitmos_player_url(url, song_id):
        return "player"
    return "block"


def redirect_target(status: int, headers: list[dict], source_url: str, song_id: str) -> str | None:
    if status not in {301, 302, 303, 307, 308} or not valid_hitmos_player_url(source_url, song_id):
        return None
    locations = [header["value"] for header in headers if header["name"].lower() == "location"]
    if len(locations) != 1:
        return None
    try:
        target = urljoin(source_url, locations[0])
        if valid_stream_url(target, song_id):
            return target
    except ValueError:
        pass
    return None


def _host_resolves_publicly(host: str) -> bool:
    """Reject an allowlisted media hostname if DNS points at a non-public IP."""
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
        return bool(addresses) and all(ipaddress.ip_address(address).is_global for address in addresses)
    except (OSError, ValueError):
        return False


class HitmosBrowserReader:
    MAX_BYTES = 262144
    DEFAULT_REQUEST_LIMIT = 30

    def __init__(self, *, diagnostics_enabled=False, lab_mode=False, request_limit=DEFAULT_REQUEST_LIMIT, proxy_url=None):
        self._gate = asyncio.Semaphore(1)
        self._budget = ClientRateLimiter(limit=request_limit)
        self.diagnostics_enabled = diagnostics_enabled
        self.lab_mode = bool(lab_mode)
        self.proxy_url = configured_hitmos_proxy() if proxy_url is None else proxy_url
        self._runtime = None
        self._browser = None

    async def close(self):
        async with self._gate:
            try:
                if self._browser is not None:
                    await self._browser.close()
            finally:
                self._browser = None
                if self._runtime is not None:
                    await self._runtime.stop()
                    self._runtime = None

    async def _get_browser(self, async_playwright):
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        if self._runtime is not None:
            await self._runtime.stop()
        self._runtime = await async_playwright().start()
        try:
            options = {"channel": "chrome", "headless": True, "timeout": 5000}
            if self.proxy_url:
                options["proxy"] = {"server": self.proxy_url}
            self._browser = await self._runtime.chromium.launch(**options)
        except BaseException:
            await self._runtime.stop()
            self._runtime = None
            raise
        return self._browser

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
        diagnostics.network_route = "local_http_proxy" if self.proxy_url else "direct"

        def result(html, stream_url=None, reason=None):
            diagnostics.elapsed_ms = round((time.monotonic() - started_at) * 1000)
            return SourceDocument(html, stream_url, reason, diagnostics)

        if not search_url and not re.fullmatch(r"[0-9]{1,12}", song_id):
            raise SourceFailure("invalid_song_id")
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright
        except ImportError as exc:
            raise SourceFailure("browser_not_installed", stage="source_metadata", diagnostics=diagnostics) from exc
        navigation_failure = [None]
        oversized = [False]
        async with self._gate:
            try:
                retry_after = self._budget.check("browser")
                if retry_after is not None:
                    raise SourceFailure("request_budget_exhausted", owner="local_policy",
                                        stage="source_search" if search_url else "source_metadata",
                                        retry_after_seconds=retry_after)
                browser_was_connected = self._browser is not None and self._browser.is_connected()
                browser_started_at = time.monotonic()
                browser = await self._get_browser(async_playwright)
                if not browser_was_connected:
                    diagnostics.browser_launch_elapsed_ms = round((time.monotonic() - browser_started_at) * 1000)
                context = None
                try:
                    create_started = time.monotonic()
                    context = await browser.new_context(java_script_enabled=False, accept_downloads=False,
                                                        user_agent="MusicStream/0.1.0")
                    diagnostics.browser_context_create_elapsed_ms = round((time.monotonic() - create_started) * 1000)
                    create_started = time.monotonic()
                    page = await context.new_page()
                    diagnostics.browser_page_create_elapsed_ms = round((time.monotonic() - create_started) * 1000)
                    cdp = await context.new_cdp_session(page)
                    await cdp.send("Network.enable")
                    await cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})
                    main_frame = (await cdp.send("Page.getFrameTree"))["frameTree"]["frame"]["id"]
                    resolution = asyncio.get_running_loop().create_future()
                    state = {"player_url": None, "started": False, "player_request_id": None, "reason": None}
                    state["player_started_at"] = None
                    received = [0]
                    documents = set()
                    network_document_frames = {}
                    request_count = [0]

                    def track_request(event):
                        request_count[0] += 1
                        diagnostics.browser_request_count = request_count[0]
                        if event.get("type") == "Document":
                            documents.add(event["requestId"])
                            diagnostics.browser_navigation_count = (diagnostics.browser_navigation_count or 0) + 1
                            frame_id = event.get("frameId")
                            if isinstance(frame_id, str):
                                network_document_frames[event["requestId"]] = frame_id

                    def track_failure(event):
                        if self.diagnostics_enabled and event.get("requestId") in documents:
                            logger.warning("Hitmos document load failed: error=%s blocked=%s canceled=%s",
                                           event.get("errorText"), event.get("blockedReason"), event.get("canceled"))
                        if event.get("requestId") in documents:
                            navigation_failure[0] = event.get("errorText")

                    def count_bytes(event):
                        if event["requestId"] not in documents:
                            return
                        received[0] += event.get("dataLength", 0)
                        if received[0] > self.MAX_BYTES and not oversized[0]:
                            oversized[0] = True
                            if self.diagnostics_enabled:
                                logger.warning("Hitmos document exceeded size limit (%s bytes)", received[0])
                            asyncio.create_task(page.close())

                    async def paused(event):
                        request = event["request"]
                        try:
                            if "responseStatusCode" in event or "responseErrorReason" in event:
                                if self.diagnostics_enabled and event["resourceType"] == "Document":
                                    logger.warning("Hitmos document response: status=%s error=%s", event.get("responseStatusCode"), event.get("responseErrorReason"))
                                if event["resourceType"] == "Document" and allowed_document(request["url"], song_id, search_url):
                                    diagnostics.metadata_status = event.get("responseStatusCode")
                                    if self.diagnostics_enabled:
                                        logger.info("Hitmos document response status %s", diagnostics.metadata_status)
                                # Inspect headers before receiving any response body or
                                # following a redirect. Even a first-hop audio response
                                # is aborted: this service resolves URLs, not audio bytes.
                                if event["requestId"] == state["player_request_id"]:
                                    if state["player_started_at"] is not None:
                                        diagnostics.player_redirect_elapsed_ms = round(
                                            (time.monotonic() - state["player_started_at"]) * 1000)
                                    diagnostics.redirect_status = event.get("responseStatusCode")
                                    locations = [h["value"] for h in event.get("responseHeaders", []) if h["name"].lower() == "location"]
                                    if len(locations) == 1:
                                        try:
                                            target_url = urljoin(request["url"], locations[0])
                                            host = urlsplit(target_url).hostname
                                            diagnostics.redirect_host = ("host_" + hashlib.sha256(host.lower().encode()).hexdigest()[:12]
                                                if self.lab_mode and host else host if host and re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*", host) else "unapproved")
                                            diagnostics.redirect_format = stream_format(target_url)
                                            redirect_path = urlsplit(target_url).path
                                            route_match = re.match(r"^/(s[1-9]/)?get/(cuts|music)/", redirect_path)
                                            diagnostics.redirect_route = ((route_match[1][:-1] + "_" if route_match[1] else "")
                                                                          + "get_" + route_match[2] if route_match else "other")
                                            diagnostics.redirect_song_id_present = bool(
                                                re.search(rf"(?:^|/){re.escape(song_id)}(?:/|$)|_{re.escape(song_id)}(?:\.[^/]*)?$",
                                                          redirect_path))
                                            filename = redirect_path.rsplit("/", 1)[-1]
                                            diagnostics.redirect_extension = (
                                                "mp3" if filename.lower().endswith(".mp3") else
                                                "other" if "." in filename else "missing")
                                        except ValueError:
                                            diagnostics.redirect_host = "unapproved"
                                    status = event.get("responseStatusCode")
                                    if status == 429:
                                        retry_value = next((h["value"] for h in event.get("responseHeaders", [])
                                                            if h["name"].lower() == "retry-after"), None)
                                        state["reason"] = "upstream_rate_limited"
                                        state["failure"] = {"owner": "external_server", "status": 429,
                                                             "retry_after": retry_after_seconds(retry_value)}
                                        diagnostics.stage = "stream_resolution"
                                        diagnostics.rejected_by = "external_server"
                                        diagnostics.http_status = 429
                                        diagnostics.retry_after_seconds = state["failure"]["retry_after"]
                                        diagnostics.internal_code = "upstream_rate_limited"
                                    elif status and status >= 400:
                                        state["reason"] = f"hitmos_http_{status}"
                                        diagnostics.stage = "stream_resolution"
                                        diagnostics.rejected_by = "external_server"
                                        diagnostics.http_status = status
                                        diagnostics.internal_code = f"hitmos_http_{status}"
                                    elif status and status not in {301, 302, 303, 307, 308}:
                                        state["reason"] = "stream_response_unexpected"
                                        diagnostics.stage = "stream_resolution"
                                        diagnostics.rejected_by = "external_server"
                                        diagnostics.http_status = status
                                        diagnostics.internal_code = "stream_response_unexpected"
                                    target = redirect_target(status or 0, event.get("responseHeaders", []),
                                                             request["url"], song_id)
                                    if not target and diagnostics.redirect_host != "unapproved":
                                        logger.warning("Hitmos redirect rejected: host=%s route=%s status=%s",
                                                       diagnostics.redirect_host, diagnostics.redirect_route,
                                                       diagnostics.redirect_status)
                                    if target:
                                        host = urlsplit(target).hostname
                                        try:
                                            public_host = await asyncio.wait_for(
                                                asyncio.to_thread(_host_resolves_publicly, host), timeout=2)
                                        except TimeoutError:
                                            public_host = False
                                        if not public_host:
                                            target = None
                                            state["reason"] = "stream_host_not_public"
                                    diagnostics.redirect_valid = bool(target)
                                    if not target and not state["reason"]:
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
                            network_id = event.get("networkId")
                            network_frame_id = network_document_frames.get(network_id)
                            frame_id = event.get("frameId")
                            action = request_action(request["url"], event["resourceType"], request["method"],
                                                    frame_id, main_frame, song_id,
                                                    state["player_url"], state["started"], search_url,
                                                    network_frame_id=network_frame_id)
                            if self.diagnostics_enabled and event["resourceType"] == "Document":
                                frame_relation = ("main" if frame_id == main_frame else
                                                  "other" if frame_id is not None else "missing")
                                network_relation = ("main" if network_frame_id == main_frame else
                                                    "other" if network_frame_id is not None else "missing")
                                logger.warning("Hitmos document request: kind=%s action=%s fetch_frame=%s network_frame=%s",
                                               document_kind(request["url"], song_id, search_url), action,
                                               frame_relation, network_relation)
                            if action in {"document", "player"}:
                                if action == "player":
                                    state["started"] = True
                                    state["player_request_id"] = event["requestId"]
                                    state["player_started_at"] = time.monotonic()
                                # Keep the browser's normal headers and guest session.
                                await cdp.send("Fetch.continueRequest", {"requestId": event["requestId"], "interceptResponse": True})
                            else:
                                if self.diagnostics_enabled and event["resourceType"] == "Document":
                                    reason = ("main_frame_mismatch" if frame_id is not None and frame_id != main_frame
                                              else "main_frame_unconfirmed" if frame_id is None and network_frame_id != main_frame
                                              else "document_outside_allowlist")
                                    logger.warning("Hitmos document request rejected (%s)", reason)
                                if event["resourceType"] == "Media" and state["started"] and not resolution.done():
                                    state["reason"] = "stream_redirect_not_allowed"
                                    resolution.set_result(None)
                                await cdp.send("Fetch.failRequest", {"requestId": event["requestId"], "errorReason": "Aborted"})
                        except Exception as exc:
                            if self.diagnostics_enabled:
                                logger.warning("Hitmos CDP request handling failed (%s)", type(exc).__name__)
                            if not resolution.done():
                                resolution.set_result(None)

                    cdp.on("Network.requestWillBeSent", track_request)
                    cdp.on("Network.loadingFailed", track_failure)
                    cdp.on("Network.dataReceived", count_bytes)
                    cdp.on("Fetch.requestPaused", paused)
                    await cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})

                    async def read(path):
                        received[0] = 0
                        navigation_started = time.monotonic()
                        response = await page.goto("https://ru.hitmox.org" + path,
                                                   wait_until="domcontentloaded", timeout=7000)
                        diagnostics.domcontentloaded_elapsed_ms = round((time.monotonic() - navigation_started) * 1000)
                        diagnostics.metadata_status = response.status if response else None
                        if not response or response.status != 200 or response.url != "https://ru.hitmox.org" + path:
                            status = response.status if response else diagnostics.metadata_status
                            retry_after = getattr(response, "headers", {}).get("retry-after") if response else None
                            code = ("upstream_rate_limited" if status == 429 else
                                    "automated_access_denied" if status == 403 else
                                    "hitmos_http_error" if status else "automated_access_denied")
                            raise SourceFailure(code,
                                owner="external_server" if status else "network", http_status=status,
                                retry_after_seconds=retry_after_seconds(retry_after),
                                stage="source_search" if search_url else "source_metadata")
                        body_started = time.monotonic()
                        body = await response.body()
                        diagnostics.document_body_elapsed_ms = round((time.monotonic() - body_started) * 1000)
                        if oversized[0] or len(body) > self.MAX_BYTES:
                            raise SourceFailure("response_too_large")
                        return body.decode("utf-8", errors="strict")

                    target_url = search_url or f"https://ru.hitmox.org/song/{song_id}"
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
                    if context is not None:
                        await context.close()
            except SourceFailure as exc:
                diagnostics.elapsed_ms = round((time.monotonic() - started_at) * 1000)
                exc.diagnostics = diagnostics
                exc.stage = "source_search" if search_url else exc.stage or "source_metadata"
                if diagnostics.internal_code is None:
                    diagnostics.stage = exc.stage
                    diagnostics.internal_code = exc.reason
                    diagnostics.rejected_by = exc.owner
                    diagnostics.http_status = exc.http_status
                    diagnostics.retry_after_seconds = exc.retry_after_seconds
                raise
            except Exception as exc:
                diagnostics.elapsed_ms = round((time.monotonic() - started_at) * 1000)
                if self.diagnostics_enabled:
                    # Keep enough detail for local fault isolation while removing
                    # URLs, credential-like fields and IP addresses from logs.
                    safe_error = re.sub(r"https?://[^\s'\"<>]+", "[URL]", str(exc))
                    safe_error = re.sub(
                        r"(?i)\b(api[_-]?key|token|secret|password|authorization|cookie)\b(?:\s*[:=]\s*|\s+)[^\s]+",
                        r"\1=[REDACTED]", safe_error)
                    safe_error = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[IP]", safe_error)
                    logger.warning("Hitmos browser operation failed (%s): %s",
                                   type(exc).__name__, " ".join(safe_error.split())[:500])
                stage = "source_search" if search_url else "stream_resolution" if diagnostics.metadata_status == 200 else "source_metadata"
                reason = ("response_too_large" if oversized[0]
                          else "source_http_error" if diagnostics.metadata_status not in (None, 200)
                          else "source_timeout" if isinstance(exc, (TimeoutError, PlaywrightTimeoutError))
                          else "source_network_error" if navigation_failure[0] or "ERR_ABORTED" in str(exc)
                          else "browser_unavailable")
                diagnostics.stage = stage
                diagnostics.internal_code = reason
                diagnostics.rejected_by = "external_server" if diagnostics.metadata_status not in (None, 200) else "network"
                raise SourceFailure(reason, stage=stage, diagnostics=diagnostics,
                    owner=diagnostics.rejected_by, http_status=diagnostics.metadata_status) from exc
