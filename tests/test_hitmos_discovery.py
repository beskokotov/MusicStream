import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from musicstream.audio_providers.base import RecordingReference, ResolutionDiagnostics, SourceDocument
from musicstream.audio_providers.hitmos import HitmosProvider, parse_search_ids, parse_search_metadata
from musicstream.audio_providers.hitmos_browser import HitmosBrowserReader, _host_resolves_publicly, allowed_document, allowed_search_url, can_use_lab_stream_url_directly, document_kind, redirect_target, request_action
from musicstream.audio_providers.matching import assess
from musicstream.audio_providers.service import SourceSelectionService
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
        links = "".join(f'<a href="/song/{i}">track</a>' for i in self.songs)
        metadata = []
        for song_id, song in self.songs.items():
            if song is None:
                continue
            title, artist, seconds = song
            item = {"@type": "MusicRecording", "@id": f"https://ru.hitmox.org/song/{song_id}",
                    "name": title, "byArtist": [{"name": artist}],
                    "duration": f"PT{seconds}S" if seconds is not None else "unknown"}
            metadata.append('<script type="application/ld+json">'+json.dumps(item)+'</script>')
        return SourceDocument(links + "".join(metadata))

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


def test_unique_exact_match_uses_search_metadata_without_opening_song_page():
    reader = Reader()
    result = find(reader)
    assert result.selection == "automatic" and result.selected_id == "1"
    assert result.candidates[0].selection_reason == "unique_high_confidence"
    assert result.candidates[0].stream_url is None
    assert reader.calls == [("search", "Rammstein Sonne")]
    assert result.candidates[0].match.comparison.duration_difference_seconds == 0


def test_play_resolves_only_the_selected_candidate_after_discovery():
    async def run():
        reader = Reader()
        provider = HitmosProvider(reader, diagnostics_enabled=True)
        discovered = await provider.find(REF)
        result = await provider.prepare(REF, discovered.selected_id)
        assert result.status == "available" and result.candidates[0].method == "html_audio"
        cached = await provider.prepare(REF, discovered.selected_id)
        assert cached.status == "available" and cached.reused_cache is True
        assert cached.diagnostics is None
        assert reader.calls == [("search", "Rammstein Sonne"), ("1", False)]
    asyncio.run(run())


def test_discovery_cache_marks_reuse_and_drops_stale_diagnostics():
    async def run():
        class TimedReader(Reader):
            async def read_search(self, query):
                document = await super().read_search(query)
                return SourceDocument(document.html, diagnostics=ResolutionDiagnostics(elapsed_ms=12))

        provider = HitmosProvider(TimedReader(), diagnostics_enabled=True)
        service = SourceSelectionService({"hitmos": provider})
        first = await service.discover(REF)
        second = await service.discover(REF)
        assert first.selection == second.selection == "automatic"
        assert first.reused_cache is False and first.diagnostics is not None
        assert second.reused_cache is True and second.diagnostics is None

    asyncio.run(run())


def test_missing_search_metadata_is_resolved_lazily_for_first_candidate():
    class LinkOnlyReader(Reader):
        async def read_search(self, query):
            self.calls.append(("search", query))
            return SourceDocument('<a href="/song/1">Sonne</a>')

    async def run():
        reader = LinkOnlyReader()
        provider = HitmosProvider(reader)
        discovered = await provider.find(REF)
        assert discovered.candidates[0].match.status == "uncertain"
        assert reader.calls == [("search", "Rammstein Sonne")]
        prepared = await provider.prepare(REF, discovered.candidates[0].id)
        assert prepared.status == "available"
        assert reader.calls == [("search", "Rammstein Sonne"), ("1", False)]
    asyncio.run(run())


def test_search_embedded_track_metadata_is_used_without_page_navigation():
    html = ('<a href="/song/91">Sonne</a>'
            '<div data-musid="track-id-91" data-musmeta=\'{"title":"Sonne",'
            '"artist":"Rammstein","duration_seconds":272}\'></div>')
    assert parse_search_ids(html) == ["91"]
    assert parse_search_metadata(html, ["91"]) == {"91": ("Sonne", "Rammstein", 272)}


def test_duplicate_exact_matches_are_retained_for_playback_fallback():
    result = find(Reader({"1": ("Sonne", "Rammstein", 272), "2": ("Sonne", "Rammstein", 272)}))
    assert result.selection == "automatic" and result.selected_id == "1"
    assert [candidate.id for candidate in result.candidates] == ["1", "2"]
    assert all(candidate.match.status == "likely" for candidate in result.candidates)


@pytest.mark.parametrize("version", ["Live", "Concert", "Remix", "Cover", "Acoustic",
                                    "Radio Edit", "Explicit", "Clean", "Demo", "Instrumental"])
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


