"""Offline TorAPI examples and an injected, network-free client."""

import asyncio
import hashlib

import httpx
import libtorrent as lt
import pytest

from musicstream.torrent import (
    TorrentSessionManager, TorrentSettings, inspect_magnet, inspect_torrent, select_audio_file,
)
from musicstream.torrent.torapi_discovery import (
    TorApiAccessDeniedError, TorApiDiscoveryProvider, TorApiResponseError,
    TorApiUnavailableError, parse_details, parse_search,
)
from test_torrent import bencode


SEARCH_EXAMPLE = [{
    "Name": '(Soundtrack, Rock) The Rookie (Daddy Cop) - 2024, MP3 (tracks)',
    "Id": "6499482", "Url": "https://rutracker.net/forum/viewtopic.php?t=6499482",
    "Size": "5.3 MB", "Category": "Саундтреки к сериалам (lossy)", "Seeds": "3",
}]


def test_saved_search_example_and_error_shape():
    hit, = parse_search(SEARCH_EXAMPLE)
    assert (hit.id, hit.category, hit.seeds, hit.size) == (
        "6499482", "Саундтреки к сериалам (lossy)", 3, "5.3 MB",
    )
    assert parse_search({"Result": "No matches were found for your title"}) == ()
    with pytest.raises(TorApiUnavailableError):
        parse_search({"Result": "Server is not available"})
    with pytest.raises(TorApiResponseError):
        parse_search([{"Result": "No data"}])


def test_saved_id_example_from_torapi_docs():
    info_hash = "3348D0A58DD8F5B251C7A361C9D235549260BA60"
    detail = parse_details("6489937", [{
        "Name": "The Rookie S06 WEB-DLRip", "Hash": info_hash,
        "Magnet": f"magnet:?xt=urn:btih:{info_hash}&tr=http%3A%2F%2Fbt.t-ru.org%2Fann",
        "Files": [{"Name": "The.Rookie.S06E01.WEB-DLRip.RGzsRutracker.avi", "Size": "644093952"}],
    }])
    assert detail is not None
    assert detail.info_hash == info_hash.lower()
    assert detail.files[0].size == "644093952"


def test_details_magnet_and_local_file_index(tmp_path):
    payload = b"ID3" + b"A" * 1000
    info = {
        b"name": b"local-album", b"piece length": 16384,
        b"pieces": hashlib.sha1(payload).digest(),
        b"files": [{b"length": len(payload), b"path": [b"song.mp3"]}],
    }
    source = tmp_path / "local.torrent"
    source.write_bytes(bencode({b"info": info}))
    info_hash = str(lt.torrent_info(str(source)).info_hash())
    magnet = f"magnet:?xt=urn:btih:{info_hash}&tr=https%3A%2F%2Ftracker.example%2Fannounce"
    # Shape follows TorAPI's published ID example. Content and hash are local test data.
    detail = parse_details("123", [{
        "Name": "local-album", "Hash": info_hash, "Magnet": magnet,
        "Files": [{"Name": "song.mp3", "Size": str(len(payload))}],
    }])
    assert detail is not None and detail.files[0].name == "song.mp3"
    assert inspect_magnet(detail.magnet).files == ()
    listed = inspect_torrent(source)
    assert select_audio_file(listed, 0).name == detail.files[0].name

    async def check_local_manager():
        manager = TorrentSessionManager(settings=TorrentSettings(tmp_path / "cache", 64 * 1024))
        async with manager.open_torrent(source, 0) as selected:
            assert selected.pieces_for_range(0, 1) == (0,)
        assert manager.active_sessions == 0

    asyncio.run(check_local_manager())


def test_prototype_transport_uses_documented_routes_only():
    seen = []

    def handler(request):
        seen.append((request.url.path, dict(request.url.params)))
        if request.url.path.endswith("/title/rutracker"):
            return httpx.Response(200, json=SEARCH_EXAMPLE)
        return httpx.Response(200, json=[{
            "Name": "Example", "Hash": "a" * 40,
            "Magnet": "magnet:?xt=urn:btih:" + "a" * 40,
            "Files": [{"Name": "song.mp3", "Size": "100"}],
        }])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = TorApiDiscoveryProvider(client)
            assert (await provider.search_title("Artist Album", category=428))[0].id == "6499482"
            assert (await provider.details("6499482")).files[0].name == "song.mp3"

    asyncio.run(run())
    assert seen == [
        ("/api/search/title/rutracker", {"query": "Artist Album", "category": "428", "page": "0"}),
        ("/api/search/id/rutracker", {"query": "6499482"}),
    ]


@pytest.mark.parametrize("kind,error", [
    ("network", TorApiUnavailableError),
    ("forbidden", TorApiAccessDeniedError),
    ("server_error", TorApiUnavailableError),
    ("source_error", TorApiUnavailableError),
    ("invalid_json", TorApiResponseError),
])
def test_search_distinguishes_failures_from_no_matches(kind, error):
    def handler(request):
        if kind == "network":
            raise httpx.ConnectError("offline")
        if kind == "forbidden":
            return httpx.Response(403)
        if kind == "server_error":
            return httpx.Response(503)
        if kind == "source_error":
            return httpx.Response(200, json={"Result": "Server is not available"})
        return httpx.Response(200, text="not json")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = TorApiDiscoveryProvider(client)
            with pytest.raises(error):
                await provider.search_title("Artist Album")

    asyncio.run(run())


def test_details_source_error_is_not_missing_torrent():
    def handler(request):
        return httpx.Response(200, json={"Result": "Server is not available"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(TorApiUnavailableError):
                await TorApiDiscoveryProvider(client).details("6499482")

    asyncio.run(run())
