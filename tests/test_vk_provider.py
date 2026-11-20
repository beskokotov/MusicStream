import asyncio

import pytest
from fastapi.testclient import TestClient

from musicstream.audio_providers.base import RecordingReference
from musicstream.audio_providers.vk import VKAudioRecording, VKAudioSearchResult
from musicstream.audio_providers.vk_browser import VKAudioProvider, VKLoginRequired
from musicstream.audio_providers.service import SourceSelectionService
from musicstream.main import app


class FakeVKReader:
    def __init__(self, recordings=(), *, login_required=False):
        self.recordings = tuple(recordings)
        self.login_required = login_required
        self.calls = 0

    async def search_in_session(self, reference):
        self.calls += 1
        if self.login_required:
            raise VKLoginRequired
        return VKAudioSearchResult(f"{reference.artist} {reference.title}", len(self.recordings), self.recordings)

    async def read_visible_results(self, reference):
        return VKAudioSearchResult(f"{reference.artist} {reference.title}", len(self.recordings), self.recordings)

    async def close(self):
        return None


def test_vk_provider_discovers_scores_and_marks_playback_unavailable():
    recording = RecordingReference(artist="Rammstein", title="Sonne", duration_seconds=272)
    provider = VKAudioProvider(FakeVKReader([
        VKAudioRecording("-2001871978_6871978", "Sonne", "Rammstein", 272),
    ]))
    result = asyncio.run(provider.find(recording))
    candidate = result.candidates[0]
    assert result.source == "vk"
    assert result.status == "playback_unavailable"
    assert candidate.id == "-2001871978_6871978"
    assert candidate.match.status == "likely"
    assert candidate.match_score == 1
    assert candidate.duration_seconds == 272
    assert candidate.method == "vk_unavailable"
    assert candidate.playback_status == "playback_unavailable"
    assert candidate.playback_reason is None
    assert candidate.external_url == "https://vk.com/audio-2001871978_6871978"
    assert candidate.stream_url is None


@pytest.mark.parametrize(("reference", "candidate", "expected_status"), [
    (RecordingReference(artist="Rammstein", title="Sonne", duration_seconds=272),
     VKAudioRecording("-2001871978_6871978", "Sonne", "Rammstein", 272), "likely"),
    (RecordingReference(artist="The Weeknd", title="Blinding Lights", duration_seconds=200),
     VKAudioRecording("543594245_456239367", "Blinding Lights", "The Weeknd", 203), "likely"),
    (RecordingReference(artist="Кино", title="Группа крови", duration_seconds=285),
     VKAudioRecording("-2001169695_47169695", "Группа крови", "Кино", 284), "likely"),
    (RecordingReference(artist="Machine Gun Kelly", title="FIX UR FACE"),
     VKAudioRecording("378613592_456257314", "FIX UR FACE", "Machine Gun Kelly feat. Fred Durst", 199), "uncertain"),
])
def test_v2_lab_examples_render_as_v3_candidates(reference, candidate, expected_status):
    result = asyncio.run(VKAudioProvider(FakeVKReader([candidate])).find(reference))
    rendered = result.model_dump()
    assert rendered["status"] == "playback_unavailable"
    assert len(rendered["candidates"]) == 1
    item = rendered["candidates"][0]
    assert item["id"] == candidate.id
    assert item["duration_seconds"] == candidate.duration_seconds
    assert item["match"]["status"] == expected_status
    assert item["playback_status"] == "playback_unavailable"
    assert item["external_url"] == candidate.external_url


def test_vk_prepare_returns_specific_safe_fallback_without_resolving_media_url():
    reference = RecordingReference(artist="Rammstein", title="Sonne", duration_seconds=272)
    reader = FakeVKReader([VKAudioRecording("-2001871978_6871978", "Sonne", "Rammstein", 272)])
    provider = VKAudioProvider(reader)

    async def run():
        await provider.find(reference)
        return await provider.prepare(reference, "-2001871978_6871978")

    result = asyncio.run(run())
    candidate = result.candidates[0]
    assert reader.calls == 1
    assert result.status == "playback_unavailable"
    assert result.reason == "vk_external_playback_not_verified"
    assert candidate.playback_status == "playback_unavailable"
    assert candidate.playback_reason == "vk_external_playback_not_verified"
    assert candidate.stream_url is None


def test_vk_auth_failure_is_a_source_status_not_an_exception():
    recording = RecordingReference(artist="Кино", title="Группа крови", duration_seconds=285)
    provider = VKAudioProvider(FakeVKReader(login_required=True))
    result = asyncio.run(provider.find(recording))
    assert result.status == "unavailable"
    assert result.reason == "auth_required"
    assert "Войдите в VK" in result.message


def test_auto_source_selection_never_discovers_vk():
    recording = RecordingReference(artist="Rammstein", title="Sonne", duration_seconds=272)
    vk_reader = FakeVKReader([
        VKAudioRecording("-2001871978_6871978", "Sonne", "Rammstein", 272),
    ])
    providers = {"vk": VKAudioProvider(vk_reader)}
    result = asyncio.run(SourceSelectionService(providers).discover(recording))
    assert vk_reader.calls == 0
    assert result.candidates == []
    assert all(source.source != "vk" for source in result.sources)


def test_audio_lab_vk_discovery_selection_and_flag(monkeypatch):
    monkeypatch.setenv("MUSICSTREAM_VK_ENABLED", "true")
    with TestClient(app) as client:
        reader = FakeVKReader([
            VKAudioRecording("-2001871978_6871978", "Sonne", "Rammstein", 272),
        ])
        app.state.audio_providers["vk"] = VKAudioProvider(reader)
        assert client.get("/api/audio/config").json() == {"vk_enabled": True, "vk_playback_experimental": False,
                                                              "playback_test_enabled": False}
        params = {"title": "Sonne", "artist": "Rammstein", "duration_seconds": 272, "provider": "vk"}
        discovered = client.get("/api/audio/resolve", params=params)
        assert discovered.status_code == 200
        assert discovered.json()["status"] == "playback_unavailable"
        assert discovered.json()["candidates"][0]["id"] == "-2001871978_6871978"
        selected = client.get("/api/audio/resolve", params={**params, "vk_id": "-2001871978_6871978"})
        assert selected.status_code == 200
        assert selected.json()["reason"] == "vk_external_playback_not_verified"
        assert selected.json()["candidates"][0]["match"]["status"] == "likely"
        refreshed = client.get("/api/audio/resolve", params={**params, "vk_refresh": "true"})
        assert refreshed.status_code == 200
        assert refreshed.json()["candidates"][0]["id"] == "-2001871978_6871978"


def test_vk_route_is_inert_without_local_flag(monkeypatch):
    monkeypatch.delenv("MUSICSTREAM_VK_ENABLED", raising=False)
    with TestClient(app) as client:
        assert client.get("/api/audio/config").json() == {"vk_enabled": False, "vk_playback_experimental": False,
                                                              "playback_test_enabled": False}
        response = client.get("/api/audio/resolve", params={
            "title": "Sonne", "artist": "Rammstein", "provider": "vk",
        })
        assert response.status_code == 200
        assert response.json()["reason"] == "vk_disabled"