def test_first_confirmed_candidate_is_auto_selected_without_refining_lower_ranked_rows():
    reader = Reader({"1": ("Sonne Live", "Rammstein", 272), "2": ("Sonne", "Rammstein", 272), "3": None})
    result = find(reader)
    assert result.selection == "automatic" and result.selected_id == "2"
    exact = next(candidate for candidate in result.candidates if candidate.id == "2")
    assert exact.match.status == "likely"
    assert reader.calls == [("search", "Rammstein Sonne")]


def test_missing_search_durations_are_enriched_and_only_exact_recording_is_auto_selected():
    class LinkOnlyReader(Reader):
        async def read_search(self, query):
            self.calls.append(("search", query))
            html = []
            for song_id, song in self.songs.items():
                html.append(f'<a href="/song/{song_id}">track</a>')
                if song is not None:
                    title, artist, _ = song
                    metadata = {"@type": "MusicRecording", "@id": f"https://ru.hitmox.org/song/{song_id}",
                                "name": title, "byArtist": [{"name": artist}], "duration": "unknown"}
                    html.append('<script type="application/ld+json">'+json.dumps(metadata)+'</script>')
            return SourceDocument("".join(html))

    async def run():
        reader = LinkOnlyReader({
            "1": ("Sonne", "Rammstein", 272),
            "2": ("Sonne Live", "Rammstein", 272),
            "3": ("Sonne Remix", "Rammstein", 272),
            "4": ("Sonne", "Rammstein, Clawfinger", 272),
        })
        provider = HitmosProvider(reader, diagnostics_enabled=True)
        result = await SourceSelectionService({"hitmos": provider}).discover(REF)
        assert result.selection == "automatic" and result.selected_id == "1"
        assert {candidate.id: candidate.duration_seconds for candidate in result.candidates} == {
            "1": 272, "2": None, "3": None, "4": None,
        }
        assert next(c for c in result.candidates if c.id == "1").match.status == "likely"
        assert next(c for c in result.candidates if c.id == "2").match.status == "mismatch"
        assert next(c for c in result.candidates if c.id == "3").match.status == "mismatch"
        assert reader.calls == [("search", "Rammstein Sonne"), ("1", False)]

    asyncio.run(run())


def test_metadata_refinement_stops_after_first_confirmed_duplicate():
    class LinkOnlyReader(Reader):
        async def read_search(self, query):
            self.calls.append(("search", query))
            metadata = ''.join(
                f'<a href="/song/{song_id}">track</a><script type="application/ld+json">'
                + json.dumps({"@type": "MusicRecording", "@id": f"https://ru.hitmox.org/song/{song_id}",
                              "name": song[0], "byArtist": [{"name": song[1]}], "duration": "unknown"})
                + '</script>' for song_id, song in self.songs.items())
            return SourceDocument(metadata)

    async def run():
        reader = LinkOnlyReader({"1": ("Sonne", "Rammstein", 272), "2": ("Sonne", "Rammstein", 272)})
        result = await HitmosProvider(reader).find(REF)
        assert result.selection == "automatic" and result.selected_id == "1"
        assert next(c for c in result.candidates if c.id == "1").match.status == "likely"
        assert next(c for c in result.candidates if c.id == "2").match.status == "uncertain"
        assert reader.calls == [("search", "Rammstein Sonne"), ("1", False)]

    asyncio.run(run())


def test_unresolved_lower_ranked_candidate_does_not_block_confirmed_winner():
    class LinkOnlyReader(Reader):
        async def read_search(self, query):
            self.calls.append(("search", query))
            html = []
            for song_id, song in self.songs.items():
                html.append(f'<a href="/song/{song_id}">track</a>')
                if song is not None:
                    title, artist, _ = song
                    metadata = {"@type": "MusicRecording", "@id": f"https://ru.hitmox.org/song/{song_id}",
                                "name": title, "byArtist": [{"name": artist}], "duration": "unknown"}
                    html.append('<script type="application/ld+json">'+json.dumps(metadata)+'</script>')
            return SourceDocument("".join(html))

    async def run():
        reader = LinkOnlyReader({
            "1": ("Sonne", "Rammstein", 272),
            "2": ("Sonne Live", "Rammstein", 272),
            "3": ("Sonne Remix", "Rammstein", 272),
            "4": None,
        })
        result = await SourceSelectionService({"hitmos": HitmosProvider(reader)}).discover(REF)
        assert result.selection == "automatic" and result.selected_id == "1"
        assert next(c for c in result.candidates if c.id == "1").match.status == "likely"
        unresolved = next(c for c in result.candidates if c.id == "4")
        assert unresolved.duration_seconds is None and unresolved.match.status == "uncertain"

    asyncio.run(run())


