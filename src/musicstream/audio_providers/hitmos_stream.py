"""Bounded, DNS-pinned HTTPS transport for Hitmos audio in local lab mode."""

import asyncio
import hashlib
import http.client
import ipaddress
import re
import secrets
import socket
import ssl
import time
from urllib.parse import unquote_to_bytes
from collections import OrderedDict
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

from musicstream.audio_providers.transport import SourceFailure
from musicstream.audio_providers.hitmos_proxy_config import configured_hitmos_proxy

MAX_REDIRECTS = 3
MAX_PROBE_BYTES = 4096
MAX_TOKENS = 128
TOKEN_TTL_SECONDS = 1800
_CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$", re.IGNORECASE)
_RANGE = re.compile(r"^bytes=(?:(\d+)-(\d*)|-(\d+))$", re.IGNORECASE)
_DNS_NAME = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*$", re.IGNORECASE)


def _path_type(url: str) -> str:
    path = urlsplit(url).path.lower()
    return "cuts" if "/cuts/" in path else "music" if "/music/" in path else "other"


def diagnostic_host_value(host: str | None) -> str | None:
    return "host_" + hashlib.sha256(host.lower().encode()).hexdigest()[:12] if host else None


def diagnostic_host(url: str) -> str | None:
    return diagnostic_host_value(urlsplit(url).hostname)


def validated_audio_url(url: str) -> bool:
    """Lexical checks only; the connection layer separately pins public DNS."""
    if not isinstance(url, str) or len(url) > 8192 or url != url.strip():
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        return False
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if (parsed.scheme != "https" or not host or parsed.port not in (None, 443)
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment or not parsed.path.startswith("/")
                or len(parsed.path) > 4096 or len(parsed.query) > 4096
                or "\\" in parsed.path):
            return False
        host = host.rstrip(".").lower()
        try:
            ipaddress.ip_address(host)
            return False
        except ValueError:
            pass
        if not _DNS_NAME.fullmatch(host):
            return False
        # Encoded separators make the validated path differ across HTTP stacks.
        if re.search(r"%(?:2f|5c|00)", parsed.path, re.IGNORECASE):
            return False
        decoded = unquote_to_bytes(parsed.path)
        if any(byte < 32 or byte == 127 for byte in decoded) or b"\\" in decoded:
            return False
        decoded_path = decoded.decode("ascii", errors="ignore")
        if any(segment in {".", ".."} for segment in decoded_path.split("/")):
            return False
        return True
    except ValueError:
        return False


def parse_range_header(value: str | None) -> tuple[str | None, tuple[int | None, int | None] | None]:
    if value is None:
        return None, None
    if not isinstance(value, str) or len(value) > 100 or "," in value:
        raise SourceFailure("invalid_range", owner="local_policy")
    match = _RANGE.fullmatch(value.strip())
    if not match:
        raise SourceFailure("invalid_range", owner="local_policy")
    if match[3] is not None:
        suffix = int(match[3])
        if suffix <= 0:
            raise SourceFailure("invalid_range", owner="local_policy")
        return f"bytes=-{suffix}", (None, None)
    start = int(match[1])
    end = int(match[2]) if match[2] else None
    if end is not None and end < start:
        raise SourceFailure("invalid_range", owner="local_policy")
    return f"bytes={start}-{'' if end is None else end}", (start, end)


def retry_after_seconds(value: str | None, *, now: float | None = None) -> int | None:
    if not value:
        return None
    try:
        delay = int(value.strip())
    except (ValueError, AttributeError):
        try:
            parsed = parsedate_to_datetime(value)
            delay = int(parsed.timestamp() - (time.time() if now is None else now))
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0, min(delay, 3600))


def is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
        return ip.is_global and not ip.is_private and not ip.is_loopback and not ip.is_link_local and not ip.is_multicast
    except ValueError:
        return False


def audio_signature(data: bytes) -> bool:
    if not data:
        return False
    if data.startswith((b"ID3", b"fLaC", b"OggS")):
        return True
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return True
    if len(data) >= 8 and data[4:8] == b"ftyp":
        return True
    # MPEG audio frames and ADTS AAC both start with a 12-bit sync word.
    return any(data[index] == 0xFF and (data[index + 1] & 0xE0) == 0xE0
               for index in range(max(0, len(data) - 1)))


def audio_content_type(value: str | None) -> bool:
    media_type = (value or "").split(";", 1)[0].strip().lower()
    return media_type.startswith("audio/") or media_type == "application/octet-stream"


