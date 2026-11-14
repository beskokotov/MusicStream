"""No network, torrent sessions or media downloads in these tests."""

import asyncio

import httpx
import pytest

from musicstream.torrent.metadata import TorrentFile, TorrentMetadata
from musicstream.torrent.prowlarr_discovery import (
    MusicRelease, ProwlarrAuthError, ProwlarrDiscoveryProvider, ProwlarrIndexerError,
    ProwlarrUnavailableError, parse_releases,
)
from musicstream.torrent.release_selection import discover_and_match, rank_releases
from musicstream.torrent.track_matcher import TrackReference, match_track
from musicstream.providers.base import Track


def file(index, path, size=1000):
    return TorrentFile(index, path.replace("\\", "/").split("/")[-1], path, size,
                       "." + path.rsplit(".", 1)[-1].lower())


def metadata(*paths):
    return TorrentMetadata("album", tuple(file(i, path) for i, path in enumerate(paths)))


def release(title, guid="release-1", seeds=5):
    return MusicRelease("prowlarr", guid, title, ("Audio/MP3",), 1000000, seeds,
                        1, "Local indexer")


def test_russian_and_foreign_tracks_use_actual_file_index():
    files = metadata("Кино - Группа крови/cover.jpg", "Кино - Группа крови/01 - Группа крови.mp3",
                     "Кино - Группа крови/02 - Закрой за мной дверь.mp3")
    result = match_track(TrackReference("Кино", "Группа крови", "Группа крови"), files)
    assert result.status == "matched" and result.file_index == 1
    assert match_track(TrackReference("Kino", "Gruppa krovi", "Группа крови"), files).file_index == 1
    assert match_track(TrackReference("Kino", "Gruppa krovi"),
                       metadata("01 - Кино - Группа крови.mp3")).file_index == 0
    foreign = metadata("Rammstein/Mutter/01 - Mein Herz Brennt.flac",
                       "Rammstein/Mutter/02 - Links 2 3 4.mp3", "Rammstein/Mutter/booklet.pdf")
    assert match_track(TrackReference("Rammstein", "Links 2 3 4", "Mutter"), foreign).file_index == 1


def test_missing_duplicate_versions_and_album_image():
    ref = TrackReference("Rammstein", "Sonne", "Mutter", 272)
    assert match_track(ref, metadata("Mutter/01 - Links.mp3")).status == "missing"
    duplicate = metadata("Mutter/03 - Sonne.mp3", "Mutter/03 - Sonne.flac")
    assert match_track(ref, duplicate).status == "ambiguous"
    versions = metadata("Mutter/03 - Sonne.mp3", "Mutter/03 - Sonne (Live).mp3")
    assert match_track(ref, versions).file_index == 0
    assert match_track(ref, metadata("Mutter.flac", "Mutter.cue", "cover.jpg")).status == "unsupported"
    assert match_track(ref, metadata("Mutter.cue", "cover.jpg", "rip.log")).status == "missing"


def test_archive_underscore_artist_prefix_and_numbered_alternate():
    files = metadata("amp021/1_piscis_nocturnal.mp3",
                     "amp021/4_piscis_nocturnal_2.mp3",
                     "amp021/cover.jpg")
    result = match_track(TrackReference("Piscis", "Nocturnal", "Nocturnal"), files)
    assert result.status == "matched" and result.file_index == 0


def test_release_ranking_requires_verified_track_and_clear_winner():
    ref = TrackReference("Rammstein", "Sonne", "Mutter")
    mp3 = release("Rammstein - Mutter (2001) MP3", "mp3", 3)
    flac = release("Rammstein - Mutter FLAC", "flac", 50)
    selection = rank_releases(ref, (flac, mp3), {
        "mp3": metadata("Mutter/03 - Sonne.mp3", "Mutter/04 - Ich Will.mp3"),
        "flac": metadata("Mutter.flac", "Mutter.cue"),
    })
    assert selection.release == mp3 and selection.file_index == 0
    assert rank_releases(ref, (mp3,), {}).release is None
    tied = rank_releases(ref, (mp3, release(mp3.title, "second", 3)), {
        "mp3": metadata("03 - Sonne.mp3"), "second": metadata("03 - Sonne.mp3")})
    assert tied.release is None and tied.reason == "ambiguous_releases"


