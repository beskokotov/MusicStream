import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from musicstream.audio_providers.base import RecordingReference
from musicstream.audio_providers.hitmos import HitmosProvider
from musicstream.audio_providers.matching import assess, normalize
from musicstream.audio_providers.transport import BoundedSourceClient, SourceFailure
from musicstream.audio_providers.youtube import YouTubeProvider
from musicstream.main import app as local_app
from musicstream.public_api import app as public_app

REF = RecordingReference(title="Sonne", artist="Rammstein", duration_seconds=272)
ID = "abcdefghijk"
SECRET = "test-server-secret-not-a-real-key"


def video(*, title="Rammstein - Sonne (Official Video)", duration="PT4M32S", embeddable=True):
    return {"id": ID, "snippet": {"title": title, "channelTitle": "Rammstein Official"},
            "contentDetails": {"duration": duration},
            "status": {"embeddable": embeddable, "privacyStatus": "public"}}


def find(handler, key=SECRET):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await YouTubeProvider(BoundedSourceClient(client), key).find(REF)
    return asyncio.run(run())


def test_missing_key_returns_external_search_without_requests():
    result = find(lambda request: pytest.fail("Must not call YouTube without a key"), None)
    assert result.status == "external_player"
    assert result.reason == "api_key_missing"
    assert result.search_url.startswith("https://www.youtube.com/results?")
    assert result.candidates == []


def test_hitmos_requires_manual_source_search():
    result = asyncio.run(HitmosProvider().find(REF))
    assert result.status == "external_player"
    assert result.reason == "manual_search_required"
    assert result.candidates == []


def test_official_youtube_search_and_matching():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == "www.googleapis.com"
        assert request.headers["x-goog-api-key"] == SECRET
        assert SECRET not in str(request.url)
        if request.url.path.endswith("/search"):
            assert request.url.params["videoEmbeddable"] == "true"
            assert request.url.params["maxResults"] == "5"
            return httpx.Response(200, json={"items": [{"id": {"videoId": ID}}]})
        assert request.url.path.endswith("/videos")
        return httpx.Response(200, json={"items": [video()]})

    result = find(handler)
    assert len(calls) == 2
    assert result.status == "available"
    assert result.candidates[0].match.status == "likely"
    assert result.candidates[0].duration_seconds == 272
    assert result.candidates[0].method == "youtube_iframe"
    assert SECRET not in result.model_dump_json()


@pytest.mark.parametrize(("item", "method", "match"), [
    (video(embeddable=False), "external_link", "likely"),
    (video(title="Rammstein - Sonne Live"), "youtube_iframe", "mismatch"),
    (video(duration="PT6M"), "youtube_iframe", "mismatch"),
])
def test_embedding_and_version_restrictions(item, method, match):
    def handler(request):
        return httpx.Response(200, json={"items": [{"id": {"videoId": ID}}] if request.url.path.endswith("/search") else [item]})
    result = find(handler)
    assert result.status == "external_player"
    assert result.candidates[0].method == method
    assert result.candidates[0].match.status == match


def test_no_results():
    result = find(lambda request: httpx.Response(200, json={"items": []}))
    assert result.status == "unavailable"


@pytest.mark.parametrize("response", [
    httpx.Response(403, text="private diagnostics"), httpx.Response(429), httpx.Response(500),
    httpx.Response(302, headers={"Location": "http://127.0.0.1/private"}),
    httpx.Response(200, json={"error": {"message": "private diagnostics"}}),
    httpx.Response(200, json={}), httpx.Response(200, json={"items": [{"id": {"videoId": "invalid"}}]}),
    httpx.Response(200, text="not-json", headers={"Content-Type": "application/json"}),
    httpx.Response(200, text="<html>private</html>"),
    httpx.Response(200, content=b" " * 262145, headers={"Content-Type": "application/json"}),
])
def test_errors_redirects_and_size_limit(response):
    calls = []

    def handler(request):
        calls.append(request)
        return response

    result = find(handler)
    assert len(calls) == 1
    assert result.status == "error"
    assert "private" not in result.model_dump_json()
    assert SECRET not in result.model_dump_json()


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout])
def test_network_errors(error):
    def handler(request):
        raise error("private diagnostics", request=request)
    assert find(handler).status == "error"


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "https://169.254.169.254/",
    "https://www.googleapis.com.evil.invalid/youtube/v3/search", "https://user:pass@www.googleapis.com/youtube/v3/search",
    "https://www.googleapis.com/youtube/v3/other", "https://www.googleapis.com:8443/youtube/v3/search",
    "https://www.googleapis.com:invalid/youtube/v3/search",
    "https://www.googleapis.com/youtube/v3/search?url=http://localhost"])
def test_destination_allowlist(url):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("SSRF request"))) as client:
            with pytest.raises(SourceFailure):
                await BoundedSourceClient(client).get_json(url, params={}, api_key=SECRET)
    asyncio.run(run())


def test_outbound_request_budget():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))) as client:
            http = BoundedSourceClient(client)
            for _ in range(20):
                await http.get_json("https://www.googleapis.com/youtube/v3/search", params={}, api_key=SECRET)
            with pytest.raises(SourceFailure):
                await http.get_json("https://www.googleapis.com/youtube/v3/search", params={}, api_key=SECRET)
    asyncio.run(run())


def test_normalization_and_unknown_duration():
    assert normalize("  Beyoncé — Déjà Vu! ") == "beyonce deja vu"
    assert assess(REF, "Rammstein - Sonne", "Rammstein", None).status == "uncertain"
    assert "duration_unknown" in assess(REF, "Rammstein - Sonne", "Rammstein", None).reasons


@pytest.mark.parametrize("version", ["live", "remix", "cover", "acoustic", "slowed", "clean", "explicit"])
def test_versions_are_not_silently_ignored(version):
    assert "version_differs" in assess(REF, f"Rammstein Sonne {version}", "Rammstein", 272).reasons


@pytest.fixture(params=[local_app, public_app])
def api(request, monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    with TestClient(request.param) as client:
        yield client


def test_resolve_route_keyless(api):
    response = api.get("/api/audio/resolve", params={"title": "Sonne", "artist": "Rammstein"})
    assert response.status_code == 200
    assert response.json()["status"] == "external_player"
    assert response.json()["reason"] == "api_key_missing"


@pytest.mark.parametrize("params", [{}, {"title": "x", "artist": " "},
    {"title": "x" * 201, "artist": "a"}, {"title": "x", "artist": "a", "duration_seconds": -1},
    {"title": "x", "artist": "a", "url": "http://localhost/private"},
    {"title": "x", "artist": "a", "provider": "other"}])
def test_resolve_route_validation(api, params):
    assert api.get("/api/audio/resolve", params=params).status_code == 422


def test_resolve_rate_limit_and_cors(api):
    for _ in range(10):
        assert api.get("/api/audio/resolve?title=Sonne&artist=Rammstein").status_code == 200
    response = api.get("/api/audio/resolve?title=Sonne&artist=Rammstein", headers={"Origin": "https://beskokotov.github.io"})
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0
    if api.app is public_app:
        assert response.headers["access-control-allow-origin"] == "https://beskokotov.github.io"


def test_resolve_deadline(api):
    class SlowProvider:
        async def find(self, recording):
            raise TimeoutError()
    api.app.state.audio_providers["youtube"] = SlowProvider()
    assert api.get("/api/audio/resolve?title=Sonne&artist=Rammstein").status_code == 504
