import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from musicstream.config import Settings
from musicstream.main import app
from musicstream.providers.musicbrainz import MusicBrainzProvider, RateLimiter


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MUSICBRAINZ_CONTACT", "test@example.invalid")
    with TestClient(app) as test_client:
        provider = app.state.search_providers["musicbrainz"]
        provider.limiter = RateLimiter()
        class MusicBrainzClient:
            def get(self, url, **kwargs):
                params = dict(kwargs.pop("params", {}))
                params["provider"] = "musicbrainz"
                return test_client.get(httpx.URL(url).copy_merge_params(params), **kwargs)
        yield MusicBrainzClient(), provider


def mock_response(provider, handler):
    # Replace only transport: keep the real app's timeout and headers.
    provider.client._transport = httpx.MockTransport(handler)
    provider.client._mounts = {}


def test_search_mapping_and_request(client):
    api, provider = client

    def handler(request):
        assert request.url.path == "/ws/2/recording/"
        assert dict(request.url.params) == {"query": "Queen & Bowie", "limit": "10", "fmt": "json"}
        assert request.headers["user-agent"] == "MusicStream/0.1.0 (test@example.invalid)"
        assert request.headers["accept"] == "application/json"
        assert request.extensions["timeout"] == dict.fromkeys(["connect", "read", "write", "pool"], 10.0)
        return httpx.Response(200, json={"recordings": [{
            "id": "recording-id", "title": "Under Pressure", "length": 248123,
            "artist-credit": [{"name": "Queen", "joinphrase": " & "},
                              {"artist": {"name": "David Bowie"}}],
            "releases": [{}, {"title": "Hot Space"}],
        }, {}]})

    mock_response(provider, handler)
    response = api.get("/api/search", params={"q": "  Queen & Bowie  "})
    assert response.status_code == 200
    assert response.json() == [{
        "title": "Under Pressure", "artist": "Queen & David Bowie",
        "duration_seconds": 248.123, "album": "Hot Space",
        "musicbrainz_id": "recording-id", "source": "musicbrainz",
        "id": "recording-id", "cover_url": None, "isrc": None, "explicit_lyrics": None, "rank": None,
    }, dict.fromkeys(["id", "title", "artist", "duration_seconds", "album", "musicbrainz_id", "cover_url", "isrc", "explicit_lyrics", "rank"]) | {"source": "musicbrainz"}]


@pytest.mark.parametrize("limit", [1, 10, 25])
def test_limit_forwarded_and_enforced(client, limit):
    api, provider = client

    def handler(request):
        assert request.url.params["limit"] == str(limit)
        return httpx.Response(200, json={"recordings": [{"title": "song", "length": 0}] * 30})

    mock_response(provider, handler)
    response = api.get("/api/search", params={"q": "song", "limit": limit})
    assert response.status_code == 200
    assert len(response.json()) == limit
    assert response.json()[0]["duration_seconds"] == 0


@pytest.mark.parametrize("params", [{}, {"q": ""}, {"q": " \t "},
    {"q": "song", "limit": 0}, {"q": "song", "limit": -1},
    {"q": "song", "limit": 26}, {"q": "song", "limit": "x"},
    {"q": "song", "limit": "1.5"}])
def test_validation_does_not_call_musicbrainz(client, params):
    api, provider = client

    def handler(request):
        pytest.fail("Invalid input must not call MusicBrainz")

    mock_response(provider, handler)
    assert api.get("/api/search", params=params).status_code == 422


def test_no_results(client):
    api, provider = client
    mock_response(provider, lambda request: httpx.Response(200, json={"recordings": []}))
    assert api.get("/api/search?q=unknown").json() == []


@pytest.mark.parametrize(("upstream", "expected"), [(400, 502), (403, 502), (429, 503),
    (500, 503), (502, 503), (503, 503)])
def test_http_errors(client, upstream, expected):
    api, provider = client
    mock_response(provider, lambda request: httpx.Response(upstream, headers={"Retry-After": "3"}))
    response = api.get("/api/search?q=song")
    assert response.status_code == expected
    assert "detail" in response.json()
    if upstream == 429:
        assert response.headers["retry-after"] == "3"


