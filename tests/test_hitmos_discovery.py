import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from musicstream.audio_providers.base import RecordingReference, SourceDocument
from musicstream.audio_providers.hitmos import HitmosProvider, parse_search_ids
from musicstream.audio_providers.hitmos_browser import HitmosBrowserReader, _host_resolves_publicly, allowed_document, allowed_search_url, request_action
from musicstream.audio_providers.matching import assess
from musicstream.audio_providers.transport import SourceFailure
from musicstream.main import app as local_app
from musicstream.public_api import app as public_app

REF = RecordingReference(title="Sonne", artist="Rammstein", duration_seconds=272)
SEARCH = "https://ru.hitmox.org/search?q=Rammstein+Sonne"


class Reader:
    def __init__(self, songs=None, error=None):
        self.songs = songs if songs is not None else {"1": ("Sonne", "Rammstein", 272)}
        self.error = error
        self.calls = []

    async def read_search(self, query):
        self.calls.append(("search", query))
        await asyncio.sleep(0)
        if self.error:
            raise SourceFailure(self.error, stage="source_search")
        return SourceDocument("".join(f'<a href="/song/{i}">track</a>' for i in self.songs))

    async def read_song(self, song_id, *, metadata_only=False):
        self.calls.append((song_id, metadata_only))
        song = self.songs[song_id]
        if song is None:
            raise SourceFailure("metadata_unavailable")
        title, artist, seconds = song
        item = {"@type": "MusicRecording", "@id": f"https://ru.hitmox.org/song/{song_id}",
                "name": title, "byArtist": [{"name": artist}], "duration": f"PT{seconds}S" if seconds is not None else "unknown"}
        stream = f"https://cdn13.deliciousoranges.com/s1/get/music/20251120/track_{song_id}.mp3"
        return SourceDocument('<script type="application/ld+json">'+json.dumps(item)+'</script>',
                              None if metadata_only else stream)


def find(reader, reference=REF):
    return asyncio.run(HitmosProvider(reader).find(reference))


def test_unique_exact_match_is_prepared_without_resolving_audio():
    reader = Reader()
    result = find(reader)
    assert result.selection == "automatic" and result.selected_id == "1"
    assert result.candidates[0].selection_reason == "unique_high_confidence"
    assert result.candidates[0].stream_url is None
    assert reader.calls == [("search", "Rammstein Sonne"), ("1", True)]
    assert result.candidates[0].match.comparison.duration_difference_seconds == 0


def test_play_resolves_fresh_address_with_the_existing_lookup():
    async def run():
        reader = Reader()
        provider = HitmosProvider(reader)
        discovered = await provider.find(REF)
        result = await provider.lookup(REF, discovered.selected_id)
        assert result.status == "available" and result.candidates[0].method == "html_audio"
        assert reader.calls[-1] == ("1", False)
    asyncio.run(run())


def test_duplicate_exact_matches_choose_first_ranked_hitmos_record():
    result = find(Reader({"1": ("Sonne", "Rammstein", 272), "2": ("Sonne", "Rammstein", 272)}))
    assert result.selection == "automatic" and result.selected_id == "1"
    assert len(result.candidates) == 1
    assert result.candidates[0].selection_reason == "unique_high_confidence"


@pytest.mark.parametrize("version", ["Live", "Remix", "Radio Edit", "Explicit", "Clean"])
def test_version_is_not_hidden_by_equal_duration(version):
    result = find(Reader({"1": ("Sonne " + version, "Rammstein", 272)}))
    candidate = result.candidates[0]
    assert candidate.match.status == "mismatch"
    assert "version_differs" in candidate.match.reasons
    assert candidate.selection_reason == "recording_mismatch"
    assert result.selected_id is None


@pytest.mark.parametrize("song", [("Sonne", "Other Artist", 272), ("Sonne", "Rammstein", None),
                                  ("Sonne", "Rammstein", 290), ("Different", "Rammstein", 272),
                                  ("Rammstein Sonne", "Rammstein", 272)])
def test_incomplete_or_fuzzy_match_is_never_auto_selected(song):
    assert find(Reader({"1": song})).selected_id is None


