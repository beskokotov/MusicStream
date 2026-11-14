"""Remote peer policy and simulated connection failures; no external traffic."""

import asyncio
import hashlib
import importlib

import libtorrent as lt
import pytest

from musicstream.torrent.metadata import TorrentSettings
from musicstream.torrent.session import TorrentSessionManager


def _own_torrent(path):
    payload = b"ID3" + b"x" * (16384 - 3)
    info = {
        b"name": b"own.mp3", b"piece length": 16384,
        b"length": len(payload), b"pieces": hashlib.sha1(payload).digest(),
    }
    path.write_bytes(lt.bencode({b"info": info}))
    return path


def test_explicit_remote_peer_policy_and_limits(tmp_path):
    local = TorrentSessionManager(settings=TorrentSettings(tmp_path / "local", 65536))
    remote = TorrentSessionManager(settings=TorrentSettings(tmp_path / "remote", 65536),
                                   max_sessions=1, max_connections=4, network_mode="remote_tcp")
    with pytest.raises(ValueError, match="loopback"):
        local.validate_peers((("10.0.0.42", 43821),))
    for endpoint in (("127.0.0.1", 43821), ("example.org", 43821),
                     ("0.0.0.0", 43821), ("10.0.0.42", 0)):
        with pytest.raises(ValueError):
            remote.validate_peers((endpoint,))
    remote.validate_peers((("10.0.0.42", 43821),))
    options = remote.session_options()
    assert options["listen_interfaces"] == ""
    assert options["enable_outgoing_tcp"] is True
    assert options["enable_outgoing_utp"] is False
    assert options["enable_dht"] is False
    assert options["enable_lsd"] is False
    assert options["enable_upnp"] is False
    assert options["enable_natpmp"] is False
    assert options["disable_hash_checks"] is False
    assert options["connections_limit"] == 4
    assert options["max_queued_disk_bytes"] <= 2 * 1024 * 1024
    assert remote.active_sessions == 0
    session = lt.session(options)
    try:
        assert session.listen_port() == 0
    finally:
        session.pause()


def test_remote_timeout_cancel_and_cleanup_without_network(tmp_path, monkeypatch):
    torrent = _own_torrent(tmp_path / "own.torrent")
    created = []

    class FakeHandle:
        def __init__(self):
            self.peers = []
            self.priorities = {}

        def set_max_connections(self, limit):
            assert limit == 4

        def prioritize_pieces(self, priorities):
            assert priorities == [0]

        def connect_peer(self, peer):
            self.peers.append(peer)

        def piece_priority(self, piece, priority):
            self.priorities[piece] = priority

        def have_piece(self, piece):
            return False

    class FakeSession:
        def __init__(self, options):
            self.options = options
            self.handle = FakeHandle()
            self.params = None
            self.closed = False
            created.append(self)

        def add_torrent(self, params):
            self.params = params
            return self.handle

        def pause(self):
            self.closed = True

        def remove_torrent(self, handle):
            assert handle is self.handle

    monkeypatch.setattr(lt, "session", FakeSession)
    manager = TorrentSessionManager(settings=TorrentSettings(tmp_path / "cache", 65536),
                                    max_sessions=1, max_connections=4, network_mode="remote_tcp")

    async def run():
        async with manager.open_torrent(torrent, 0, peers=(("10.0.0.42", 43821),)) as transfer:
            session = created[0]
            assert session.handle.peers == [("10.0.0.42", 43821)]
            assert session.params.file_priorities == [1]
            assert session.params.flags & lt.torrent_flags.disable_pex
            with pytest.raises(TimeoutError):
                await transfer.read_range(0, 512, timeout_seconds=0.05)
            task = asyncio.create_task(transfer.read_range(8192, 8704, timeout_seconds=5))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert session.handle.peers == [("10.0.0.42", 43821)]
            assert session.handle.priorities[0] == 0
        assert session.closed
        assert manager.active_sessions == 0
        assert list((tmp_path / "cache").iterdir()) == []

    asyncio.run(run())


def test_remote_fastapi_start_does_not_open_torrent(tmp_path, monkeypatch):
    main = importlib.import_module("musicstream.main")
    torrent = _own_torrent(tmp_path / "own.torrent")
    monkeypatch.setattr(main, "TORRENT_HTTP_ENABLED", True)
    monkeypatch.setattr(main, "TORRENT_AUTO_ENABLED", True)
    monkeypatch.setenv("MUSICSTREAM_TORRENT_NETWORK_MODE", "remote_tcp")
    monkeypatch.setenv("MUSICSTREAM_TORRENT_REMOTE_PEERS", '[["10.0.0.42", 43821]]')
    monkeypatch.setenv("MUSICSTREAM_TORRENT_AUTO_TEST_SOURCE", str(torrent))
    monkeypatch.setattr(lt, "session", lambda *args, **kwargs: pytest.fail("Unexpected BitTorrent start"))

    async def run():
        async with main.lifespan(main.app):
            registry = main.app.state.torrent_registry
            assert registry.manager.network_mode == "remote_tcp"
            assert registry.manager.active_sessions == 0
            assert main.app.state.torrent_auto.peers == (("10.0.0.42", 43821),)

    asyncio.run(run())