@pytest.mark.parametrize("header", [None, "invalid", "-1"])
def test_retry_after_fallback(client, header):
    api, provider = client
    mock_response(provider, lambda request: httpx.Response(429, headers={"Retry-After": header} if header else {}))
    response = api.get("/api/search?q=song")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"


@pytest.mark.parametrize(("error", "expected"), [(httpx.ConnectError, 502),
    (httpx.ReadError, 502), (httpx.ConnectTimeout, 504), (httpx.ReadTimeout, 504)])
def test_network_errors(client, error, expected):
    api, provider = client

    def handler(request):
        raise error("internal error that must not leak", request=request)

    mock_response(provider, handler)
    response = api.get("/api/search?q=song")
    assert response.status_code == expected
    assert "internal error" not in response.text


@pytest.mark.parametrize("body", [b"not json", b"{}", b'{"recordings":null}',
    b'{"recordings":[{"length":-1}]}'])
def test_invalid_responses(client, body):
    api, provider = client
    mock_response(provider, lambda request: httpx.Response(200, content=body))
    assert api.get("/api/search?q=song").status_code == 502


def test_missing_contact_keeps_existing_routes(monkeypatch):
    monkeypatch.delenv("MUSICBRAINZ_CONTACT", raising=False)
    with TestClient(app) as api:
        assert api.get("/").json() == {"project": "MusicStream", "status": "running",
                                             "message": "Музыкальный сервер работает!"}
        assert api.get("/health").json() == {"status": "ok"}
        assert api.get("/api/search?q=song&provider=musicbrainz").status_code == 503
        assert api.get("/api/search?q=%20%20").status_code == 422
        assert api.get("/api/search").status_code == 422


def test_configuration(monkeypatch):
    monkeypatch.setenv("MUSICBRAINZ_CONTACT", "  https://example.invalid/contact  ")
    assert Settings.from_env().user_agent == "MusicStream/0.1.0 (https://example.invalid/contact)"
    monkeypatch.setenv("MUSICBRAINZ_CONTACT", "bad\nheader")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_default_limiter_shared():
    client = httpx.AsyncClient()
    try:
        assert MusicBrainzProvider(client).limiter is MusicBrainzProvider(client).limiter
    finally:
        asyncio.run(client.aclose())


def test_concurrent_requests_share_rate_limit(monkeypatch):
    import musicstream.providers.musicbrainz as module

    now = [100.0]
    starts = []
    real_sleep = asyncio.sleep
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])

    async def sleep(delay):
        now[0] += delay
        await real_sleep(0)

    monkeypatch.setattr(module.asyncio, "sleep", sleep)

    async def run():
        async def handler(request):
            starts.append(now[0])
            await real_sleep(0)
            return httpx.Response(200, json={"recordings": []})

        limiter = RateLimiter()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            a, b = MusicBrainzProvider(http, limiter), MusicBrainzProvider(http, limiter)
            await asyncio.gather(a.search("one"), b.search("two"), a.search("three"))

    asyncio.run(run())
    assert starts == [100.0, 101.0, 102.0]


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("date_header", [False, True])
def test_retry_after_delays_next_request(monkeypatch, status, date_header):
    import musicstream.providers.musicbrainz as module
    from musicstream.providers.base import ProviderError

    now = [100.0]
    starts = []
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])

    async def sleep(delay):
        now[0] += delay

    monkeypatch.setattr(module.asyncio, "sleep", sleep)

    async def run():
        def handler(request):
            starts.append(now[0])
            header = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=10)) if date_header else "10"
            return httpx.Response(status, headers={"Retry-After": header})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            provider = MusicBrainzProvider(http, RateLimiter())
            for _ in range(2):
                with pytest.raises(ProviderError):
                    await provider.search("song")

    asyncio.run(run())
    assert 9 <= starts[1] - starts[0] <= 10