def test_explicit_true_needs_source_confirmation_and_false_is_not_clean():
    explicit = REF.model_copy(update={"explicit_lyrics": True})
    result = find(Reader(), explicit)
    assert result.selected_id is None
    assert "explicit_unconfirmed" in result.candidates[0].match.reasons
    unflagged = REF.model_copy(update={"explicit_lyrics": False})
    assert assess(unflagged, "Sonne Clean", "Rammstein", 272).status == "mismatch"
    assert assess(explicit, "Sonne (Explicit)", "Rammstein", 272).status == "likely"
    assert assess(explicit, "Sonne (Clean)", "Rammstein", 272).status == "mismatch"


def test_partial_failure_or_truncated_search_disables_auto_choice():
    reader = Reader({"1": ("Sonne Live", "Rammstein", 272), "2": ("Sonne", "Rammstein", 272), "3": None})
    result = find(reader)
    assert result.selection == "automatic" and result.selected_id == "2"
    assert len(reader.calls) == 3  # search, rejected Live result, exact studio recording


def test_empty_result_and_source_errors_are_distinct():
    assert find(Reader({})).reason == "no_results"
    assert find(Reader(error="source_http_error")).status == "error"
    assert find(Reader({"1": None})).reason == "candidate_metadata_failed"


def test_bounded_ttl_cache_coalesces_concurrent_discovery_and_returns_copies():
    async def run():
        reader = Reader()
        provider = HitmosProvider(reader)
        now = [0.0]
        provider._clock = lambda: now[0]
        results = await asyncio.gather(*(provider.find(REF) for _ in range(8)))
        assert len(reader.calls) == 2
        results[0].candidates.clear()
        assert len((await provider.find(REF)).candidates) == 1
        now[0] = 61
        await provider.find(REF)
        assert len(reader.calls) == 4
        for i in range(70):
            await provider.find(REF.model_copy(update={"title": f"Song {i}"}))
        assert len(provider._search_cache) == 64
    asyncio.run(run())


def test_negative_cache_prevents_repeated_failed_search_requests():
    async def run():
        reader = Reader(error="source_http_error")
        provider = HitmosProvider(reader)
        await provider.find(REF)
        await provider.find(REF)
        assert len(reader.calls) == 1
    asyncio.run(run())


def test_browser_search_fetches_query_without_loading_media(monkeypatch):
    api = pytest.importorskip("playwright.async_api")
    visited = []

    async def goto(url, **kwargs):
        visited.append(url)
        return SimpleNamespace(status=200, url=url, body=AsyncMock(return_value=b'<a href="/song/1">Sonne</a>'))

    page = SimpleNamespace(goto=goto, evaluate=AsyncMock(), close=AsyncMock())
    cdp = SimpleNamespace(send=AsyncMock(return_value={"frameTree": {"frame": {"id": "main"}}}), on=Mock())
    context = SimpleNamespace(new_page=AsyncMock(return_value=page), new_cdp_session=AsyncMock(return_value=cdp))
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    runtime = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))

    @asynccontextmanager
    async def fake_runtime():
        yield runtime

    monkeypatch.setattr(api, "async_playwright", fake_runtime)

    async def run():
        reader = HitmosBrowserReader()
        result = await reader.read_search("Rammstein Sonne")
        assert parse_search_ids(result.html) == ["1"]
        assert visited == [SEARCH]
        page.evaluate.assert_not_called()
        browser.close.assert_awaited_once()
    asyncio.run(run())


def test_browser_search_reports_http_denial_without_retrying_or_challenge_bypass(monkeypatch):
    api = pytest.importorskip("playwright.async_api")
    visited = []

    async def goto(url, **kwargs):
        visited.append(url)
        return SimpleNamespace(status=403, url=url, body=AsyncMock(return_value=b"blocked"))

    page = SimpleNamespace(goto=goto, evaluate=AsyncMock(), close=AsyncMock())
    cdp = SimpleNamespace(send=AsyncMock(return_value={"frameTree": {"frame": {"id": "main"}}}), on=Mock())
    context = SimpleNamespace(new_page=AsyncMock(return_value=page), new_cdp_session=AsyncMock(return_value=cdp))
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    runtime = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))

    @asynccontextmanager
    async def fake_runtime():
        yield runtime

    monkeypatch.setattr(api, "async_playwright", fake_runtime)

    async def run():
        with pytest.raises(SourceFailure) as error:
            await HitmosBrowserReader().read_search("Rammstein Sonne")
        assert error.value.reason == "automated_access_denied"
        assert visited == [SEARCH]
        browser.close.assert_awaited_once()
    asyncio.run(run())