def _resolve_public_addresses(host: str) -> list[str]:
    try:
        records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(record[4][0] for record in records))
    except (OSError, ValueError) as exc:
        raise SourceFailure("audio_dns_failed", owner="network", host=host) from exc
    if not addresses or len(addresses) > 16 or any(not is_public_ip(address) for address in addresses):
        raise SourceFailure("audio_dns_not_public", owner="local_policy", host=host)
    return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, ip: str, *, proxy_url: str | None = None, timeout: float = 5.0):
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self._pinned_ip = ip
        self._proxy_url = proxy_url

    def connect(self):
        if self._proxy_url:
            proxy = urlsplit(self._proxy_url)
            raw = socket.create_connection((proxy.hostname, proxy.port), self.timeout)
            try:
                pinned_host = f"[{self._pinned_ip}]" if ":" in self._pinned_ip else self._pinned_ip
                authority = f"{pinned_host}:443"
                request = (f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n"
                           "Proxy-Connection: keep-alive\r\n\r\n").encode("ascii")
                raw.sendall(request)
                response = bytearray()
                while b"\r\n\r\n" not in response and len(response) <= 8192:
                    chunk = raw.recv(1024)
                    if not chunk:
                        break
                    response.extend(chunk)
                if len(response) > 8192 or b"\r\n\r\n" not in response:
                    raise SourceFailure("proxy_connect_invalid_response", owner="network", stage="proxy_connect")
                header_block = bytes(response).split(b"\r\n\r\n", 1)[0]
                status_line = header_block.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
                match = re.fullmatch(r"HTTP/1\.[01] ([0-9]{3})(?: .*)?", status_line)
                if not match or int(match[1]) != 200:
                    status = int(match[1]) if match else None
                    retry_value = None
                    for line in header_block.split(b"\r\n")[1:]:
                        key, separator, value = line.partition(b":")
                        if separator and key.strip().lower() == b"retry-after":
                            retry_value = value.decode("ascii", errors="ignore").strip()
                            break
                    raise SourceFailure("proxy_connect_denied", owner="network", stage="proxy_connect",
                        http_status=status, retry_after_seconds=retry_after_seconds(retry_value))
            except BaseException:
                raw.close()
                raise
        else:
            raw = socket.create_connection((self._pinned_ip, 443), self.timeout)
        try:
            # TLS certificate validation and SNI use the original DNS hostname.
            # CONNECT targets the already checked IP to prevent proxy-side DNS rebinding.
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


@dataclass
class _UpstreamResponse:
    connection: _PinnedHTTPSConnection
    response: http.client.HTTPResponse
    final_url: str

    def close(self):
        try:
            self.response.close()
        finally:
            self.connection.close()


def _request_pinned(url: str, ip: str, range_value: str | None,
                    proxy_url: str | None = None) -> _UpstreamResponse:
    parsed = urlsplit(url)
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    connection = _PinnedHTTPSConnection(parsed.hostname, ip, proxy_url=proxy_url)
    try:
        headers = {
            "Host": parsed.hostname,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "User-Agent": "MusicStream/0.1.0",
            # A cross-origin <audio> request from Hitmos sends its origin as
            # Referer under the browser's default strict-origin policy.
            "Referer": "https://ru.hitmox.org/",
            "Connection": "close",
        }
        if range_value:
            headers["Range"] = range_value
        connection.request("GET", target, headers=headers)
        response = connection.getresponse()
        return _UpstreamResponse(connection, response, url)
    except BaseException:
        connection.close()
        raise


async def _open_checked(url: str, range_value: str | None,
                        proxy_url: str | None = None) -> _UpstreamResponse:
    current = url
    for redirect_count in range(MAX_REDIRECTS + 1):
        if not validated_audio_url(current):
            raise SourceFailure("audio_url_rejected", owner="local_policy")
        parsed = urlsplit(current)
        host = parsed.hostname.rstrip(".").lower()
        try:
            addresses = await asyncio.wait_for(asyncio.to_thread(_resolve_public_addresses, host), timeout=2.5)
        except TimeoutError as exc:
            raise SourceFailure("audio_dns_timeout", owner="network", host=host) from exc
        last_error = None
        upstream = None
        for address in addresses[:2]:
            try:
                upstream = await asyncio.wait_for(
                    asyncio.to_thread(_request_pinned, current, address, range_value, proxy_url), timeout=5.5)
                break
            except (OSError, http.client.HTTPException, ssl.SSLError, TimeoutError) as exc:
                last_error = exc
        if upstream is None:
            raise SourceFailure("audio_connection_failed", owner="network", host=host,
                                path_type=_path_type(current), stage="redirect") from last_error
        status = upstream.response.status
        if status not in {301, 302, 303, 307, 308}:
            if status == 429:
                delay = retry_after_seconds(upstream.response.getheader("Retry-After"))
                upstream.close()
                raise SourceFailure("upstream_rate_limited", owner="external_server", host=host,
                                    path_type=_path_type(current), http_status=status,
                                    retry_after_seconds=delay, stage="http_range")
            if status >= 400:
                retry_after = retry_after_seconds(upstream.response.getheader("Retry-After"))
                upstream.close()
                raise SourceFailure(f"upstream_http_{status}", owner="external_server", host=host,
                                    path_type=_path_type(current), http_status=status,
                                    retry_after_seconds=retry_after, stage="http_range")
            return upstream
        location = upstream.response.getheader("Location")
        upstream.close()
        if redirect_count >= MAX_REDIRECTS:
            raise SourceFailure("audio_redirect_limit", owner="local_policy", host=host,
                                http_status=status)
        if not location:
            raise SourceFailure("audio_redirect_missing_location", owner="external_server", host=host,
                                http_status=status)
        target = urljoin(current, location)
        if not validated_audio_url(target):
            raise SourceFailure("audio_redirect_rejected", owner="local_policy", host=host,
                                http_status=status)
        current = target
    raise SourceFailure("audio_redirect_limit", owner="local_policy")


