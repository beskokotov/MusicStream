import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from musicstream.audio_providers.base import DiscoveredRecording, DiscoveryBatch, RecordingReference
from musicstream.audio_providers.hitmos import HitmosProvider
from musicstream.audio_providers.selection import rank_batch
from musicstream.audio_providers.service import SourceSelectionService
from musicstream.audio_providers.youtube import YouTubeProvider
from musicstream.audio_providers.transport import BoundedSourceClient
from musicstream.audio_providers.base import ResolutionDiagnostics
from musicstream.audio_providers.transport import SourceFailure
from musicstream.main import app as local_app
from musicstream.public_api import app as public_app

REF = RecordingReference(title="Sonne", artist="Rammstein", duration_seconds=272)


def item(source="hitmos", **changes):
    fields = dict(id="1" if source == "hitmos" else "abcdefghijk", title="Sonne", artist="Rammstein",
                  duration_seconds=272, method="external_link" if source == "hitmos" else "youtube_iframe",
                  external_url="https://ru.hitmox.org/song/1" if source == "hitmos" else "https://www.youtube.com/watch?v=abcdefghijk")
    fields.update(changes)
    return DiscoveredRecording(**fields)


class Provider:
    def __init__(self, name, recordings=None, *, status="ok", reason=None, error=False, delay=0):
        self.name = name
        self.batch = DiscoveryBatch(source=name, status=status, reason=reason or ("candidates_found" if recordings else "no_results"),
            message="Source state", search_url="https://ru.hitmox.org/search?q=Sonne" if name == "hitmos" else "https://www.youtube.com/results?search_query=Sonne",
            recordings=recordings or [])
        self.error, self.delay = error, delay
        self.calls = 0
        self.active = 0
        self.peak = 0

    async def discover(self, recording):
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
            if self.error:
                raise RuntimeError("private-token-and-signed-url")
            return self.batch.model_copy(deep=True)
        finally:
            self.active -= 1

    async def prepare(self, recording, candidate_id):
        result = rank_batch(recording, self.batch)
        if self.name == "hitmos" and result.candidates:
            result.status = "available"
            result.candidates[0].method = "html_audio"
            result.candidates[0].stream_url = "https://cdn13.deliciousoranges.com/s1/get/music/20251120/track_1.mp3"
        return result


def test_discovery_contract_contains_no_match_or_media_address():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("No key: no network"))) as client:
            batch = await YouTubeProvider(BoundedSourceClient(client), None).discover(REF)
            assert batch.status == "blocked" and batch.reason == "api_key_missing"
        batch = await HitmosProvider().discover(REF)
        assert batch.status == "blocked"
        assert "stream_url" not in item().model_dump()
        assert "match" not in item().model_dump()
    asyncio.run(run())


def test_blocked_hitmos_can_fall_back_to_official_youtube_adapter():
    async def run():
        hitmos = Provider("hitmos", status="blocked", reason="robots_disallowed")
        youtube = Provider("youtube", [item("youtube")])
        result = await SourceSelectionService({"hitmos": hitmos, "youtube": youtube}).discover(REF, allow_youtube=True)
        assert result.source == "auto" and result.selected_source == "youtube"
        assert result.selected_id == "abcdefghijk"
        assert result.candidates[0].source == "youtube"
        assert result.sources[0].reason == "robots_disallowed"
    asyncio.run(run())


def test_no_youtube_request_or_cached_match_without_consent():
    async def run():
        hitmos = Provider("hitmos", status="blocked")
        youtube = Provider("youtube", [item("youtube")])
        service = SourceSelectionService({"hitmos": hitmos, "youtube": youtube})
        await service.prepare(REF, "youtube", "abcdefghijk")
        result = await service.discover(REF)
        assert youtube.calls == 0
        assert result.selected_source is None and result.sources[-1].reason == "consent_required"
        assert not result.reused_match
        result = await service.discover(REF, allow_youtube=True)
        assert result.reused_match and result.selected_source == "youtube"
    asyncio.run(run())


def test_candidates_from_two_sources_do_not_collapse_into_one_recording():
    async def run():
        service = SourceSelectionService({"hitmos": Provider("hitmos", [item()]), "youtube": Provider("youtube", [item("youtube")])})
        result = await service.discover(REF, allow_youtube=True)
        assert result.selection == "choose" and result.selected_id is None
        assert {c.source for c in result.candidates} == {"hitmos", "youtube"}
        assert all(c.selection_reason == "manual_confirmation_required" for c in result.candidates)
    asyncio.run(run())


@pytest.mark.parametrize("changes", [{"title": "Sonne Live"}, {"title": "Sonne Remix"}, {"title": "Sonne Radio Edit"},
                                    {"title": "Sonne Clean"}, {"title": "Sonne Explicit"}, {"duration_seconds": 400}])
