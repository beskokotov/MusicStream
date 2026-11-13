import httpx
import pytest
from fastapi.testclient import TestClient

from musicstream.main import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("MUSICBRAINZ_CONTACT", raising=False)
    original_client = httpx.AsyncClient
    requests = []
    handlers = [lambda request: httpx.Response(200, json={"data": []})]

    def handle(request):
        requests.append(request)
        assert request.url.host == "api.deezer.com", "Deezer must not call MusicBrainz"
        return handlers[0](request)

    def create_client(**kwargs):
        return original_client(**kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(httpx, "AsyncClient", create_client)
    with TestClient(app) as api:
        yield api, requests, handlers


@pytest.mark.parametrize("selected", [None, "deezer"])
def test_deezer_default_and_explicit_mapping(client, selected):
    api, requests, handlers = client
    handlers[0] = lambda request: httpx.Response(200, json={"data": [{
        "id": 123, "title": "Sonne", "artist": {"name": "Rammstein"},
        "album": {"title": "Mutter", "cover_xl": "https://example.invalid/cover.jpg"},
        "duration": 272, "isrc": "TEST123", "explicit_lyrics": False, "rank": 0,
    }]})
    params = {"q": "  Rammstein  "}
    if selected:
        params["provider"] = selected
    response = api.get("/api/search", params=params)
    assert response.status_code == 200
    assert response.json() == [{
        "id": "123", "title": "Sonne", "artist": "Rammstein", "album": "Mutter",
        "duration_seconds": 272.0, "musicbrainz_id": None,
        "cover_url": "https://example.invalid/cover.jpg", "isrc": "TEST123",
        "explicit_lyrics": False, "rank": 0, "source": "deezer",
    }]
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == "/search"
    assert dict(request.url.params) == {"q": "Rammstein", "limit": "10"}
    assert request.headers["user-agent"] == "MusicStream/0.1.0"
    assert request.extensions["timeout"] == dict.fromkeys(["connect", "read", "write", "pool"], 10.0)


@pytest.mark.parametrize("item", [{}, {"artist": None, "album": None, "duration": None,
    "isrc": None, "explicit_lyrics": None, "rank": None}])
def test_missing_fields_are_null(client, item):
    api, _, handlers = client
    handlers[0] = lambda request: httpx.Response(200, json={"data": [item]})
    response = api.get("/api/search?q=song")
    assert response.status_code == 200
    assert response.json() == [dict.fromkeys([
        "id", "title", "artist", "album", "duration_seconds", "musicbrainz_id",
        "cover_url", "isrc", "explicit_lyrics", "rank",
    ]) | {"source": "deezer"}]


@pytest.mark.parametrize("cover_field", ["cover", "cover_small", "cover_medium", "cover_big", "cover_xl"])
def test_cover_fallback_and_explicit_true(client, cover_field):
    api, _, handlers = client
    handlers[0] = lambda request: httpx.Response(200, json={"data": [{
        "album": {cover_field: "https://example.invalid/cover"}, "explicit_lyrics": True,
        "duration": 0,
    }]})
    result = api.get("/api/search?q=song").json()[0]
    assert result["cover_url"] == "https://example.invalid/cover"
    assert result["explicit_lyrics"] is True
    assert result["duration_seconds"] == 0


@pytest.mark.parametrize("limit", [1, 10, 25])
def test_limits(client, limit):
    api, requests, handlers = client
    handlers[0] = lambda request: httpx.Response(200, json={"data": [{}] * 30})
    response = api.get("/api/search", params={"q": "song", "limit": limit})
    assert response.status_code == 200
    assert len(response.json()) == limit
    assert requests[0].url.params["limit"] == str(limit)


@pytest.mark.parametrize("params", [{}, {"q": ""}, {"q": " \t "},
    {"q": "song", "limit": 0}, {"q": "song", "limit": 26},
    {"q": "song", "limit": "1.5"}, {"q": "song", "provider": "unknown"}])
def test_validation_without_network(client, params):
    api, requests, _ = client
    assert api.get("/api/search", params=params).status_code == 422
    assert requests == []


@pytest.mark.parametrize(("status", "expected"), [(301, 502), (400, 502), (403, 502),
    (404, 502), (429, 503), (500, 503), (503, 503)])
def test_http_errors_do_not_leak_response(client, status, expected):
    api, requests, handlers = client
    handlers[0] = lambda request: httpx.Response(status, text="private diagnostics")
    response = api.get("/api/search?q=song")
    assert response.status_code == expected
    assert set(response.json()) == {"detail"}
    assert "private diagnostics" not in response.text
    assert len(requests) == 1


@pytest.mark.parametrize(("error", "expected"), [(httpx.ConnectError, 502),
    (httpx.ReadError, 502), (httpx.ConnectTimeout, 504), (httpx.ReadTimeout, 504)])
def test_network_errors(client, error, expected):
    api, requests, handlers = client

    def handler(request):
        raise error("private exception diagnostics", request=request)

    handlers[0] = handler
    response = api.get("/api/search?q=song")
    assert response.status_code == expected
    assert "private" not in response.text
    assert len(requests) == 1


@pytest.mark.parametrize("payload", [{"error": {"type": "Exception", "message": "private diagnostics", "code": 4}},
    {}, {"data": None}, {"data": {}}, {"data": [None]}, [],
    {"data": [{"id": []}]}, {"data": [{"duration": -1}]},
    {"data": [{"explicit_lyrics": "false"}]}, {"data": [{"artist": "private diagnostics"}]}])
def test_invalid_json_structure_and_api_errors(client, payload):
    api, _, handlers = client
    handlers[0] = lambda request: httpx.Response(200, json=payload)
    response = api.get("/api/search?q=song")
    assert response.status_code == 502
    assert "private" not in response.text
    assert "validation" not in response.text.lower()


def test_invalid_json(client):
    api, _, handlers = client
    handlers[0] = lambda request: httpx.Response(200, text="private not json")
    response = api.get("/api/search?q=song")
    assert response.status_code == 502
    assert "private" not in response.text


def test_routes_and_empty_results(client):
    api, requests, _ = client
    assert api.get("/").json()["project"] == "MusicStream"
    assert api.get("/health").json() == {"status": "ok"}
    assert requests == []
    assert api.get("/api/search?q=unknown").json() == []


def test_musicbrainz_requires_contact_without_fallback(client):
    api, requests, _ = client
    assert api.get("/api/search?q=song&provider=musicbrainz").status_code == 503
    assert requests == []


def test_invalid_musicbrainz_contact_does_not_disable_deezer(client, monkeypatch):
    api, _, _ = client
    monkeypatch.setenv("MUSICBRAINZ_CONTACT", "bad\nheader")
    with TestClient(app) as other_api:
        assert other_api.get("/api/search?q=song").status_code == 200
        assert other_api.get("/api/search?q=song&provider=musicbrainz").status_code == 503