def test_cancellation_releases_discovery_gate():
    class SlowReader(Reader):
        async def read_search(self, query):
            await asyncio.sleep(60)

    async def run():
        provider = HitmosProvider(SlowReader())
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(.01):
                await provider.find(REF)
        provider.reader = Reader()
        async with asyncio.timeout(1):
            assert (await provider.find(REF)).selected_id == "1"
    asyncio.run(run())


def test_shared_parser_deduplicates_and_rejects_foreign_song_links():
    assert parse_search_ids('<a href="/song/1">a</a><a href="https://ru.hitmox.org/song/1">b</a>'
                           '<div data-musid="track-id-2" data-musmeta="{}"></div>'
                           '<a href="https://evil.invalid/song/3">c</a><a href="/song/4?q=x">d</a>') == ["1", "2"]
    with pytest.raises(SourceFailure):
        parse_search_ids(" " * 262145)


@pytest.mark.parametrize("url", ["https://ru.hitmox.org/search?q=x&url=http://localhost", SEARCH + "&q=x",
    "https://ru.hitmox.org/search?q=", "https://evil.invalid/search?q=x", SEARCH.replace("https:", "http:"),
    "https://user:password@ru.hitmox.org/search?q=x", "https://ru.hitmox.org:8443/search?q=x",
    "https://ru.hitmox.org/search?q=" + "x" * 402, "https://ru.hitmox.org/search?q=x%0A"])
def test_search_destinations_remain_bounded(url):
    assert not allowed_search_url(url)
    assert not allowed_document(url, "", url)


def test_search_request_gate_allows_only_the_exact_search_document():
    assert allowed_search_url(SEARCH)
    assert request_action(SEARCH, "Document", "GET", "main", "main", "", None, False, SEARCH) == "document"
    assert request_action(SEARCH, "Fetch", "GET", "main", "main", "", None, False, SEARCH) == "block"
    assert request_action(SEARCH + "x", "Document", "GET", "main", "main", "", None, False, SEARCH) == "block"
    assert request_action("https://ru.hitmox.org/unapproved", "Document", "GET", "main", "main", "", None, False) == "block"


def test_hitmos_media_hosts_must_resolve_only_to_public_addresses(monkeypatch):
    monkeypatch.setattr("musicstream.audio_providers.hitmos_browser.socket.getaddrinfo",
                        lambda *args, **kwargs: [(None, None, None, None, ("203.0.113.10", 443))])
    assert not _host_resolves_publicly("media.example")  # TEST-NET address is reserved.
    monkeypatch.setattr("musicstream.audio_providers.hitmos_browser.socket.getaddrinfo",
                        lambda *args, **kwargs: [(None, None, None, None, ("8.8.8.8", 443))])
    assert _host_resolves_publicly("media.example")
    monkeypatch.setattr("musicstream.audio_providers.hitmos_browser.socket.getaddrinfo",
                        lambda *args, **kwargs: [(None, None, None, None, ("8.8.8.8", 443)),
                                                 (None, None, None, None, ("127.0.0.1", 443))])
    assert not _host_resolves_publicly("media.example")


@pytest.mark.parametrize("app", [local_app, public_app])
def test_discovery_and_play_use_one_validated_api(app, monkeypatch):
    monkeypatch.delenv("HITMOS_BROWSER_ENABLED", raising=False)
    with TestClient(app) as client:
        app.state.audio_providers["hitmos"] = HitmosProvider(Reader())
        params = {"provider": "hitmos", "title": "Sonne", "artist": "Rammstein", "duration_seconds": 272,
                  "explicit_lyrics": False}
        result = client.get("/api/audio/resolve", params=params)
        assert result.status_code == 200 and result.json()["selected_id"] == "1"
        result = client.get("/api/audio/resolve", params={**params, "hitmos_id": "1"})
        assert result.status_code == 200 and result.json()["status"] == "available"
        assert client.get("/api/audio/resolve", params={**params, "url": "http://localhost"}).status_code == 422