def test_metadata_refinement_stops_after_one_successful_candidate():
    class LinkOnlyReader(Reader):
        async def read_search(self, query):
            self.calls.append(("search", query))
            html = []
            for song_id, song in self.songs.items():
                html.append(f'<a href="/song/{song_id}">track</a>')
                if song is not None:
                    title, artist, _ = song
                    metadata = {"@type": "MusicRecording", "@id": f"https://ru.hitmox.org/song/{song_id}",
                                "name": title, "byArtist": [{"name": artist}], "duration": "unknown"}
                    html.append('<script type="application/ld+json">'+json.dumps(metadata)+'</script>')
            return SourceDocument("".join(html))

    async def run():
        reader = LinkOnlyReader({
            "1": ("Sonne", "Rammstein", 272), "2": ("Sonne", "Rammstein", 272),
            "3": ("Sonne", "Rammstein", 272), "4": ("Sonne", "Rammstein", 272),
            "5": ("Sonne", "Rammstein", 272),
        })
        result = await HitmosProvider(reader).find(REF)
        assert len([call for call in reader.calls if call[0] != "search"]) == 1
        assert result.selection == "automatic" and result.selected_id == "1"
        assert next(c for c in result.candidates if c.id == "5").duration_seconds is None

    asyncio.run(run())


def test_fast_match_checks_next_ranked_candidates_after_version_rejection_without_id_sorting():
    class OrderedReader(Reader):
        async def read_search(self, query):
            self.calls.append(("search", query))
            html = []
            for song_id, _ in self.songs.items():
                html.append(f'<a href="/song/{song_id}">Sonne</a>')
                metadata = {"@type": "MusicRecording", "@id": f"https://ru.hitmox.org/song/{song_id}",
                            "name": "Sonne", "byArtist": [{"name": "Rammstein"}], "duration": "unknown"}
                html.append('<script type="application/ld+json">'+json.dumps(metadata)+'</script>')
            return SourceDocument("".join(html))

        async def read_song(self, song_id, *, metadata_only=False):
            self.calls.append((song_id, metadata_only))
            title, artist, seconds = self.songs[song_id]
            metadata = {"@type": "MusicRecording", "@id": f"https://ru.hitmox.org/song/{song_id}",
                        "name": title, "byArtist": [{"name": artist}], "duration": f"PT{seconds}S"}
            stream = f"https://cdn13.deliciousoranges.com/s1/get/music/20251120/track_{song_id}.mp3"
            return SourceDocument('<script type="application/ld+json">'+json.dumps(metadata)+'</script>',
                                  None if metadata_only else stream)

    async def run():
        reader = OrderedReader({"99": ("Sonne Live", "Rammstein", 272),
                                "2": ("Sonne Remix", "Rammstein", 272),
                                "7": ("Sonne", "Rammstein", 272),
                                "1": ("Sonne", "Rammstein", 272)})
        provider = HitmosProvider(reader, diagnostics_enabled=True)
        batch = await provider.discover(REF)
        assert batch.auto_candidate_id == "7"
        assert [call[0] for call in reader.calls[1:]] == ["99", "2", "7"]

    asyncio.run(run())


def test_timeout_during_metadata_enrichment_keeps_candidate_unconfirmed(monkeypatch):
    class SlowReader(Reader):
        async def read_search(self, query):
            self.calls.append(("search", query))
            metadata = ''.join(
                f'<a href="/song/{song_id}">Sonne</a><script type="application/ld+json">'
                + json.dumps({"@type": "MusicRecording", "@id": f"https://ru.hitmox.org/song/{song_id}",
                              "name": "Sonne", "byArtist": [{"name": "Rammstein"}], "duration": "unknown"})
                + '</script>' for song_id in self.songs)
            return SourceDocument(metadata)

        async def read_song(self, song_id, *, metadata_only=False):
            self.calls.append((song_id, metadata_only))
            await asyncio.sleep(.03)
            return await super().read_song(song_id, metadata_only=metadata_only)

    monkeypatch.setattr("musicstream.audio_providers.hitmos.STREAM_PREPARATION_ENRICHMENT_TIMEOUT_SECONDS", .005)

    async def run():
        reader = SlowReader({"1": ("Sonne", "Rammstein", 272), "2": ("Sonne", "Rammstein", 272)})
        result = await HitmosProvider(reader).find(REF)
        assert result.selection == "choose" and result.selected_id is None
        assert all(candidate.duration_seconds is None for candidate in result.candidates)

    asyncio.run(run())


def test_empty_result_errors_and_candidates_without_search_metadata_are_distinct():
    assert find(Reader({})).reason == "no_results"
    assert find(Reader(error="source_http_error")).status == "error"
    unresolved = find(Reader({"1": None}))
    assert unresolved.reason == "ambiguous_candidates"
    assert unresolved.candidates[0].match.status == "uncertain"