def test_score_never_overrides_version_or_duration_rejection(changes):
    batch = Provider("hitmos", [item(**changes)]).batch
    result = rank_batch(REF, batch)
    assert result.candidates[0].match.status == "mismatch"
    assert result.selected_id is None


def test_external_only_youtube_cannot_be_selected_for_embedded_play():
    result = rank_batch(REF, Provider("youtube", [item("youtube", method="external_link")]).batch)
    assert result.selected_id is None
    assert result.status == "external_player"


@pytest.mark.parametrize("mode", ["error", "blocked", "empty"])
def test_failed_discovery_is_not_reported_as_track_absence(mode):
    async def run():
        provider = Provider("hitmos", error=mode == "error", status="blocked" if mode == "blocked" else "ok")
        result = await SourceSelectionService({"hitmos": provider}).discover(REF, allow_youtube=True)
        assert result.reason == ("no_results" if mode == "empty" else "sources_unavailable")
        assert "private-token" not in result.model_dump_json()
    asyncio.run(run())


def test_one_provider_error_does_not_hide_the_other_candidates_or_force_auto_selection():
    async def run():
        service = SourceSelectionService({"hitmos": Provider("hitmos", error=True), "youtube": Provider("youtube", [item("youtube")])})
        result = await service.discover(REF, allow_youtube=True)
        assert len(result.candidates) == 1
        assert result.selection == "choose"
        assert result.sources[0].reason == "provider_error"
    asyncio.run(run())


def test_provider_timeout_is_isolated_and_cancelled():
    async def run():
        slow = Provider("hitmos", delay=60)
        service = SourceSelectionService({"hitmos": slow, "youtube": Provider("youtube", [item("youtube")])})
        service.PROVIDER_TIMEOUT = .02
        result = await service.discover(REF, allow_youtube=True)
        assert result.sources[0].reason == "provider_timeout"
        assert slow.active == 0 and len(result.candidates) == 1
    asyncio.run(run())


def test_cache_coalesces_parallel_requests_expires_and_copies_results():
    async def run():
        now = [0]
        provider = Provider("hitmos", [item()], delay=.001)
        service = SourceSelectionService({"hitmos": provider}, clock=lambda: now[0])
        results = await asyncio.gather(*(service.discover(REF) for _ in range(6)))
        assert provider.calls == 1
        results[0].candidates.clear()
        assert len((await service.discover(REF)).candidates) == 1
        now[0] = 61
        await service.discover(REF)
        assert provider.calls == 2
        await service.discover(REF.model_copy(update={"explicit_lyrics": False}))
        assert provider.calls == 3
    asyncio.run(run())


def test_errors_are_negatively_cached_for_fifteen_seconds():
    async def run():
        now = [0]
        provider = Provider("hitmos", error=True)
        service = SourceSelectionService({"hitmos": provider}, clock=lambda: now[0])
        await service.discover(REF)
        await service.discover(REF)
        assert provider.calls == 1
        now[0] = 16
        await service.discover(REF)
        assert provider.calls == 2
    asyncio.run(run())


def test_each_provider_is_serial_and_total_parallelism_is_two():
    async def run():
        hitmos, youtube = Provider("hitmos", delay=.005), Provider("youtube", delay=.005)
        service = SourceSelectionService({"hitmos": hitmos, "youtube": youtube})
        await asyncio.gather(*(service.discover(REF.model_copy(update={"title": f"Song {i}"}), allow_youtube=True) for i in range(6)))
        assert hitmos.peak == youtube.peak == 1
    asyncio.run(run())


def test_remembered_mapping_skips_search_but_play_revalidates_and_never_caches_url():
    async def run():
        now = [0]
        hitmos = Provider("hitmos", [item()])
        service = SourceSelectionService({"hitmos": hitmos}, clock=lambda: now[0])
        prepared = await service.prepare(REF, "hitmos", "1")
        assert prepared.candidates[0].stream_url
        remembered = await service.discover(REF)
        assert remembered.reused_match and remembered.selected_id == "1" and hitmos.calls == 0
        assert remembered.candidates[0].stream_url is None
        assert "cdn13" not in remembered.model_dump_json()
        hitmos.batch.recordings[0].title = "Sonne Live"
        failed = await service.prepare(REF, "hitmos", "1")
        # A transport can return an address even when its metadata no longer fits.
        assert failed.candidates[0].match.status == "mismatch"
        assert not (await service.discover(REF)).reused_match
    asyncio.run(run())


