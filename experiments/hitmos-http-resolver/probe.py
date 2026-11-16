"""Compare the existing Hitmos Browser Reader with a pinned HTTPX prototype.

This experiment performs only fixed-origin HTML GETs and, through the existing
Browser Reader, the site's ordinary player request. It never follows redirects
to media or reads audio bytes. Run with ``.venv\\Scripts\\python.exe
experiments\\hitmos-http-resolver\\probe.py --live``.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import httpcore
import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from musicstream.audio_providers.base import RecordingReference, SourceDocument  # noqa: E402
from musicstream.audio_providers.hitmos import (  # noqa: E402
    parse_search_ids, parse_search_metadata, parse_song, valid_stream_url,
)
from musicstream.audio_providers.hitmos_browser import HitmosBrowserReader  # noqa: E402
from musicstream.audio_providers.hitmos_stream import validated_audio_url  # noqa: E402


HOST = "ru.hitmox.org"
MAX_HTML = 262144
TIMEOUT = 7.0
TRACKS = (
    RecordingReference(title="Sonne", artist="Rammstein", duration_seconds=272),
    RecordingReference(title="Du hast", artist="Rammstein", duration_seconds=235),
    RecordingReference(title="Mutter", artist="Rammstein", duration_seconds=268),
    RecordingReference(title="Blinding Lights", artist="The Weeknd", duration_seconds=200),
    RecordingReference(title="Группа крови", artist="Кино", duration_seconds=284),
)


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    """Connect only to addresses checked as public; TLS still verifies HOST."""

    def __init__(self, addresses: tuple[str, ...]):
        self.addresses = addresses
        self.backend = httpcore.AnyIOBackend()
        self.index = 0

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        name = host.decode("ascii") if isinstance(host, bytes) else str(host)
        if name.casefold() != HOST or port != 443:
            raise OSError("destination_not_allowed")
        ip = self.addresses[self.index % len(self.addresses)]
        self.index += 1
        return await self.backend.connect_tcp(ip, port, timeout, local_address, socket_options)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise OSError("unix_sockets_not_allowed")

    async def sleep(self, seconds):
        await self.backend.sleep(seconds)


class _PinnedTransport(httpx.AsyncHTTPTransport):
    def __init__(self, addresses: tuple[str, ...]):
        super().__init__(verify=True, http1=True, http2=False, retries=0)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=True),
            max_connections=1,
            max_keepalive_connections=1,
            network_backend=_PinnedBackend(addresses),
        )


async def _public_addresses() -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    answers = await asyncio.wait_for(
        loop.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM), TIMEOUT
    )
    addresses = tuple(dict.fromkeys(str(row[4][0]) for row in answers))
    if not addresses or len(addresses) > 16 or any(
        not ipaddress.ip_address(value).is_global for value in addresses
    ):
        raise RuntimeError("dns_address_not_public")
    return addresses


class HttpxResolver:
    def __init__(self, addresses: tuple[str, ...]):
        self.transport = _PinnedTransport(addresses)
        self.client = httpx.AsyncClient(
            transport=self.transport, follow_redirects=False, timeout=TIMEOUT,
            trust_env=False, headers={"User-Agent": "MusicStream/0.1.0",
                                      "Accept": "text/html,application/xhtml+xml"},
        )
        self.requests = 0
        self.last_request_at = 0.0

    async def close(self):
        await self.client.aclose()

    async def get_html(self, path: str) -> tuple[str, int, float, str | None]:
        if not path.startswith(("/search?", "/song/")) or "#" in path:
            raise ValueError("path_not_allowed")
        delay = 1.0 - (time.monotonic() - self.last_request_at)
        if delay > 0:
            await asyncio.sleep(delay)
        self.last_request_at = time.monotonic()
        started = time.perf_counter()
        self.requests += 1
        async with self.client.stream("GET", f"https://{HOST}{path}") as response:
            elapsed = (time.perf_counter() - started) * 1000
            if response.is_redirect:
                # Search/song document redirects are not followed.
                return "", response.status_code, elapsed, "redirect_not_followed"
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if response.status_code != 200:
                return "", response.status_code, elapsed, f"http_{response.status_code}"
            if content_type not in {"text/html", "application/xhtml+xml"}:
                return "", response.status_code, elapsed, "not_html"
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_HTML:
                    return "", response.status_code, elapsed, "response_too_large"
            return body.decode(response.encoding or "utf-8", "replace"), response.status_code, elapsed, None


async def httpx_track(resolver: HttpxResolver, recording: RecordingReference) -> dict:
    query = f"{recording.artist} {recording.title}"
    started = time.perf_counter()
    request_before = resolver.requests
    try:
        html, status, search_ms, error = await resolver.get_html("/search?" + urlencode({"q": query}))
    except (httpx.HTTPError, OSError, TimeoutError) as exc:
        return {"method": "httpx", "title": recording.title, "status": None,
                "search_ms": round((time.perf_counter() - started) * 1000, 1),
                "metadata_ms": None, "audio_source_ms": None, "requests": 1,
                "audio_redirect": None, "range": "not_checked_audio_not_fetched",
                "error": type(exc).__name__}
    row = {"method": "httpx", "title": recording.title, "status": status,
           "search_ms": round(search_ms, 1), "metadata_ms": None,
           "audio_source_ms": None, "requests": 1, "audio_redirect": None,
           "range": "not_checked_audio_not_fetched", "error": error}
    if error:
        row["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return row
    ids = parse_search_ids(html)
    embedded = parse_search_metadata(html, ids)
    row["candidates"] = [
        {"id": song_id, "title": embedded.get(song_id, (None, None, None))[0],
         "artist": embedded.get(song_id, (None, None, None))[1],
         "duration_seconds": embedded.get(song_id, (None, None, None))[2]}
        for song_id in ids[:5]
    ]
    song_id = ids[0] if ids else None
    if song_id:
        before = resolver.requests
        metadata_started = time.perf_counter()
        song_html, song_status, _, song_error = await resolver.get_html(f"/song/{song_id}")
        row["metadata_ms"] = round((time.perf_counter() - metadata_started) * 1000, 1)
        row["status_song"] = song_status
        row["metadata_error"] = song_error
        row["requests"] = resolver.requests - request_before
        row["song_metadata"] = None
        if not song_error:
            title, artist, duration = parse_song(song_html, song_id)
            row["song_metadata"] = {"title": title, "artist": artist, "duration_seconds": duration}
            # Record only whether the page embeds a syntactically accepted player URL.
            # Never request/follow it in this prototype.
            from musicstream.audio_providers.hitmos import inspect_player_url
            player_url, _ = inspect_player_url(song_html, song_id, lab_mode=True)
            is_safe_shape = bool(player_url and (
                valid_stream_url(player_url, song_id) or validated_audio_url(player_url)
            ))
            row["audio_source_ms"] = None
            row["audio_redirect"] = "player_url_embedded" if is_safe_shape else "not_available_in_html"
        row["requests"] = resolver.requests - request_before
        if resolver.requests < before:
            raise AssertionError("request_counter_regressed")
    row["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return row


async def browser_track(reader: HitmosBrowserReader, recording: RecordingReference) -> dict:
    started = time.perf_counter()
    search_started = time.perf_counter()
    try:
        document = await reader.read_search(f"{recording.artist} {recording.title}")
        search_ms = (time.perf_counter() - search_started) * 1000
        html = document.html if isinstance(document, SourceDocument) else document
        ids = parse_search_ids(html)
        candidates = parse_search_metadata(html, ids)
        row = {"method": "browser_reader", "title": recording.title,
               "search_ms": round(search_ms, 1), "metadata_ms": None,
               "audio_source_ms": None, "requests": "document + robots/player (not exposed)",
               "candidates": [
                   {"id": song_id, "title": candidates.get(song_id, (None, None, None))[0],
                    "artist": candidates.get(song_id, (None, None, None))[1],
                    "duration_seconds": candidates.get(song_id, (None, None, None))[2]}
                   for song_id in ids[:5]
               ]}
        if ids:
            metadata_started = time.perf_counter()
            detail = await reader.read_song(ids[0])
            row["metadata_ms"] = round((time.perf_counter() - metadata_started) * 1000, 1)
            row["audio_source_ms"] = row["metadata_ms"]
            row["detail_status"] = detail.diagnostics.metadata_status if detail.diagnostics else None
            row["redirect_status"] = detail.diagnostics.redirect_status if detail.diagnostics else None
            row["redirect_validated"] = detail.diagnostics.redirect_valid if detail.diagnostics else None
            row["song_metadata"] = None
            if detail.html:
                title, artist, duration = parse_song(detail.html, ids[0])
                row["song_metadata"] = {"title": title, "artist": artist, "duration_seconds": duration}
        row["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return row
    except Exception as exc:  # Store safe error class; no URLs, headers, or tokens.
        return {"method": "browser_reader", "title": recording.title,
                "total_ms": round((time.perf_counter() - started) * 1000, 1),
                "error": type(exc).__name__}


async def main() -> None:
    try:
        addresses = await _public_addresses()
    except Exception as exc:
        output = ROOT / "experiments" / "hitmos-http-resolver" / "results.json"
        output.write_text(json.dumps([{"pass": "cold", "method": "httpx",
                                       "error": type(exc).__name__}], indent=2) + "\n",
                          encoding="utf-8")
        print(json.dumps({"method": "httpx", "error": type(exc).__name__}, ensure_ascii=False))
        return
    httpx_resolver = HttpxResolver(addresses)
    rows = []
    try:
        blocked = False
        for recording in TRACKS:
            if blocked:
                row = {"pass": "cold", "method": "httpx", "title": recording.title,
                       "skipped": "previous_request_was_403"}
            else:
                try:
                    row = await httpx_track(httpx_resolver, recording)
                except Exception as exc:
                    row = {"pass": "cold", "method": "httpx", "title": recording.title,
                           "error": type(exc).__name__}
                if row.get("status") == 403 or row.get("error") == "HTTPStatusError":
                    blocked = True
            row.setdefault("pass", "cold")
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
        if blocked:
            rows.extend({"pass": "repeat", "method": "httpx", "title": recording.title,
                         "skipped": "http_403_is_not_retried"} for recording in TRACKS)
            print(json.dumps({"pass": "repeat", "method": "httpx",
                              "skipped": "http_403_is_not_retried"}, ensure_ascii=False), flush=True)
        else:
            # Repeat only when the ordinary cold GET was accepted.
            for recording in TRACKS:
                row = {"pass": "repeat", **await httpx_track(httpx_resolver, recording)}
                rows.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        await httpx_resolver.close()
    output = ROOT / "experiments" / "hitmos-http-resolver" / "results.json"
    output.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="make fixed-origin public HTML requests")
    args = parser.parse_args()
    if not args.live:
        parser.error("pass --live to make rate-limited HTTPX and Browser Reader requests")
    asyncio.run(main())
