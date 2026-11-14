"""Fetch one approved v1 torrent piece from Internet Archive HTTPS web seeds.

This module has no libtorrent session and is used only by explicit local playback.
Every HTTP hop is resolved, connected to a checked literal IP, and verified by
TLS for its original hostname. Only a complete SHA-1-verified piece is returned.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import re
import socket
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit

from .metadata import _libtorrent


_ARCHIVE_CDN = re.compile(r"dn[0-9]{6}\.(?:us|ca)\.archive\.org\Z")
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)\Z")
_APPROVED_BASE = "https://archive.org/download/"


class UnsafeWebSeedError(ValueError):
    """A URL, DNS answer, or connection violated the egress policy."""


class WebSeedHTTPError(IOError):
    """A web seed did not return the exact requested HTTP byte range."""


class PieceHashMismatchError(IOError):
    """A complete piece did not match its v1 torrent SHA-1 hash."""


@dataclass(frozen=True)
class WebSeedDiagnostics:
    allowed_web_seeds: int
    rejected_web_seeds: int
    connections: int
    received_bytes: int
    verified_pieces: int
    http_errors: int
    first_piece_seconds: float | None
    redirects: tuple[str, ...]
    dns_seconds: float = 0.0
    tcp_seconds: float = 0.0
    tls_seconds: float = 0.0
    response_seconds: float = 0.0
    body_seconds: float = 0.0
    sha1_seconds: float = 0.0
    retries: int = 0
    worker_seconds: float = 0.0
    failed_attempt_seconds: float = 0.0
    reused_connections: int = 0


class SafeWebSeedFetcher:
    """Bounded, fail-closed fetcher for one explicitly approved Archive torrent."""

    def __init__(
        self, torrent_path: str | Path, *, approved_info_hashes: frozenset[str],
        archive_item: str = "amp021",
        max_piece_bytes: int = 1024 * 1024, max_redirects: int = 3,
        max_connections: int = 8, timeout_seconds: float = 15,
    ) -> None:
        if not approved_info_hashes:
            raise ValueError("An explicit info-hash allowlist is required")
        if not 1 <= max_piece_bytes <= 2 * 1024 * 1024:
            raise ValueError("Invalid piece limit")
        if not 0 <= max_redirects <= 5 or not 1 <= max_connections <= 12:
            raise ValueError("Invalid redirect or connection limit")
        if not 1 <= timeout_seconds <= 60:
            raise ValueError("Invalid timeout")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", archive_item):
            raise ValueError("Invalid Archive item identifier")
        source = Path(torrent_path)
        if source.suffix.lower() != ".torrent" or not source.is_file() or source.stat().st_size > 1024 * 1024:
            raise ValueError("Expected a small local .torrent")
        lt = _libtorrent()
        params = lt.load_torrent_file(str(source))
        self._info = params.ti
        hashes = self._info.info_hashes()
        if self._info.priv() or not hashes.has_v1() or str(hashes.v1) not in approved_info_hashes:
            raise ValueError("A public, approved v1 info hash is required")
        self._item = self._info.name()
        if self._item != archive_item:
            raise ValueError("Torrent name does not match the approved Archive item")
        seeds = tuple(params.url_seeds) + tuple(params.http_seeds)
        self._allowed_seeds = tuple(seed for seed in seeds if seed == _APPROVED_BASE)
        if len(self._allowed_seeds) != 1:
            raise UnsafeWebSeedError("Exactly one approved HTTPS Archive seed is required")
        self._rejected_seeds = len(seeds) - 1
        self._storage = self._info.files()
        self.max_piece_bytes = max_piece_bytes
        self.max_redirects = max_redirects
        self.max_connections = max_connections
        self.timeout_seconds = timeout_seconds
        self._connections = 0
        self._received = 0
        self._verified = 0
        self._http_errors = 0
        self._first_piece_seconds: float | None = None
        self._redirect_hosts: list[str] = []
        self._timings = dict.fromkeys(("dns", "tcp", "tls", "response", "body", "sha1"), 0.0)
        self._kept: tuple[str, str, ssl.SSLSocket, int] | None = None
        self._redirect_urls: dict[str, str] = {}
        self._reused_connections = 0

    def diagnostics(self) -> WebSeedDiagnostics:
        return WebSeedDiagnostics(
            len(self._allowed_seeds), self._rejected_seeds, self._connections,
            self._received, self._verified, self._http_errors,
            self._first_piece_seconds, tuple(self._redirect_hosts),
            *(self._timings[key] for key in ("dns", "tcp", "tls", "response", "body", "sha1")),
            reused_connections=self._reused_connections,
        )

    def close(self) -> None:
        if self._kept is not None:
            self._kept[2].close()
            self._kept = None

    def fetch_piece(self, piece_index: int) -> bytes:
        """Return a whole piece only after its torrent SHA-1 has been checked."""
        return self.fetch_pieces(piece_index, 1)[0]

    def fetch_pieces(self, first: int, count: int) -> tuple[bytes, ...]:
        """Fetch at most two adjacent pieces per bounded HTTP Range, then verify each."""
        if (isinstance(first, bool) or isinstance(count, bool) or
                not isinstance(first, int) or not isinstance(count, int) or
                not 0 <= first < self._info.num_pieces() or
                not 1 <= count <= 2 or first + count > self._info.num_pieces()):
            raise ValueError("Invalid piece span")
        sizes = tuple(self._info.piece_size(index) for index in range(first, first + count))
        if any(size > self.max_piece_bytes for size in sizes) or sum(sizes) > 2 * 1024 * 1024:
            raise ValueError("Piece span exceeds download limit")
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        remaining_connections = self.max_connections
        begin = first * self._storage.piece_length()
        end = begin + sum(sizes)
        result = bytearray()
        for index in range(self._storage.num_files()):
            file_begin = self._storage.file_offset(index)
            file_end = file_begin + self._storage.file_size(index)
            overlap_begin = max(begin, file_begin)
            overlap_end = min(end, file_end)
            if overlap_begin >= overlap_end:
                continue
            length = overlap_end - overlap_begin
            if self._storage.file_flags(index) & _libtorrent().file_storage.flag_pad_file:
                result.extend(bytes(length))
                continue
            path = self._safe_file_path(index)
            url = _APPROVED_BASE + quote(path, safe="/")
            payload, used = self._fetch_range(
                url, path, overlap_begin - file_begin, overlap_end - file_begin - 1,
                self._storage.file_size(index), deadline, remaining_connections,
            )
            remaining_connections -= used
            result.extend(payload)
        if len(result) != sum(sizes):
            raise WebSeedHTTPError("Torrent piece has a gap in its file mapping")
        hash_started = time.monotonic()
        pieces = []
        offset = 0
        for index, size in enumerate(sizes, first):
            piece = bytes(result[offset:offset + size])
            if hashlib.sha1(piece).digest() != bytes(self._info.hash_for_piece(index)):
                raise PieceHashMismatchError("Torrent piece failed SHA-1 verification")
            pieces.append(piece)
            offset += size
        self._timings["sha1"] += time.monotonic() - hash_started
        self._verified += count
        if self._first_piece_seconds is None:
            self._first_piece_seconds = time.monotonic() - started
        return tuple(pieces)

    def _safe_file_path(self, index: int) -> str:
        path = self._storage.file_path(index).replace("\\", "/")
        parts = path.split("/")
        if len(parts) < 2 or parts[0] != self._item or len(path) > 1024 or any(
            part in ("", ".", "..") or any(ord(char) < 32 for char in part)
            for part in parts
        ):
            raise UnsafeWebSeedError("Unsafe torrent file path")
        return path

    def _validate_url(self, url: str, file_path: str) -> str:
        if len(url) > 2048:
            raise UnsafeWebSeedError("Web seed URL is too long")
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise UnsafeWebSeedError("Invalid web seed port") from exc
        host = parsed.hostname
        if (parsed.scheme != "https" or port not in (None, 443) or not host or
                parsed.username is not None or parsed.password is not None or
                parsed.query or parsed.fragment):
            raise UnsafeWebSeedError("Web seed URL is not approved HTTPS")
        if host != "archive.org" and not _ARCHIVE_CDN.fullmatch(host):
            raise UnsafeWebSeedError("Web seed host is not approved")
        decoded = unquote(parsed.path)
        if any(part in (".", "..") for part in decoded.split("/")):
            raise UnsafeWebSeedError("Unsafe web seed path")
        if host == "archive.org":
            allowed = f"/download/{file_path}"
            if decoded != allowed:
                raise UnsafeWebSeedError("Unexpected Archive download path")
        elif not re.fullmatch(r"/[0-9]+/items/" + re.escape(file_path), decoded):
            raise UnsafeWebSeedError("Unexpected Archive storage path")
        return host

    @staticmethod
    def _resolve(host: str) -> tuple[str, ...]:
        try:
            answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise UnsafeWebSeedError("Web seed DNS failed") from exc
        ips = tuple(dict.fromkeys(answer[4][0] for answer in answers))
        if not ips or len(ips) > 16:
            raise UnsafeWebSeedError("Invalid number of DNS answers")
        try:
            invalid = any(not ipaddress.ip_address(ip).is_global for ip in ips)
        except ValueError as exc:
            raise UnsafeWebSeedError("Web seed DNS returned an invalid address") from exc
        if invalid:
            raise UnsafeWebSeedError("Web seed DNS returned a non-global address")
        return ips

    @staticmethod
    def _connect(host: str, ip: str, timeout: float, timings: dict[str, float] | None = None) -> ssl.SSLSocket:
        # Connecting to a literal IP prevents an implicit second DNS lookup.
        started = time.monotonic()
        raw = socket.create_connection((ip, 443), timeout=timeout)
        if timings is not None:
            timings["tcp"] += time.monotonic() - started
        try:
            if ipaddress.ip_address(raw.getpeername()[0]) != ipaddress.ip_address(ip):
                raise UnsafeWebSeedError("Connected to an unexpected IP")
            context = ssl.create_default_context()
            started = time.monotonic()
            secure = context.wrap_socket(raw, server_hostname=host)
            if timings is not None:
                timings["tls"] += time.monotonic() - started
            if ipaddress.ip_address(secure.getpeername()[0]) != ipaddress.ip_address(ip):
                secure.close()
                raise UnsafeWebSeedError("TLS connection changed remote IP")
            return secure
        except BaseException:
            raw.close()
            raise

    def _fetch_range(
        self, url: str, file_path: str, start: int, end: int, total: int,
        deadline: float, connection_budget: int,
    ) -> tuple[bytes, int]:
        expected = end - start + 1
        used = 0
        url = self._redirect_urls.get(file_path, url)
        for hop in range(self.max_redirects + 1):
            host = self._validate_url(url, file_path)
            secure = None
            uses = 0
            ip = None
            if self._kept is not None:
                kept_host, kept_ip, kept_socket, kept_uses = self._kept
                self._kept = None
                if kept_host == host and kept_uses < 4:
                    try:
                        if ipaddress.ip_address(kept_socket.getpeername()[0]) == ipaddress.ip_address(kept_ip):
                            secure, ip, uses = kept_socket, kept_ip, kept_uses
                            self._reused_connections += 1
                    except OSError:
                        pass
                if secure is None:
                    kept_socket.close()
            if secure is None:
                started = time.monotonic()
                ips = self._resolve(host)
                self._timings["dns"] += time.monotonic() - started
                for ip in ips:
                    if used >= connection_budget:
                        raise WebSeedHTTPError("Connection limit reached")
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise TimeoutError("Web seed deadline exceeded")
                    used += 1
                    self._connections += 1
                    try:
                        secure = self._connect(host, ip, min(left, 5), self._timings)
                        break
                    except (OSError, ssl.SSLError):
                        continue
            if secure is None:
                raise WebSeedHTTPError("Unable to connect to approved web seed")
            try:
                parsed = urlsplit(url)
                secure.settimeout(max(0.1, deadline - time.monotonic()))
                request = (
                    f"GET {parsed.path} HTTP/1.1\r\nHost: {host}\r\n"
                    f"Range: bytes={start}-{end}\r\nAccept-Encoding: identity\r\n"
                    "Connection: keep-alive\r\nUser-Agent: MusicStream-safe-webseed/1\r\n\r\n"
                )
                secure.sendall(request.encode("ascii"))
                response = http.client.HTTPResponse(secure)
                started = time.monotonic()
                response.begin()
                self._timings["response"] += time.monotonic() - started
                if response.status in (301, 302, 303, 307, 308):
                    if hop >= self.max_redirects:
                        raise WebSeedHTTPError("Redirect limit reached")
                    location = response.getheader("Location")
                    if not location:
                        raise WebSeedHTTPError("Redirect has no location")
                    next_url = urljoin(url, location)
                    next_host = self._validate_url(next_url, file_path)
                    self._redirect_hosts.append(next_host)
                    url = next_url
                    continue
                if response.status != 206:
                    raise WebSeedHTTPError(f"Expected HTTP 206, got {response.status}")
                match = _CONTENT_RANGE.fullmatch(response.getheader("Content-Range", ""))
                if not match or tuple(map(int, match.groups())) != (start, end, total):
                    raise WebSeedHTTPError("Incorrect Content-Range")
                if response.getheader("Content-Length") != str(expected):
                    raise WebSeedHTTPError("Incorrect Content-Length")
                if response.getheader("Transfer-Encoding") or response.getheader("Content-Encoding", "identity") != "identity":
                    raise WebSeedHTTPError("Unsupported response encoding")
                chunks = bytearray()
                body_started = time.monotonic()
                while len(chunks) < expected:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise TimeoutError("Web seed deadline exceeded")
                    secure.settimeout(left)
                    chunk = response.read(min(64 * 1024, expected - len(chunks)))
                    if not chunk:
                        raise WebSeedHTTPError("Web seed response ended early")
                    chunks.extend(chunk)
                    self._received += len(chunk)
                self._timings["body"] += time.monotonic() - body_started
                self._redirect_urls[file_path] = url
                if not response.will_close and response.version == 11 and uses < 3:
                    self._kept = (host, ip, secure, uses + 1)
                    secure = None
                return bytes(chunks), used
            except (WebSeedHTTPError, TimeoutError):
                self._http_errors += 1
                raise
            except (OSError, http.client.HTTPException) as exc:
                self._http_errors += 1
                raise WebSeedHTTPError("Web seed connection or response failed") from exc
            finally:
                if secure is not None:
                    secure.close()
        raise WebSeedHTTPError("Redirect limit reached")