def _content_range(value: str | None) -> tuple[int, int, int] | None:
    match = _CONTENT_RANGE.fullmatch(value or "")
    if not match:
        return None
    start, end, total = (int(part) for part in match.groups())
    return (start, end, total) if start <= end < total else None


def _validate_range_response(upstream: _UpstreamResponse, requested: tuple[int | None, int | None] | None) -> None:
    response = upstream.response
    host = urlsplit(upstream.final_url).hostname
    if not audio_content_type(response.getheader("Content-Type")):
        raise SourceFailure("audio_content_type_rejected", owner="external_server", host=host,
                            path_type=_path_type(upstream.final_url), http_status=response.status,
                            stage="http_range")
    if requested is not None:
        actual = _content_range(response.getheader("Content-Range"))
        if response.status != 206 or actual is None:
            raise SourceFailure("audio_range_not_supported", owner="external_server", host=host,
                                path_type=_path_type(upstream.final_url), stage="http_range",
                                http_status=response.status,
                                retry_after_seconds=retry_after_seconds(response.getheader("Retry-After")))
        start, end, _ = actual
        requested_start, requested_end = requested
        if requested_start is not None and start != requested_start:
            raise SourceFailure("audio_range_mismatch", owner="external_server", host=host,
                                path_type=_path_type(upstream.final_url), stage="http_range",
                                http_status=response.status)
        if requested_end is not None and end > requested_end:
            raise SourceFailure("audio_range_mismatch", owner="external_server", host=host,
                                path_type=_path_type(upstream.final_url), stage="http_range",
                                http_status=response.status)
    elif response.status not in {200, 206}:
        retry_after = retry_after_seconds(response.getheader("Retry-After"))
        code = "upstream_rate_limited" if response.status == 429 else f"upstream_http_{response.status}"
        raise SourceFailure(code, owner="external_server", host=host, http_status=response.status,
                            retry_after_seconds=retry_after)


@dataclass
class _ProxyToken:
    url: str
    expires_at: float


@dataclass(frozen=True)
class ValidatedAudioSource:
    token: str
    host: str
    path_type: str
    content_type: str


@dataclass
class OpenAudioResponse:
    status_code: int
    headers: dict[str, str]
    upstream: _UpstreamResponse
    prefix: bytes = b""

    async def body(self):
        try:
            if self.prefix:
                yield self.prefix
            while True:
                chunk = await asyncio.wait_for(asyncio.to_thread(self.upstream.response.read, 65536), timeout=12)
                if not chunk:
                    break
                yield chunk
        finally:
            await asyncio.to_thread(self.upstream.close)