def test_bounded_ttl_cache_coalesces_concurrent_discovery_and_returns_copies():
    async def run():
        reader = Reader()
        provider = HitmosProvider(reader)
        now = [0.0]
        provider._clock = lambda: now[0]
        results = await asyncio.gather(*(provider.find(REF) for _ in range(8)))
        assert len(reader.calls) == 1
        results[0].candidates.clear()
        assert len((await provider.find(REF)).candidates) == 1
        now[0] = 61
        await provider.find(REF)
        assert len(reader.calls) == 2
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
    context = SimpleNamespace(new_page=AsyncMock(return_value=page), new_cdp_session=AsyncMock(return_value=cdp), close=AsyncMock())
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock(), is_connected=Mock(return_value=True))
    runtime = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)), stop=AsyncMock())
    monkeypatch.setattr(api, "async_playwright", lambda: SimpleNamespace(start=AsyncMock(return_value=runtime)))

    async def run():
        reader = HitmosBrowserReader()
        result = await reader.read_search("Rammstein Sonne")
        await reader.read_search("Rammstein Sonne")
        assert parse_search_ids(result.html) == ["1"]
        assert visited == [SEARCH, SEARCH]
        page.evaluate.assert_not_called()
        runtime.chromium.launch.assert_awaited_once()
        assert context.close.await_count == 2
        await reader.close()
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
    context = SimpleNamespace(new_page=AsyncMock(return_value=page), new_cdp_session=AsyncMock(return_value=cdp), close=AsyncMock())
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock(), is_connected=Mock(return_value=True))
    runtime = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)), stop=AsyncMock())
    monkeypatch.setattr(api, "async_playwright", lambda: SimpleNamespace(start=AsyncMock(return_value=runtime)))

    async def run():
        reader = HitmosBrowserReader()
        with pytest.raises(SourceFailure) as error:
            await reader.read_search("Rammstein Sonne")
        assert error.value.reason == "automated_access_denied"
        assert visited == [SEARCH]
        await reader.close()
        browser.close.assert_awaited_once()
    asyncio.run(run())


def test_browser_aborted_navigation_is_network_failure_and_closes_context(monkeypatch):
    api = pytest.importorskip("playwright.async_api")
    from playwright.async_api import Error as PlaywrightError

    page = SimpleNamespace(goto=AsyncMock(side_effect=PlaywrightError("Page.goto: net::ERR_ABORTED")))
    cdp = SimpleNamespace(send=AsyncMock(return_value={"frameTree": {"frame": {"id": "main"}}}), on=Mock())
    context = SimpleNamespace(new_page=AsyncMock(return_value=page), new_cdp_session=AsyncMock(return_value=cdp), close=AsyncMock())
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock(), is_connected=Mock(return_value=True))
    runtime = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)), stop=AsyncMock())
    monkeypatch.setattr(api, "async_playwright", lambda: SimpleNamespace(start=AsyncMock(return_value=runtime)))

    async def run():
        reader = HitmosBrowserReader()
        with pytest.raises(SourceFailure) as error:
            await reader.read_search("Rammstein Sonne")
        assert error.value.reason == "source_network_error"
        context.close.assert_awaited_once()
        await reader.close()
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


def test_fetch_without_frame_id_requires_matching_network_frame_confirmation():
    song = "https://ru.hitmox.org/song/57658982"
    assert request_action(song, "Document", "GET", None, "main", "57658982", None, False,
                          network_frame_id="main") == "document"
    assert request_action(song, "Document", "GET", None, "main", "57658982", None, False) == "block"
    assert request_action(song, "Document", "GET", "subframe", "main", "57658982", None, False,
                          network_frame_id="main") == "block"
    assert request_action("https://evil.invalid/song/57658982", "Document", "GET", None, "main",
                          "57658982", None, False, network_frame_id="main") == "block"
    assert request_action(song, "Fetch", "GET", None, "main", "57658982", None, False,
                          network_frame_id="main") == "block"
    assert document_kind(song, "57658982") == "song"
    assert document_kind("https://ru.hitmox.org/robots.txt", "57658982") == "robots"
    assert document_kind("https://ru.hitmox.org/other", "57658982") == "other"


def test_lab_resolves_hitmos_source_url_redirect_before_direct_media_probe():
    song = "57658982"
    source = f"https://ru.hitmox.org/get/cuts/aa/bb/{'a' * 32}/{song}/track.mp3"
    cdn = f"https://s4.deliciouspeaches.com/get/cuts/aa/bb/{'a' * 32}/{song}/track.mp3"
    assert not can_use_lab_stream_url_directly(source)
    assert can_use_lab_stream_url_directly(cdn)
    assert redirect_target(302, [{"name": "Location", "value": cdn}], source, song,
                           lab_mode=True) == cdn


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