@pytest.mark.parametrize("first_seeds,second_seeds,expected", [
    (8, 3, "first"), (0, 5, "second"),
])
def test_equal_recording_prefers_more_seeders(first_seeds, second_seeds, expected):
    ref = TrackReference("Artist", "Song", "Album")
    first = release("Artist - Album MP3", "first", first_seeds)
    second = release("Artist - Album MP3", "second", second_seeds)
    files = metadata("Artist/Album/01 - Song.mp3")
    result = rank_releases(ref, (first, second), {"first": files, "second": files})
    assert result.release.source_id == expected


def test_seeders_outweigh_other_peers_and_live_version_is_rejected():
    ref = TrackReference("Artist", "Song", "Album")
    zero = release("Artist - Album FLAC", "zero", 0)
    four = release("Artist - Album FLAC", "four", 4)
    live = release("Artist - Album Live FLAC", "live", 30)
    from dataclasses import replace
    zero = replace(zero, peers=20)
    files = metadata("Artist/Album/01 - Song.flac")
    result = rank_releases(ref, (zero, four, live),
                           {item.source_id: files for item in (zero, four, live)})
    assert result.release == four
    assert result.file_index == 0
    assert rank_releases(ref, (live,), {"live": files}).release is None
    studio = release("Artist - Album FLAC", "studio", 2)
    assert rank_releases(ref, (live, studio), {"live": files, "studio": files}).release == studio


def test_unknown_seeders_remain_eligible_and_ambiguous_track_is_rejected():
    ref = TrackReference("Artist", "Song", "Album")
    unknown = release("Artist - Album FLAC", "unknown", 0)
    from dataclasses import replace
    unknown = replace(unknown, seeders=None)
    files = metadata("Artist/Album/01 - Song.flac")
    assert rank_releases(ref, (unknown,), {"unknown": files}).release == unknown
    duplicate = metadata("Artist/Album/01 - Song.mp3", "Artist/Album/01 - Song.flac")
    assert rank_releases(ref, (unknown,), {"unknown": duplicate}).release is None


def test_parse_prowlarr_result_removes_embedded_api_key():
    row = {"title": "Кино - Группа крови MP3", "guid": "https://example.test/42",
           "protocol": "torrent", "indexerId": 1, "indexer": "NoNaMe Club",
           "size": 12345, "seeders": 7, "peers": 12,
           "categories": [{"id": 3010, "name": "Audio/MP3"}],
           "downloadUrl": "http://127.0.0.1:18746/1/download?apikey=secret&link=opaque"}
    hit, = parse_releases([row], base_url="http://127.0.0.1:18746")
    assert (hit.title, hit.seeders, hit.peers, hit.category) == (row["title"], 7, 12, ("Audio/MP3",))
    assert "secret" not in hit.metadata_url and "link=opaque" in hit.metadata_url
    assert parse_releases([], base_url="http://127.0.0.1:18746") == ()


@pytest.mark.parametrize("mode,expected", [
    ("empty", None), ("auth", ProwlarrAuthError),
    ("offline", ProwlarrUnavailableError), ("disabled", ProwlarrIndexerError),
])
def test_provider_distinguishes_empty_from_errors(mode, expected):
    def handler(request):
        assert request.headers["X-Api-Key"] == "local-test-key"
        assert "apikey" not in request.url.params
        if mode == "offline":
            raise httpx.ConnectError("offline")
        if mode == "auth":
            return httpx.Response(401)
        if request.url.path.endswith("/indexerstatus"):
            return httpx.Response(200, json=[{"indexerId": 1, "disabledTill": "2999-01-01T00:00:00Z"}]
                                  if mode == "disabled" else [])
        assert request.url.params["query"] == "Кино Группа крови"
        return httpx.Response(200, json=[])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = ProwlarrDiscoveryProvider(client, api_key="local-test-key", indexer_ids=(1,))
            if expected:
                with pytest.raises(expected):
                    await provider.search("Кино", "Группа крови")
            else:
                assert await provider.search("Кино", "Группа крови") == ()

    asyncio.run(run())


