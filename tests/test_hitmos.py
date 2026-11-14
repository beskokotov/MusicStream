import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from musicstream.audio_providers.base import RecordingReference
from musicstream.audio_providers.hitmos import HitmosProvider, parse_song
from musicstream.audio_providers.hitmos_browser import allowed_document
from musicstream.audio_providers.transport import SourceFailure
from musicstream.main import app as local_app
from musicstream.public_api import app as public_app

REF = RecordingReference(title="Song", artist="Artist", duration_seconds=219)
SONG_ID = "81423717"


def document(**changes):
    metadata = {"@type": "MusicRecording", "@id": f"https://ru.hitmox.org/song/{SONG_ID}",
                "name": "Song", "byArtist": [{"name": "Artist"}], "duration": "P0DT0H3M39S"}
    metadata.update(changes)
    return '<html><script type="application/ld+json">' + json.dumps(metadata) + '</script><a href="https://untrusted.invalid/media.mp3">ignored</a></html>'


class Reader:
    def __init__(self, html=None, error=None):
        self.html = html or document()
        self.error = error
        self.calls = []

    async def read_song(self, song_id):
        self.calls.append(song_id)
        if self.error:
            raise SourceFailure(self.error)
        return self.html


def test_manual_search_without_network():
    reader = Reader()
    result = asyncio.run(HitmosProvider(reader).find(REF))
    assert result.status == "external_player"
    assert result.reason == "manual_search_required"
    assert result.search_url == "https://ru.hitmox.org/search?q=Artist+Song"
    assert reader.calls == []


def test_lookup_metadata_matching_and_external_playback():
    reader = Reader()
    result = asyncio.run(HitmosProvider(reader).lookup(REF, SONG_ID))
    assert reader.calls == [SONG_ID]
    assert result.status == "external_player"
    candidate = result.candidates[0]
    assert candidate.duration_seconds == 219
    assert candidate.match.status == "likely"
    assert candidate.method == "external_link"
    assert candidate.external_url == f"https://ru.hitmox.org/song/{SONG_ID}"
    assert "media.mp3" not in result.model_dump_json()


def test_other_version_is_flagged():
    result = asyncio.run(HitmosProvider(Reader(document(name="Song Live"))).lookup(REF, SONG_ID))
    assert result.candidates[0].match.status == "mismatch"
    assert "version_differs" in result.candidates[0].match.reasons


def test_browser_not_configured_keeps_external_link():
    result = asyncio.run(HitmosProvider().lookup(REF, SONG_ID))
    assert result.reason == "browser_not_configured"
    assert result.status == "external_player"


@pytest.mark.parametrize("error", ["browser_unavailable", "request_budget_exhausted", "automated_access_denied", "source_http_error"])
def test_source_errors(error):
    result = asyncio.run(HitmosProvider(Reader(error=error)).lookup(REF, SONG_ID))
    assert result.status == "error"
    assert result.reason == error


@pytest.mark.parametrize("changes", [{"@id": "https://evil.invalid/song/81423717"},
    {"@id": "https://ru.hitmox.org/song/1"}, {"@type": "AggregateRating"},
    {"name": None}, {"byArtist": []}, {"byArtist": [{"name": 123}]}])
def test_wrong_or_missing_metadata_rejected(changes):
    with pytest.raises(SourceFailure):
        parse_song(document(**changes), SONG_ID)


def test_unknown_duration_is_null():
    assert parse_song(document(duration="unknown"), SONG_ID)[2] is None


def test_size_limit():
    result = asyncio.run(HitmosProvider(Reader(" " * 262145)).lookup(REF, SONG_ID))
    assert result.reason == "response_too_large"


@pytest.mark.parametrize("url", [f"https://ru.hitmox.org/song/{SONG_ID}"])
def test_browser_document_allowlist(url):
    assert allowed_document(url, SONG_ID)


@pytest.mark.parametrize("url", ["https://ru.hitmox.org/search?q=Song", "https://ru.hitmox.org/get/1",
    "https://ru.hitmox.org/cover/1", "https://ru.hitmox.org/song/1", "http://ru.hitmox.org/song/81423717",
    "https://127.0.0.1/song/81423717", "https://ru.hitmox.org.evil.invalid/song/81423717",
    "https://user:pass@ru.hitmox.org/song/81423717", "https://ru.hitmox.org:8443/song/81423717",
    "https://ru.hitmox.org/song/81423717?ref=1"])
def test_forbidden_browser_destinations(url):
    assert not allowed_document(url, SONG_ID)


@pytest.fixture(params=[local_app, public_app])
def api(request, monkeypatch):
    monkeypatch.delenv("HITMOS_BROWSER_ENABLED", raising=False)
    with TestClient(request.param) as client:
        request.param.state.audio_providers["hitmos"] = HitmosProvider(Reader())
        yield client


def test_hitmos_lookup_route(api):
    response = api.get("/api/audio/resolve", params={"title": "Song", "artist": "Artist", "duration_seconds":219,
        "provider": "hitmos", "hitmos_id": SONG_ID})
    assert response.status_code == 200
    assert response.json()["candidates"][0]["match"]["status"] == "likely"


@pytest.mark.parametrize("params", [{"provider": "hitmos", "hitmos_id": "http://localhost"},
    {"provider": "hitmos", "hitmos_id": "1/../../"}, {"provider": "youtube", "hitmos_id": SONG_ID},
    {"provider": "hitmos", "hitmos_id": "1" * 13}])
def test_lookup_validation(api, params):
    assert api.get("/api/audio/resolve", params={"title":"Song", "artist":"Artist", **params}).status_code == 422