def test_remembered_mapping_expires_and_all_caches_are_bounded():
    async def run():
        now = [0]
        service = SourceSelectionService({"hitmos": Provider("hitmos", [item()])}, clock=lambda: now[0])
        await service.prepare(REF, "hitmos", "1")
        now[0] = 601
        assert not (await service.discover(REF)).reused_match
        for i in range(70):
            await service.discover(REF.model_copy(update={"title": str(i)}))
        assert len(service._batches) == 64
        for i in range(70):
            reference = REF.model_copy(update={"title": str(i)})
            service.providers["hitmos"].batch.recordings = [item(title=str(i))]
            await service.prepare(reference, "hitmos", "1")
        assert len(service._matches) == 64
    asyncio.run(run())


def test_youtube_prepare_uses_official_details_without_searching_again():
    async def run():
        calls = []

        def handler(request):
            calls.append(request)
            assert request.url.path == "/youtube/v3/videos"
            assert request.url.params["id"] == "abcdefghijk"
            assert request.headers["X-Goog-Api-Key"] == "test-not-real"
            return httpx.Response(200, json={"items": [{"id": "abcdefghijk", "snippet": {"title": "Sonne", "channelTitle": "Rammstein"},
                "contentDetails": {"duration": "PT4M32S"}, "status": {"embeddable": True, "privacyStatus": "public"}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = YouTubeProvider(BoundedSourceClient(client), "test-not-real")
            result = await provider.prepare(REF, "abcdefghijk")
            assert result.status == "available" and result.candidates[0].match.status == "likely"
            assert len(calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["uncertain", "mismatch", "untrusted_url"])
def test_unconfirmed_or_untrusted_preparation_cannot_seed_reuse(failure):
    async def run():
        changes = {"duration_seconds": None} if failure == "uncertain" else {"title": "Sonne Live"} if failure == "mismatch" else {}
        provider = Provider("hitmos", [item(**changes)])
        result = await provider.prepare(REF, "1")
        if failure == "untrusted_url":
            result.candidates[0].stream_url = "https://127.0.0.1/private.mp3"
        service = SourceSelectionService({"hitmos": provider})
        assert service.remember(REF, result) is False
        assert not service._matches
    asyncio.run(run())


@pytest.mark.parametrize("app", [local_app, public_app])
def test_unified_route_and_reuse_preserve_manual_api(app, monkeypatch):
    monkeypatch.delenv("HITMOS_BROWSER_ENABLED", raising=False)
    with TestClient(app) as client:
        hitmos, youtube = Provider("hitmos", [item()]), Provider("youtube", [item("youtube")])
        app.state.audio_providers.update(hitmos=hitmos, youtube=youtube)
        params = {"title": "Sonne", "artist": "Rammstein", "duration_seconds": 272}
        result = client.get("/api/audio/resolve", params={**params, "provider": "auto"})
        assert result.status_code == 200 and result.json()["selected_source"] == "hitmos"
        assert youtube.calls == 0
        result = client.get("/api/audio/resolve", params={**params, "provider": "hitmos", "hitmos_id": "1"})
        assert result.json()["status"] == "available"
        result = client.get("/api/audio/resolve", params={**params, "provider": "auto"})
        assert result.json()["reused_match"] is True
        assert not result.json()["candidates"][0]["stream_url"]
        assert client.get("/api/audio/resolve", params={**params, "provider": "auto", "candidate_id": "1"}).status_code == 422
        assert client.get("/api/audio/resolve", params={**params, "provider": "youtube", "candidate_id": "bad"}).status_code == 422


@pytest.mark.parametrize("app,enabled", [(local_app, True), (public_app, False)])
def test_auto_discovery_diagnostics_stay_local(app, enabled, monkeypatch):
    class DeniedReader:
        async def read_search(self, query):
            raise SourceFailure("robots_disallowed", diagnostics=ResolutionDiagnostics(robots_status=200))

    monkeypatch.setenv("MUSICSTREAM_DIAGNOSTICS", "true")
    monkeypatch.delenv("HITMOS_BROWSER_ENABLED", raising=False)
    with TestClient(app) as client:
        app.state.audio_providers["hitmos"].reader = DeniedReader()
        response = client.get("/api/audio/resolve", params={"title": "Sonne", "artist": "Rammstein", "provider": "auto"})
        assert response.status_code == 200
        assert (response.json()["diagnostics"] is not None) == enabled


def test_preparation_exception_is_safe_and_forgets_an_old_mapping():
    class BrokenProvider(Provider):
        async def prepare(self, recording, candidate_id):
            raise RuntimeError("private-cookie-token")

    async def run():
        provider = Provider("hitmos", [item()])
        service = SourceSelectionService({"hitmos": provider})
        await service.prepare(REF, "hitmos", "1")
        service.providers["hitmos"] = BrokenProvider("hitmos")
        result = await service.prepare(REF, "hitmos", "1")
        assert result.reason == "provider_error"
        assert "private-cookie" not in result.model_dump_json()
        assert not service._matches
    asyncio.run(run())
