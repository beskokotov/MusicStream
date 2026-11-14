"""Isolated, metadata-only Hitmoz / Hitmos.fm research adapters.

This prototype never fetches media bytes and never returns a media URL. It is
not wired into MusicStream Auto Play. A media field is only reported as
present/absent with its hostname and a coarse type, pending access-rights and
host-allowlist review.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import time
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import quote_plus, urljoin, urlsplit, urlunsplit

import httpcore
import httpx

try:
    from selectolax.lexbor import LexborHTMLParser
except ImportError:  # Keep the main MusicStream install independent of this experiment.
    LexborHTMLParser = None


HOSTS = {"hitmoz": "ru.hitmoz.org", "hitmos_fm": "rus.hitmos.fm"}
MAX_HTML_BYTES = 512 * 1024
MAX_REDIRECTS = 3
REQUEST_INTERVAL = 1.0
_ID = re.compile(r"[0-9]{1,14}\Z")
_DURATION = re.compile(r"(?:(\d{1,3}):)?(\d{1,2}):(\d{2})\Z")
_ISO_DURATION = re.compile(r"P(?:(\d{1,3})D)?(?:T(?:(\d{1,3})H)?(?:(\d{1,3})M)?(?:(\d{1,3})S)?)?\Z", re.I)


class AdapterFailure(Exception):
    """Safe, non-sensitive provider failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class MediaObservation:
    present: bool
    kind: str | None
    host: str | None
    allowed_host: bool = False


@dataclass(frozen=True)
class Candidate:
    source: str
    id: str
    artist: str | None
    title: str | None
    duration_seconds: float | None
    match: str
    match_reasons: tuple[str, ...]
    page_path: str | None
    media: MediaObservation


@dataclass(frozen=True)
class FetchResult:
    status: int
    url: str
    content_type: str
    body: str
    redirect_count: int


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    """httpcore backend that connects only to the DNS addresses checked here."""

    def __init__(self, addresses: dict[str, tuple[str, ...]]):
        self.addresses = addresses
        self.backend = httpcore.AnyIOBackend()
        self.indices: dict[str, int] = {}

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        name = host.decode("ascii") if isinstance(host, bytes) else str(host)
        ips = self.addresses.get(name.casefold())
        if not ips or port != 443:
            raise OSError("Destination was not DNS checked")
        index = self.indices.get(name, 0)
        self.indices[name] = index + 1
        return await self.backend.connect_tcp(ips[index % len(ips)], port, timeout, local_address, socket_options)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise OSError("Unix sockets are not allowed")

    async def sleep(self, seconds):
        await self.backend.sleep(seconds)


class _PinnedTransport(httpx.AsyncHTTPTransport):
    """One-host transport; each fetch gets a fresh DNS-pinned connection pool."""

    def __init__(self, host: str, addresses: tuple[str, ...]):
        super().__init__(verify=True, http1=True, http2=False, retries=0)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=True),
            max_connections=2,
            max_keepalive_connections=0,
            network_backend=_PinnedBackend({host: addresses}),
        )


