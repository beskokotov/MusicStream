import socket

import pytest

from musicstream.audio_providers.hitmos_stream import (
    audio_content_type,
    audio_signature,
    is_public_ip,
    parse_range_header,
    retry_after_seconds,
    validated_audio_url,
)
from musicstream.audio_providers.transport import SourceFailure


@pytest.mark.parametrize("url", [
    "http://cdn.example/audio.mp3",
    "https://127.0.0.1/audio.mp3",
    "https://[::1]/audio.mp3",
    "https://cdn.example:8443/audio.mp3",
    "https://user:pass@cdn.example/audio.mp3",
    "https://cdn.example/a/%2e%2e/private.mp3",
    "https://cdn.example/a%2fprivate.mp3",
    "https://cdn.example/audio.mp3#fragment",
])
def test_lab_url_lexical_policy_rejects_unsafe_urls(url):
    assert not validated_audio_url(url)


def test_lab_url_lexical_policy_accepts_public_https_hostname():
    assert validated_audio_url("https://audio.example.net/cuts/track.mp3?sig=opaque")


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.1.1", "192.168.1.2", "::1", "fc00::1"])
def test_non_public_ip_is_rejected(address):
    assert not is_public_ip(address)


def test_public_address_is_accepted():
    assert is_public_ip("1.1.1.1")


def test_retry_after_supports_seconds_and_http_date():
    assert retry_after_seconds("17", now=1000) == 17
    assert retry_after_seconds("Thu, 01 Jan 1970 00:16:57 GMT", now=1000) == 17
    assert retry_after_seconds("invalid") is None


def test_range_parser_rejects_multiple_and_malformed_ranges():
    assert parse_range_header("bytes=10-19") == ("bytes=10-19", (10, 19))
    with pytest.raises(SourceFailure):
        parse_range_header("bytes=0-1,4-5")


def test_audio_signature_and_content_type_checks():
    assert audio_signature(b"ID3" + bytes(16))
    assert audio_signature(b"\xff\xfb" + bytes(16))
    assert not audio_signature(b"<html>not audio</html>")
    assert audio_content_type("audio/mpeg")
    assert audio_content_type("application/octet-stream")
    assert not audio_content_type("text/html")


def test_dns_rejects_a_mixed_public_and_private_answer(monkeypatch):
    from musicstream.audio_providers import hitmos_stream

    records = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
    ]
    monkeypatch.setattr(hitmos_stream.socket, "getaddrinfo", lambda *args, **kwargs: records)
    with pytest.raises(SourceFailure) as error:
        hitmos_stream._resolve_public_addresses("audio.example.net")
    assert error.value.reason == "audio_dns_not_public"
    assert error.value.owner == "local_policy"


def test_dynamic_redirect_policy_is_lab_only():
    from musicstream.audio_providers.hitmos_browser import redirect_target

    headers = [{"name": "Location", "value": "https://media.example.net/audio/track.mp3?sig=x"}]
    source = "https://ru.hitmox.org/get/track"
    assert redirect_target(302, headers, source, "123") is None
    assert redirect_target(302, headers, source, "123", lab_mode=True) == headers[0]["value"]


def test_hitmos_lab_requires_explicit_environment(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from musicstream.audio_providers.routes import audio_lifespan

    async def run():
        app = SimpleNamespace(state=SimpleNamespace(local_audio_diagnostics=True))
        async with audio_lifespan(app):
            assert app.state.hitmos_lab_mode is False
            assert app.state.hitmos_stream_proxy is None

    monkeypatch.delenv("HITMOS_LAB_MODE", raising=False)
    asyncio.run(run())


def test_hitmos_lab_mode_uses_process_local_configuration(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from musicstream.audio_providers.routes import audio_lifespan

    async def run():
        app = SimpleNamespace(state=SimpleNamespace(local_audio_diagnostics=True))
        async with audio_lifespan(app):
            assert app.state.hitmos_lab_mode is True
            assert app.state.audio_limiter.limit == 18
            assert app.state.audio_providers["hitmos"].lab_mode is True

    monkeypatch.setenv("HITMOS_LAB_MODE", "true")
    monkeypatch.setenv("HITMOS_LAB_RATE_LIMIT", "18")
    asyncio.run(run())


def test_hitmos_proxy_config_requires_loopback_http_connect():
    from musicstream.audio_providers.hitmos_proxy_config import (
        HitmosProxyConfigurationError,
        _canonical_local_http_proxy,
        _select_windows_proxy,
    )

    assert _canonical_local_http_proxy("127.0.0.1:10809") == "http://127.0.0.1:10809"
    assert _canonical_local_http_proxy("http://[::1]:17321") == "http://[::1]:17321"
    assert _select_windows_proxy("http=127.0.0.1:1080;https=127.0.0.1:10809") == "127.0.0.1:10809"
    for unsafe in ("socks5://127.0.0.1:1080", "http://proxy.example:8080",
                   "http://user:secret@127.0.0.1:10809"):
        with pytest.raises(HitmosProxyConfigurationError):
            _canonical_local_http_proxy(unsafe)


def test_pinned_https_proxy_connects_to_checked_ip(monkeypatch):
    from musicstream.audio_providers.hitmos_stream import _PinnedHTTPSConnection

    class FakeSocket:
        def __init__(self):
            self.sent = b""
            self.closed = False

        def sendall(self, value):
            self.sent += value

        def recv(self, _size):
            return b"HTTP/1.1 200 Connection established\r\n\r\n"

        def close(self):
            self.closed = True

    class FakeTLS:
        def wrap_socket(self, raw, *, server_hostname):
            self.server_hostname = server_hostname
            return raw

    raw = FakeSocket()
    destinations = []
    monkeypatch.setattr("musicstream.audio_providers.hitmos_stream.socket.create_connection",
                        lambda endpoint, timeout: destinations.append(endpoint) or raw)
    connection = _PinnedHTTPSConnection("audio.example.net", "93.184.216.34",
        proxy_url="http://127.0.0.1:10809")
    tls = FakeTLS()
    connection._context = tls
    connection.connect()
    assert destinations == [("127.0.0.1", 10809)]
    assert b"CONNECT 93.184.216.34:443 HTTP/1.1" in raw.sent
    assert tls.server_hostname == "audio.example.net"


def test_proxy_connect_denial_is_not_misreported_as_cdn_status(monkeypatch):
    from musicstream.audio_providers.hitmos_stream import _PinnedHTTPSConnection

    class FakeSocket:
        def __init__(self):
            self.sent = b""

        def sendall(self, value):
            self.sent += value

        def recv(self, _size):
            return b"HTTP/1.1 502 Proxy Error\r\n\r\n"

        def close(self):
            pass

    raw = FakeSocket()
    monkeypatch.setattr("musicstream.audio_providers.hitmos_stream.socket.create_connection",
                        lambda endpoint, timeout: raw)
    connection = _PinnedHTTPSConnection("audio.example.net", "93.184.216.34",
        proxy_url="http://127.0.0.1:10809")
    with pytest.raises(SourceFailure) as error:
        connection.connect()
    assert error.value.reason == "proxy_connect_denied"
    assert error.value.owner == "network"
    assert error.value.http_status == 502
