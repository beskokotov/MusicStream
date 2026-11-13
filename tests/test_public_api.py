import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from musicstream import public_api
from musicstream.public_rate_limit import ClientRateLimiter

ORIGIN = "https://beskokotov.github.io"


@pytest.fixture
def client(monkeypatch):
    original = httpx.AsyncClient
    requests = []
    handlers = [lambda request: httpx.Response(200, json={"data": []})]

    def handle(request):
        requests.append(request)
        assert request.url.host == "api.deezer.com"
        return handlers[0](request)

    def create(**kwargs):
        return original(**kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(httpx, "AsyncClient", create)
    with TestClient(public_api.app) as api:
        yield api, requests, handlers


def test_public_search_reuses_deezer(client):
    api, requests, handlers = client
    handlers[0] = lambda request: httpx.Response(200, json={"data": [{
        "id": 123, "title": "Sonne", "artist": {"name": "Rammstein"},
        "album": {"title": "Mutter"}, "duration": 272, "explicit_lyrics": False,
    }]})
    response = api.get("/api/search", params={"q": "  Rammstein  "})
    assert response.status_code == 200
    track = response.json()[0]
    assert track["title"] == "Sonne"
    assert track["source"] == "deezer"
    assert track["explicit_lyrics"] is False
    assert track["cover_url"] is None
    assert track["duration_seconds"] == 272
    assert len(requests) == 1
    assert dict(requests[0].url.params) == {"q": "Rammstein", "limit": "10"}
    assert requests[0].extensions["timeout"]["read"] == 10.0
    assert set(public_api.app.state.search_providers) == {"deezer"}


@pytest.mark.parametrize("params", [{}, {"q": ""}, {"q": "  "}, {"q": "x" * 201},
    {"q": "song", "limit": 0}, {"q": "song", "limit": 26},
    {"q": "song", "limit": "abc"}, {"q": "song", "provider": "musicbrainz"}])
def test_invalid_search_does_not_call_upstream(client, params):
    api, requests, _ = client
    response = api.get("/api/search", params=params)
    assert response.status_code == 422
    assert set(response.json()) == {"detail"}
    assert requests == []


def test_boundary_query_and_result_limit(client):
    api, requests, handlers = client
    handlers[0] = lambda request: httpx.Response(200, json={"data": [{}] * 30})
    response = api.get("/api/search", params={"q": "x" * 200, "limit": 25})
    assert response.status_code == 200
    assert len(response.json()) == 25
    assert requests[0].url.params["limit"] == "25"


def test_route_inventory_is_restricted():
    paths = public_api.app.openapi()["paths"]
    assert set(paths) == {"/health", "/api/search", "/api/audio/resolve"}
    assert all(set(operations) == {"get"} for operations in paths.values())


@pytest.mark.parametrize("path", ["/", "/docs", "/docs/oauth2-redirect", "/redoc",
    "/openapi.json", "/app", "/static/index.html", "/static/config.js", "/health/"])
def test_private_routes_not_exposed(client, path):
    api, requests, _ = client
    assert api.get(path).status_code == 404
    assert requests == []


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete", "head"])
def test_only_get_routes(client, method):
    api, requests, _ = client
    assert getattr(api, method)("/api/search?q=song").status_code == 405
    assert requests == []


def test_cors_allowed_origin_and_preflight(client):
    api, _, _ = client
    response = api.get("/health", headers={"Origin": ORIGIN})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert "access-control-allow-credentials" not in response.headers
    preflight = api.options("/api/search", headers={
        "Origin": ORIGIN, "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "Accept",
    })
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == ORIGIN
    assert preflight.headers["access-control-allow-methods"] == "GET"


@pytest.mark.parametrize("origin", ["https://attacker.invalid", "null",
    "http://beskokotov.github.io", "https://beskokotov.github.io.evil.invalid",
    "https://beskokotov.github.io/MusicStream/"])
def test_cors_rejects_other_origins(client, origin):
    api, _, _ = client
    response = api.get("/health", headers={"Origin": origin})
    assert "access-control-allow-origin" not in response.headers
    preflight = api.options("/api/search", headers={
        "Origin": origin, "Access-Control-Request-Method": "GET",
    })
    assert preflight.status_code == 400


def test_cors_rejects_other_methods(client):
    api, _, _ = client
    assert api.options("/api/search", headers={
        "Origin": ORIGIN, "Access-Control-Request-Method": "POST",
    }).status_code == 400


def test_cors_is_not_authentication(client):
    api, _, _ = client
    assert api.get("/health").status_code == 200


def test_rate_limit_and_sliding_window(client):
    api, requests, _ = client
    now = [100.0]
    public_api.app.state.rate_limiter = ClientRateLimiter(clock=lambda: now[0])
    for _ in range(30):
        assert api.get("/health").status_code == 200
    response = api.get("/api/search?q=song", headers={"Origin": ORIGIN})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert "Retry-After" in response.headers["access-control-expose-headers"]
    assert requests == []
    now[0] += 59
    assert api.get("/health").headers["retry-after"] == "1"
    now[0] += 1
    assert api.get("/health").status_code == 200


def test_forwarded_headers_cannot_change_client_quota(client):
    api, _, _ = client
    for index in range(30):
        assert api.get("/health", headers={"X-Forwarded-For": f"198.51.100.{index}"}).status_code == 200
    assert api.get("/health", headers={
        "X-Forwarded-For": "203.0.113.99", "CF-Connecting-IP": "203.0.113.99",
        "Forwarded": "for=203.0.113.99",
    }).status_code == 429


def test_uvicorn_with_no_trusted_proxies_does_not_accept_spoofed_ip(client):
    # Integration boundary corresponding to the documented UVICORN_PROXY_HEADERS=false.
    api, _, _ = client
    with TestClient(ProxyHeadersMiddleware(public_api.app, trusted_hosts=[])) as proxy_client:
        for index in range(30):
            assert proxy_client.get("/health", headers={"X-Forwarded-For": f"203.0.113.{index}"}).status_code == 200
        assert proxy_client.get("/health", headers={"X-Forwarded-For": "203.0.113.199"}).status_code == 429


def test_clients_have_independent_quotas(client):
    with TestClient(public_api.app, client=("198.51.100.1", 1234)) as first:
        for _ in range(30):
            assert first.get("/health").status_code == 200
        assert first.get("/health").status_code == 429
        second = TestClient(public_api.app, client=("198.51.100.2", 5678))
        try:
            assert second.get("/health").status_code == 200
        finally:
            second.close()


def test_rate_limiter_memory_is_bounded_and_expired_clients_are_removed():
    now = [100.0]
    limiter = ClientRateLimiter(max_clients=2, clock=lambda: now[0])
    assert limiter.check("one") is None
    assert limiter.check("two") is None
    assert limiter.check("three") == 60
    now[0] += 60
    assert limiter.check("three") is None


@pytest.mark.parametrize(("status", "expected"), [(403, 502), (429, 503), (500, 503), (503, 503)])
def test_deezer_http_errors_are_sanitized(client, status, expected):
    api, _, handlers = client
    handlers[0] = lambda request: httpx.Response(status, text="private diagnostic value")
    response = api.get("/api/search?q=song", headers={"Origin": ORIGIN})
    assert response.status_code == expected
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert "private" not in response.text


@pytest.mark.parametrize(("error", "expected"), [(httpx.ReadTimeout, 504), (httpx.ConnectError, 502)])
def test_deezer_network_errors(client, error, expected):
    api, _, handlers = client

    def handler(request):
        raise error("private diagnostic value", request=request)

    handlers[0] = handler
    response = api.get("/api/search?q=song")
    assert response.status_code == expected
    assert "private" not in response.text


@pytest.mark.parametrize("payload", [{"error": {"message": "private"}}, {}, {"data": None}])
def test_deezer_invalid_response(client, payload):
    api, _, handlers = client
    handlers[0] = lambda request: httpx.Response(200, json=payload)
    response = api.get("/api/search?q=song")
    assert response.status_code == 502
    assert "private" not in response.text


def test_overall_search_deadline_cancels_slow_upstream(client, monkeypatch):
    api, _, handlers = client
    cancelled = []

    async def handler(request):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.append(True)

    handlers[0] = handler
    monkeypatch.setattr(public_api, "SEARCH_TIMEOUT_SECONDS", .01)
    response = api.get("/api/search?q=song")
    assert response.status_code == 504
    assert cancelled == [True]