class SafeSiteClient:
    """Low-rate, same-origin HTTPS page fetcher with pinned public DNS.

    Redirects are deliberately not delegated to HTTPX: each Location is checked
    against the site's fixed host allowlist and independently DNS-resolved.
    """

    def __init__(self, site: str, *, timeout: float = 5.0, clock=time.monotonic):
        if site not in HOSTS:
            raise ValueError("Unknown adapter site")
        if not 1 <= timeout <= 10:
            raise ValueError("Invalid timeout")
        self.site = site
        self.host = HOSTS[site]
        self.timeout = timeout
        self.clock = clock
        self._last_request = 0.0
        self._gate = asyncio.Lock()

    async def _resolve_public(self, host: str) -> tuple[str, ...]:
        if host.casefold() != self.host:
            raise AdapterFailure("host_not_allowed")
        loop = asyncio.get_running_loop()
        try:
            answers = await asyncio.wait_for(loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM), self.timeout)
            return self._checked_ips(item[4][0] for item in answers)
        except AdapterFailure:
            raise
        except (OSError, ValueError, TimeoutError) as exc:
            raise AdapterFailure("dns_failed") from exc

    @staticmethod
    def _checked_ips(values) -> tuple[str, ...]:
        try:
            ips = tuple(dict.fromkeys(str(value) for value in values))
            if not ips or len(ips) > 16 or any(not ipaddress.ip_address(ip).is_global for ip in ips):
                raise AdapterFailure("dns_address_not_public")
            return ips
        except ValueError as exc:
            raise AdapterFailure("dns_address_invalid") from exc

    def _check_url(self, url: str) -> str:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise AdapterFailure("invalid_url") from exc
        if (parsed.scheme != "https" or parsed.hostname != self.host or port not in (None, 443)
                or parsed.username or parsed.password or parsed.fragment):
            raise AdapterFailure("destination_not_allowed")
        return parsed.hostname

    async def fetch(self, url: str, *, accepted_types: tuple[str, ...] = ("text/html", "application/xhtml+xml")) -> FetchResult:
        current = url
        redirects = 0
        while True:
            host = self._check_url(current)
            addresses = await self._resolve_public(host)
            async with self._gate:
                delay = REQUEST_INTERVAL - (self.clock() - self._last_request)
                if delay > 0:
                    await asyncio.sleep(delay)
                self._last_request = self.clock()
                transport = _PinnedTransport(host, addresses)
                try:
                    async with httpx.AsyncClient(transport=transport, timeout=self.timeout,
                                                 follow_redirects=False, trust_env=False) as client:
                        async with client.stream("GET", current, headers={
                            "User-Agent": "MusicStream-research/0.1",
                            "Accept": "text/html,application/xhtml+xml",
                        }) as response:
                            if response.is_redirect:
                                if redirects >= MAX_REDIRECTS or not response.headers.get("location"):
                                    raise AdapterFailure("redirect_limit")
                                current = urljoin(current, response.headers["location"])
                                redirects += 1
                                continue
                            if response.status_code != 200:
                                raise AdapterFailure(f"http_{response.status_code}")
                            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                            if content_type not in accepted_types:
                                raise AdapterFailure("not_html")
                            body = bytearray()
                            async for chunk in response.aiter_bytes():
                                body.extend(chunk)
                                if len(body) > MAX_HTML_BYTES:
                                    raise AdapterFailure("response_too_large")
                            parsed = urlsplit(current)
                            safe_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
                            return FetchResult(200, safe_url, content_type, body.decode(response.encoding or "utf-8", "replace"), redirects)
                except httpx.TimeoutException as exc:
                    raise AdapterFailure("timeout") from exc
                except httpx.HTTPError as exc:
                    raise AdapterFailure("network_error") from exc
                finally:
                    await transport.aclose()