def test_priority_resolves_current_prowlarr_ids_and_filters_each_search():
    queries = []

    def handler(request):
        assert request.headers["X-Api-Key"] == "local-test-key"
        if request.url.path.endswith("/indexer"):
            return httpx.Response(200, json=[
                {"id": 19, "name": "The Pirate Bay", "enable": True, "protocol": "torrent"},
                {"id": 7, "name": "NoNaMe Club", "enable": True, "protocol": "torrent"},
            ])
        queries.append(request.url.params.get_list("indexerIds"))
        return httpx.Response(200, json=[
            {"title": "Artist - Album MP3", "guid": "one", "protocol": "torrent",
             "indexerId": 19, "indexer": "The Pirate Bay", "size": 1000,
             "categories": [{"id": 3010, "name": "Audio/MP3"}]},
            {"title": "Artist - Album MP3", "guid": "two", "protocol": "torrent",
             "indexerId": 7, "indexer": "NoNaMe Club", "size": 1000,
             "categories": [{"id": 3010, "name": "Audio/MP3"}]},
        ])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = ProwlarrDiscoveryProvider(client, api_key="local-test-key")
            priority = await provider.resolve_indexer_priority(("The Pirate Bay", "NoNaMe Club"))
            assert priority == ((19, "The Pirate Bay"), (7, "NoNaMe Club"))
            first = await provider.search("Artist", "Album", indexer_ids=(priority[0][0],))
            second = await provider.search("Artist", "Album", indexer_ids=(priority[1][0],))
            assert [item.indexer_id for item in first] == [19]
            assert [item.indexer_id for item in second] == [7]
            assert queries == [["19"], ["7"]]

    asyncio.run(run())


def test_deezer_to_prowlarr_to_synthetic_torrent_file():
    deezer = TrackReference.from_deezer(Track(
        source="deezer", artist="Кино", title="Группа крови", album="Группа крови",
        duration_seconds=286))
    rows = [{
        "title": "Кино - Группа крови (1988) MP3 tracks", "guid": "release-42",
        "protocol": "torrent", "indexerId": 1, "indexer": "Local indexer",
        "size": 9000, "seeders": 2, "categories": [{"id": 3010, "name": "Audio/MP3"}],
    }]

    def handler(request):
        assert request.url.params["query"] == "Кино Группа крови"
        return httpx.Response(200, json=rows)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = ProwlarrDiscoveryProvider(client, api_key="test")
            synthetic = metadata("Группа крови/cover.jpg", "Группа крови/01 - Группа крови.mp3",
                                 "Группа крови/02 - Закрой за мной дверь.mp3")

            async def resolve(candidate):
                assert candidate.title == rows[0]["title"]
                return synthetic

            selected = await discover_and_match(deezer, provider, resolve)
            assert selected.release.title == rows[0]["title"]
            assert selected.file_index == 1

    asyncio.run(run())


def test_verified_single_can_match_when_deezer_album_differs():
    reference = TrackReference("Other Artist", "Blue Song", "Blue Album")
    single = release("Other Artist - Blue Song FLAC", "single", 3)
    files = metadata("Other Artist/Blue Song.flac")
    selected = rank_releases(reference, (single,), {single.source_id: files})
    assert selected.release == single
    assert selected.file_index == 0


def test_verified_single_can_match_without_deezer_album():
    reference = TrackReference("Other Artist", "Blue Song")
    single = release("Other Artist - Blue Song MP3", "single", 1)
    files = metadata("Other Artist/Blue Song.mp3")
    selected = rank_releases(reference, (single,), {single.source_id: files})
    assert selected.release == single
    assert selected.file_index == 0


def test_indexer_priority_cannot_promote_wrong_edition():
    reference = TrackReference("Artist", "Song", "Album")
    deluxe = release("Artist - Album Deluxe MP3", "primary", 100)
    standard = release("Artist - Album MP3", "backup", 1)
    file_list = metadata("Artist/Album/01 - Song.mp3")
    assert rank_releases(reference, (deluxe,), {"primary": file_list}).release is None
    selected = rank_releases(reference, (deluxe, standard),
                             {"primary": file_list, "backup": file_list})
    assert selected.release == standard
