"""Loopback-only BitTorrent checks using generated files owned by the test."""

import asyncio
import hashlib
import time

import libtorrent as lt
import pytest
from fastapi.testclient import TestClient

from musicstream.torrent import CacheLimitError, TorrentSessionManager, TorrentSettings


def bencode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(map(bencode, value)) + b"e"
    return b"d" + b"".join(bencode(key) + bencode(value[key]) for key in sorted(value)) + b"e"


@pytest.fixture
def local_swarm(tmp_path):
    source = tmp_path / "seed"
    album = source / "album"
    album.mkdir(parents=True)
    other = b"ID3" + b"A" * (20 * 1024 - 3)
    selected = b"ID3" + bytes(range(256)) * 200
    (album / "other.flac").write_bytes(other)
    (album / "selected.mp3").write_bytes(selected)
    payload = other + selected
    piece_length = 16 * 1024
    info = {
        b"name": b"album",
        b"piece length": piece_length,
        b"pieces": b"".join(hashlib.sha1(payload[i:i + piece_length]).digest()
                            for i in range(0, len(payload), piece_length)),
        b"files": [
            {b"length": len(other), b"path": [b"other.flac"]},
            {b"length": len(selected), b"path": [b"selected.mp3"]},
        ],
    }
    torrent_path = tmp_path / "local.torrent"
    torrent_path.write_bytes(bencode({b"info": info}))

    seeder = lt.session({
        "listen_interfaces": "127.0.0.1:0",
        "enable_dht": False, "enable_upnp": False,
        "enable_natpmp": False, "enable_lsd": False,
        "connections_limit": 2,
        "alert_mask": lt.alert.category_t.all_categories,
    })
    params = lt.add_torrent_params()
    params.ti = lt.torrent_info(str(torrent_path))
    params.save_path = str(source)
    params.flags |= lt.torrent_flags.seed_mode
    seed_handle = seeder.add_torrent(params)
    deadline = time.monotonic() + 5
    while seeder.listen_port() == 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert seeder.listen_port() > 0
    yield torrent_path, selected, seeder.listen_port()
    seeder.pause()
    seeder.remove_torrent(seed_handle)


def test_local_ranges_and_piece_boundary(local_swarm, tmp_path):
    torrent_path, selected, port = local_swarm
    manager = TorrentSessionManager(
        settings=TorrentSettings(tmp_path / "cache", 128 * 1024),
        max_sessions=2, max_connections=2,
    )

    async def run():
        assert manager.active_sessions == 0
        async with manager.open_torrent(torrent_path, 1, peers=(("127.0.0.1", port),)) as transfer:
            assert manager.active_sessions == 1
            assert transfer.pieces_for_range(0, 1000) == (1,)
            started = time.perf_counter()
            first = await transfer.read_range(0, 1000, timeout_seconds=8)
            first_seconds = time.perf_counter() - started
            assert first == selected[:1000]
            assert transfer._handle.have_piece(1)
            assert not transfer._handle.have_piece(0)
            middle = await transfer.read_range(20000, 23000, timeout_seconds=8)
            assert middle == selected[20000:23000]
            assert transfer._handle.have_piece(1) and transfer._handle.have_piece(2)
            assert transfer._handle.file_progress()[0] < 20 * 1024
            assert not transfer._handle.have_piece(3)
            print(f"first_range_seconds={first_seconds:.3f}")
        assert manager.active_sessions == 0
        assert list((tmp_path / "cache").iterdir()) == []

    asyncio.run(run())


def test_no_peer_timeout_and_cancel(local_swarm, tmp_path):
    torrent_path, _, _ = local_swarm
    manager = TorrentSessionManager(settings=TorrentSettings(tmp_path / "cache", 64 * 1024))

    async def run():
        async with manager.open_torrent(torrent_path, 1) as transfer:
            with pytest.raises(TimeoutError):
                await transfer.read_range(0, 1000, timeout_seconds=0.15)
            task = asyncio.create_task(transfer.read_range(0, 1000, timeout_seconds=5))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert transfer._handle.piece_priority(0) == 0
        assert manager.active_sessions == 0
        assert list((tmp_path / "cache").iterdir()) == []

    asyncio.run(run())


def test_cache_and_peer_limits(local_swarm, tmp_path):
    torrent_path, _, _ = local_swarm
    manager = TorrentSessionManager(settings=TorrentSettings(tmp_path / "cache", 16 * 1024), max_sessions=1)

    async def run():
        async with manager.open_torrent(torrent_path, 1) as transfer:
            with pytest.raises(CacheLimitError):
                await transfer.read_range(0, 20000)
        with pytest.raises(ValueError, match="loopback"):
            async with manager.open_torrent(torrent_path, 1, peers=(("8.8.8.8", 6881),)):
                pass

    asyncio.run(run())


def test_session_limit_and_fastapi_start_is_lazy(local_swarm, tmp_path, monkeypatch):
    torrent_path, _, _ = local_swarm
    manager = TorrentSessionManager(settings=TorrentSettings(tmp_path / "cache", 64 * 1024), max_sessions=1)

    async def run():
        async with manager.open_torrent(torrent_path, 1):
            second = manager.open_torrent(torrent_path, 1)
            opening = asyncio.create_task(second.__aenter__())
            await asyncio.sleep(0.05)
            assert not opening.done()
        await asyncio.wait_for(opening, 2)
        assert manager.active_sessions == 1
        await second.aclose()
        assert manager.active_sessions == 0

    asyncio.run(run())
    from musicstream.main import app
    monkeypatch.setattr(lt, "session", lambda *args, **kwargs: pytest.fail("FastAPI started a torrent session"))
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