def _duration(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 86400:
        return float(value)
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if raw.isdigit():
        number = int(raw)
        return float(number) if number <= 86400 else None
    iso = _ISO_DURATION.fullmatch(raw)
    if iso and any(iso.groups()):
        days, hours, minutes, seconds = (int(x or 0) for x in iso.groups())
        total = days * 86400 + hours * 3600 + minutes * 60 + seconds
        return float(total) if total <= 86400 else None
    match = _DURATION.fullmatch(raw)
    if match:
        hours, minutes, seconds = (int(x or 0) for x in match.groups())
        total = hours * 3600 + minutes * 60 + seconds
        return float(total) if total <= 86400 else None
    return None


def _string(data: dict, *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip() and len(value) <= 500:
            return unescape(value.strip())
    return None


def _artist(value: Any) -> str | None:
    if isinstance(value, str):
        return unescape(value.strip())[:500] or None
    if isinstance(value, dict):
        return _string(value, "name", "title")
    if isinstance(value, list):
        names = [_artist(item) for item in value]
        return ", ".join(name for name in names if name) or None
    return None


def _media_observation(*values: Any, base_url: str | None = None) -> MediaObservation:
    for value in values:
        if not isinstance(value, str) or len(value) > 4096:
            continue
        raw = unescape(value.strip())
        if not raw or raw.startswith(("data:", "blob:", "javascript:")):
            continue
        if base_url and not raw.startswith(("https://", "http://", "//")):
            raw = urljoin(base_url, raw)
        try:
            parsed = urlsplit(raw if raw.startswith("//") else "https://placeholder.invalid/" + raw.lstrip("/")
                              if raw.startswith("/") else raw)
        except ValueError:
            continue
        if parsed.scheme not in ("https", "http") or not parsed.hostname:
            continue
        path = parsed.path.casefold()
        kind = "mp3" if path.endswith(".mp3") else "hls" if path.endswith(".m3u8") else None
        if kind is None and not any(marker in path for marker in ("/get/", "/download/", "/stream/", "/audio/")):
            continue
        kind = kind or "audio_url"
        return MediaObservation(True, kind, parsed.hostname.casefold(), False)
    return MediaObservation(False, None, None, False)


def _match(reference, title: str | None, artist: str | None, duration: float | None) -> tuple[str, tuple[str, ...]]:
    if not title:
        return "mismatch", ("title_missing",)
    # Import the application matcher without wiring this experiment into routes.
    from musicstream.audio_providers.base import RecordingReference
    from musicstream.audio_providers.matching import assess

    if not isinstance(reference, RecordingReference):
        raise TypeError("Expected MusicStream RecordingReference")
    result = assess(reference, title, artist, duration)
    return result.status, tuple(result.reasons)


def _walk_dicts(value: Any, depth: int = 0):
    if depth > 24:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child, depth + 1)


def _media_from_dict(data: dict, *, base_url: str | None = None) -> MediaObservation:
    values = []
    for key in ("play", "download", "url", "src"):
        value = data.get(key)
        if isinstance(value, dict):
            values.extend((value.get("url"), value.get("src"), value.get("href")))
        else:
            values.append(value)
    return _media_observation(*values, base_url=base_url)


class HitmozAdapter:
    def __init__(self, client: SafeSiteClient):
        if client.site != "hitmoz":
            raise ValueError("Hitmoz client required")
        self.client = client

    async def search(self, reference) -> tuple[Candidate, ...]:
        query = quote_plus(f"{reference.artist} {reference.title}")
        path = f"/search?query={query}"
        result = await self.client.fetch(f"https://{self.client.host}{path}")
        if LexborHTMLParser is None:
            raise AdapterFailure("selectolax_not_installed")
        tree = LexborHTMLParser(result.body)
        candidates = []
        for node in tree.css(".tracks__item[data-musmeta]")[:12]:
            raw = node.attributes.get("data-musmeta", "")
            try:
                meta = json.loads(raw)
            except (TypeError, ValueError):
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            href_node = node.css_first('a[href*="/song/"]')
            href = href_node.attributes.get("href", "") if href_node else ""
            match = re.search(r"(?:^|/)song/([0-9]{1,14})(?:/|$)", urlsplit(urljoin(result.url, href)).path)
            item_id = match.group(1) if match else _string(meta, "id", "track_id", "musid")
            if not item_id or not _ID.fullmatch(str(item_id)):
                continue
            item_id = str(item_id)
            title = _string(meta, "title", "name")
            artist = _artist(meta.get("artist") or meta.get("artist_name") or meta.get("performer"))
            duration = _duration(meta.get("duration") or meta.get("length"))
            if title is None and href_node:
                title = (href_node.text(strip=True) or "")[:500] or None
            candidates.append(Candidate("hitmoz", item_id, artist, title, duration,
                *_match(reference, title, artist, duration), f"/song/{item_id}",
                _media_observation(meta.get("url"), meta.get("play"), meta.get("download"), base_url=result.url)))
        return tuple(candidates)

    async def inspect_song(self, reference, candidate_id: str) -> Candidate:
        if not _ID.fullmatch(candidate_id):
            raise AdapterFailure("invalid_id")
        result = await self.client.fetch(f"https://{self.client.host}/song/{candidate_id}")
        if LexborHTMLParser is None:
            raise AdapterFailure("selectolax_not_installed")
        tree = LexborHTMLParser(result.body)
        title = artist = None
        duration = None
        media_values: list[Any] = []
        for script in tree.css('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.text())
            except (ValueError, TypeError):
                continue
            for item in _walk_dicts(data):
                if str(item.get("@type", "")).casefold() in {"musicrecording", "musictrack"}:
                    title = title or _string(item, "name", "title")
                    by = item.get("byArtist") or item.get("artist")
                    artist = artist or _artist(by)
                    duration = duration or _duration(item.get("duration"))
                media_values.extend((item.get("url"), item.get("audio")))
        title_node = tree.css_first(".track__title, [data-track-title]")
        artist_node = tree.css_first(".track__artist, [data-track-artist]")
        duration_node = tree.css_first(".track__fulltime, [data-duration]")
        title = title or (title_node.attributes.get("data-track-title") if title_node else None) or (title_node.text(strip=True) if title_node else None)
        artist = artist or (artist_node.attributes.get("data-track-artist") if artist_node else None) or (artist_node.text(strip=True) if artist_node else None)
        if duration is None and duration_node:
            duration = _duration(duration_node.attributes.get("data-duration") or duration_node.text(strip=True))
        for node in tree.css(".tracks__item[data-musmeta], [data-musmeta]")[:20]:
            try:
                data = json.loads(node.attributes.get("data-musmeta", ""))
            except (TypeError, ValueError):
                continue
            if isinstance(data, dict):
                title = title or _string(data, "title", "name")
                artist = artist or _artist(data.get("artist") or data.get("artist_name") or data.get("performer"))
                duration = duration or _duration(data.get("duration") or data.get("length"))
                media_values.extend((data.get("url"), data.get("play"), data.get("download")))
        for node in tree.css("audio[src], audio source[src], a[href]")[:100]:
            attrs = node.attributes
            media_values.extend((attrs.get("src"), attrs.get("href")))
        media = _media_observation(*media_values, base_url=result.url)
        status, reasons = _match(reference, title, artist, duration)
        return Candidate("hitmoz", candidate_id, artist, title, duration, status, reasons,
                         f"/song/{candidate_id}", media)


def _rsc_payloads(html: str) -> tuple[str, ...]:
    """Extract JSON string arguments from Next flight push calls, tolerating chunking."""
    payloads = []
    marker = "self.__next_f.push("
    pos = 0
    decoder = json.JSONDecoder()
    while len(payloads) < 64:
        start = html.find(marker, pos)
        if start < 0:
            break
        array_start = start + len(marker)
        try:
            value, end = decoder.raw_decode(html[array_start:])
        except ValueError:
            pos = array_start
            continue
        pos = array_start + end
        if isinstance(value, list) and len(value) >= 2 and isinstance(value[1], str):
            payloads.append(value[1])
    return tuple(payloads)


def parse_hitmos_fm_rsc(html: str, reference, *, base_url: str = "https://rus.hitmos.fm/") -> tuple[Candidate, ...]:
    candidates: dict[str, Candidate] = {}
    for payload in _rsc_payloads(html):
        # RSC text often embeds JSON among framing and component records. Scan
        # object starts independently; malformed fragments do not poison a page.
        decoder = json.JSONDecoder()
        for index, char in enumerate(payload):
            if char != "{":
                continue
            try:
                obj, _ = decoder.raw_decode(payload[index:])
            except ValueError:
                continue
            for item in _walk_dicts(obj):
                item_id = item.get("id")
                if not isinstance(item_id, (str, int)) or not _ID.fullmatch(str(item_id)):
                    continue
                title = _string(item, "title", "name")
                artist = _artist(item.get("artist") or item.get("artistName") or item.get("performer"))
                if not title and not artist:
                    continue
                duration = _duration(item.get("duration") or item.get("length"))
                media = _media_from_dict(item, base_url=base_url)
                # RSC can include unrelated site-wide cards (radio stations,
                # promotions). Keep records carrying track-like metadata.
                if not artist and duration is None and not media.present:
                    continue
                status, reasons = _match(reference, title, artist, duration)
                candidates[str(item_id)] = Candidate("hitmos_fm", str(item_id), artist, title, duration,
                    status, reasons, f"/track/{item_id}",
                    media)
                if len(candidates) >= 24:
                    return tuple(sorted(candidates.values(), key=lambda item: ({"likely": 0, "uncertain": 1, "mismatch": 2}[item.match], item.id)))
    return tuple(sorted(candidates.values(), key=lambda item: ({"likely": 0, "uncertain": 1, "mismatch": 2}[item.match], item.id)))


class HitmosFmAdapter:
    def __init__(self, client: SafeSiteClient):
        if client.site != "hitmos_fm":
            raise ValueError("Hitmos.fm client required")
        self.client = client

    async def search(self, reference) -> tuple[Candidate, ...]:
        query = quote_plus(f"{reference.artist} {reference.title}")
        path = f"/search?query={query}"
        result = await self.client.fetch(f"https://{self.client.host}{path}")
        if LexborHTMLParser is None:
            raise AdapterFailure("selectolax_not_installed")
        tree = LexborHTMLParser(result.body)
        candidates: dict[str, Candidate] = {c.id: c for c in parse_hitmos_fm_rsc(result.body, reference, base_url=result.url)}
        # HTML rows supplement incomplete RSC fields; never infer a play URL.
        for row in tree.css(".track-item, [data-track-id]")[:24]:
            item_id = row.attributes.get("data-track-id") or row.attributes.get("data-id")
            if not item_id or not _ID.fullmatch(item_id):
                link = row.css_first('a[href*="/track/"], a[href*="/song/"]')
                href = link.attributes.get("href", "") if link else ""
                found = re.search(r"/(?:track|song)/([0-9]{1,14})(?:/|$)", urlsplit(urljoin(result.url, href)).path)
                item_id = found.group(1) if found else None
            if not item_id or not _ID.fullmatch(item_id):
                continue
            title_node = row.css_first(".track-item__title, [data-title]")
            artist_node = row.css_first(".track-item__artist, [data-artist]")
            title = title_node.attributes.get("data-title") if title_node else None
            title = title or (title_node.text(strip=True) if title_node else None)
            artist = artist_node.attributes.get("data-artist") if artist_node else None
            artist = artist or (artist_node.text(strip=True) if artist_node else None)
            duration_node = row.css_first(".track-item__duration, [data-duration]")
            duration = _duration((duration_node.attributes.get("data-duration") or duration_node.text(strip=True)) if duration_node else None)
            if not title and not artist:
                continue
            status, reasons = _match(reference, title, artist, duration)
            prior = candidates.get(item_id)
            candidates[item_id] = Candidate("hitmos_fm", item_id, artist or (prior.artist if prior else None),
                title or (prior.title if prior else None), duration or (prior.duration_seconds if prior else None),
                status, reasons, f"/track/{item_id}", prior.media if prior else MediaObservation(False, None, None))
        return tuple(sorted(candidates.values(), key=lambda item: ({"likely": 0, "uncertain": 1, "mismatch": 2}[item.match], item.id)))