class HitmosStreamProxy:
    """Only accepts opaque tokens minted after a Hitmos resolver result."""

    def __init__(self, *, ttl_seconds: int = TOKEN_TTL_SECONDS, max_tokens: int = MAX_TOKENS,
                 proxy_url: str | None = None):
        self.ttl_seconds = ttl_seconds
        self.max_tokens = max_tokens
        self._tokens: OrderedDict[str, _ProxyToken] = OrderedDict()
        self._lock = asyncio.Lock()
        self._retry_until = 0.0
        self.proxy_url = configured_hitmos_proxy() if proxy_url is None else proxy_url

    async def probe_and_register(self, url: str) -> ValidatedAudioSource:
        if time.monotonic() < self._retry_until:
            wait = max(1, int(self._retry_until - time.monotonic()))
            raise SourceFailure("upstream_rate_limited", owner="external_server", retry_after_seconds=wait)
        if not validated_audio_url(url):
            raise SourceFailure("audio_url_rejected", owner="local_policy")
        try:
            upstream = await _open_checked(url, f"bytes=0-{MAX_PROBE_BYTES - 1}", self.proxy_url)
        except SourceFailure as exc:
            if exc.http_status == 429:
                delay = exc.retry_after_seconds if exc.retry_after_seconds is not None else 60
                self._retry_until = time.monotonic() + delay
            raise
        try:
            _validate_range_response(upstream, (0, MAX_PROBE_BYTES - 1))
            length = upstream.response.getheader("Content-Length")
            if length is not None and (not length.isdigit() or int(length) > MAX_PROBE_BYTES):
                raise SourceFailure("audio_probe_too_large", owner="external_server",
                                    host=urlsplit(upstream.final_url).hostname,
                                    path_type=_path_type(upstream.final_url), stage="http_range",
                                    http_status=upstream.response.status)
            sample = await asyncio.wait_for(
                asyncio.to_thread(upstream.response.read, MAX_PROBE_BYTES + 1), timeout=8)
            if not sample or len(sample) > MAX_PROBE_BYTES or not audio_signature(sample):
                raise SourceFailure("audio_signature_rejected", owner="external_server",
                                    host=urlsplit(upstream.final_url).hostname,
                                    path_type=_path_type(upstream.final_url), stage="http_range",
                                    http_status=upstream.response.status)
            final_url = upstream.final_url
            token = secrets.token_urlsafe(32)
            async with self._lock:
                now = time.monotonic()
                for expired in [key for key, item in self._tokens.items() if item.expires_at <= now]:
                    del self._tokens[expired]
                self._tokens[token] = _ProxyToken(final_url, now + self.ttl_seconds)
                self._tokens.move_to_end(token)
                while len(self._tokens) > self.max_tokens:
                    self._tokens.popitem(last=False)
            return ValidatedAudioSource(token=token, host=diagnostic_host(final_url),
                path_type=_path_type(final_url),
                content_type="audio" if (upstream.response.getheader("Content-Type", "").lower().startswith("audio/")) else "octet_stream")
        except SourceFailure as exc:
            if exc.http_status == 429:
                self._retry_until = time.monotonic() + (exc.retry_after_seconds if exc.retry_after_seconds is not None else 60)
            raise
        finally:
            upstream.close()

    async def open(self, token: str, range_header: str | None) -> OpenAudioResponse:
        range_value, requested = parse_range_header(range_header)
        async with self._lock:
            item = self._tokens.get(token)
            if item is None or item.expires_at <= time.monotonic():
                self._tokens.pop(token, None)
                raise SourceFailure("audio_token_expired", owner="local_policy")
            self._tokens.move_to_end(token)
            url = item.url
        if time.monotonic() < self._retry_until:
            raise SourceFailure("upstream_rate_limited", owner="external_server",
                                retry_after_seconds=max(1, int(self._retry_until - time.monotonic())))
        try:
            upstream = await _open_checked(url, range_value, self.proxy_url)
        except SourceFailure as exc:
            if exc.http_status == 429:
                delay = exc.retry_after_seconds if exc.retry_after_seconds is not None else 60
                self._retry_until = time.monotonic() + delay
            raise
        try:
            _validate_range_response(upstream, requested)
            if upstream.response.status == 429:
                delay = retry_after_seconds(upstream.response.getheader("Retry-After"))
                self._retry_until = time.monotonic() + (delay if delay is not None else 60)
                raise SourceFailure("upstream_rate_limited", owner="external_server",
                                    host=urlsplit(upstream.final_url).hostname, http_status=429,
                                    retry_after_seconds=delay)
            prefix = b""
            if requested is None or requested[0] == 0:
                prefix = await asyncio.wait_for(
                    asyncio.to_thread(upstream.response.read, MAX_PROBE_BYTES), timeout=8)
                if not audio_signature(prefix):
                    raise SourceFailure("audio_signature_rejected", owner="external_server",
                                        host=urlsplit(upstream.final_url).hostname,
                                        http_status=upstream.response.status)
            headers = {
                "Accept-Ranges": upstream.response.getheader("Accept-Ranges", "bytes"),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            }
            for name in ("Content-Type", "Content-Length", "Content-Range"):
                value = upstream.response.getheader(name)
                if value and "\r" not in value and "\n" not in value:
                    headers[name] = value
            return OpenAudioResponse(upstream.response.status, headers, upstream, prefix)
        except SourceFailure:
            upstream.close()
            raise
        except BaseException:
            upstream.close()
            raise
