"""Public discovery policy and counters using only synthetic torrent metadata."""

import asyncio
import hashlib
import importlib
from pathlib import Path
from types import SimpleNamespace

import libtorrent as lt
import pytest

from musicstream.torrent.metadata import TorrentSettings
from musicstream.torrent.session import TorrentSessionManager


def _torrent(path: Path, *, private: bool = False) -> tuple[str, bytes]:
    payload = b"ID3" + b"a" * (16384 - 3)
    info = {b"name": b"owned.mp3", b"piece length": 16384,
            b"length": len(payload), b"pieces": hashlib.sha1(payload).digest()}
    if private:
        info[b"private"] = 1
    path.write_bytes(lt.bencode({b"announce": b"https://tracker.example/announce", b"info": info}))
    return str(lt.load_torrent_file(str(path)).ti.info_hashes().v1), payload


def _manager(tmp_path, *hashes):
    return TorrentSessionManager(settings=TorrentSettings(tmp_path / "cache", 65536),
                                 max_sessions=1, max_connections=4,
                                 network_mode="public_discovery",
                                 public_bind_ip="10.0.0.42",
                                 approved_public_info_hashes=frozenset(hashes))


def test_public_discovery_is_explicit_and_bounded(tmp_path):
    with pytest.raises(ValueError, match="approved"):
        _manager(tmp_path)
    with pytest.raises(ValueError, match="bind address"):
        TorrentSessionManager(network_mode="public_discovery",
                              approved_public_info_hashes=frozenset({"a" * 40}))
    manager = _manager(tmp_path, "a" * 40)
    manager.validate_peers(())
    with pytest.raises(ValueError, match="does not accept"):
        manager.validate_peers((("10.0.0.42", 43821),))
    options = manager.session_options()
    assert options["listen_interfaces"] == "10.0.0.42:0"
    assert options["outgoing_interfaces"] == "10.0.0.42"
    assert options["enable_dht"] is True
    assert options["enable_lsd"] is False
    assert options["enable_upnp"] is False
    assert options["enable_natpmp"] is False
    assert options["enable_incoming_tcp"] is False
    assert options["enable_incoming_utp"] is False
    assert options["connections_limit"] == 4
    assert options["disable_hash_checks"] is False
    assert manager.active_sessions == 0


def test_private_and_unapproved_torrents_rejected_before_session(tmp_path, monkeypatch):
    private = tmp_path / "private.torrent"
    private_hash, _ = _torrent(private, private=True)
    public = tmp_path / "public.torrent"
    _torrent(public)
    monkeypatch.setattr(lt, "session", lambda *args: pytest.fail("Session started before policy check"))

    async def run():
        manager = _manager(tmp_path, private_hash)
        with pytest.raises(ValueError, match="Private"):
            async with manager.open_torrent(private, 0):
                pass
        with pytest.raises(ValueError, match="not approved"):
            async with manager.open_torrent(public, 0):
                pass
        assert manager.active_sessions == 0
        assert not (tmp_path / "cache").exists()

    asyncio.run(run())


def test_public_trackers_and_safe_diagnostics_without_network(tmp_path, monkeypatch):
    torrent = tmp_path / "owned.torrent"
    info_hash, payload = _torrent(torrent)
    created = []

    class FakeHandle:
        def __init__(self):
            self.peers = []
            self.payload = 0

        def set_max_connections(self, count):
            assert count == 4

        def prioritize_pieces(self, pieces):
            assert pieces == [0]

        def piece_priority(self, piece, priority):
            pass

        def have_piece(self, piece):
            return True

        def status(self):
            return SimpleNamespace(list_peers=7, num_peers=2, num_seeds=1,
                                   total_payload_download=self.payload)

        def get_peer_info(self):
            return self.peers

    class FakeSession:
        def __init__(self, options):
            self.options = options
            self.handle = FakeHandle()
            self.params = None
            created.append(self)

        def add_torrent(self, params):
            self.params = params
            (Path(params.save_path) / "owned.mp3").write_bytes(payload)
            return self.handle

        def pause(self):
            pass

        def pop_alerts(self):
            return []

        def status(self):
            return SimpleNamespace(dht_nodes=0)

        def listen_port(self):
            return 43821

        def remove_torrent(self, handle):
            assert handle is self.handle

    monkeypatch.setattr(lt, "session", FakeSession)
    manager = _manager(tmp_path, info_hash)

    async def run():
        async with manager.open_torrent(torrent, 0) as transfer:
            session = created[0]
            assert transfer.diagnostics().no_transfer_reason == "no_range_requested"
            transfer._requested_pieces = (0,)
            assert transfer.diagnostics().no_transfer_reason == "handshake_pending"
            session.handle.peers = [SimpleNamespace(flags=lt.peer_info.handshake, pieces=[False],
                                                   download_queue_length=0)]
            assert transfer.diagnostics().no_transfer_reason == "handshake_pending"
            assert transfer.diagnostics().handshake_pending_peers == 1
            session.handle.peers = [SimpleNamespace(flags=0, pieces=[False], download_queue_length=0)]
            assert transfer.diagnostics().no_transfer_reason == "requested_pieces_not_advertised"
            session.handle.peers = [SimpleNamespace(flags=lt.peer_info.remote_choked, pieces=[True],
                                                   download_queue_length=0)]
            assert transfer.diagnostics().no_transfer_reason == "not_interested_in_available_piece"
            session.handle.peers = [SimpleNamespace(flags=lt.peer_info.remote_choked |
                                                   lt.peer_info.interesting, pieces=[True],
                                                   download_queue_length=0)]
            assert transfer.diagnostics().no_transfer_reason == "requested_piece_peers_choking"
            assert transfer.diagnostics().interested_peers == 1
            assert transfer.diagnostics().choking_piece_peers == 1
            session.handle.peers = [SimpleNamespace(flags=lt.peer_info.interesting, pieces=[True],
                                                   download_queue_length=2)]
            assert transfer.diagnostics().no_transfer_reason == "awaiting_payload"
            assert transfer.diagnostics().queued_block_requests == 2
            assert transfer.diagnostics().sent_block_requests == 0
            session.handle.payload = len(payload)
            assert session.options["enable_dht"] is True
            assert session.params.trackers == ["https://tracker.example/announce"]
            assert not session.params.flags & lt.torrent_flags.override_trackers
            assert not session.params.flags & lt.torrent_flags.disable_dht
            assert session.params.flags & lt.torrent_flags.disable_pex
            assert session.params.file_priorities == [1]
            assert await transfer.read_range(0, 512) == payload[:512]
            counters = transfer.diagnostics()
            assert counters.discovered_peers == 7
            assert counters.connected_peers == 2
            assert counters.downloaded_payload_bytes == len(payload)
            assert counters.verified_piece_bytes == len(payload)
            assert counters.first_audio_fragment_seconds is not None
            assert counters.first_audio_fragment_seconds >= 0
            assert counters.connected_seeds == 1
            assert counters.listen_port == 43821
            assert counters.no_transfer_reason is None
            assert not hasattr(counters, "peer_addresses")
        assert manager.active_sessions == 0
        assert list((tmp_path / "cache").iterdir()) == []

    asyncio.run(run())


def test_public_fastapi_start_is_lazy(tmp_path, monkeypatch):
    main = importlib.import_module("musicstream.main")
    torrent = tmp_path / "owned.torrent"
    info_hash, _ = _torrent(torrent)
    monkeypatch.setattr(main, "TORRENT_HTTP_ENABLED", True)
    monkeypatch.setattr(main, "TORRENT_AUTO_ENABLED", True)
    monkeypatch.setenv("MUSICSTREAM_TORRENT_NETWORK_MODE", "public_discovery")
    monkeypatch.setenv("MUSICSTREAM_TORRENT_PUBLIC_APPROVED_INFO_HASHES", f'["{info_hash}"]')
    monkeypatch.setenv("MUSICSTREAM_TORRENT_PUBLIC_BIND_IP", "10.0.0.42")
    monkeypatch.setenv("MUSICSTREAM_TORRENT_AUTO_TEST_SOURCE", str(torrent))
    monkeypatch.setattr(lt, "session", lambda *args: pytest.fail("Unexpected BitTorrent start"))

    async def run():
        async with main.lifespan(main.app):
            assert main.app.state.torrent_registry.manager.network_mode == "public_discovery"
            assert main.app.state.torrent_registry.manager.active_sessions == 0
            assert main.app.state.torrent_auto.peers == ()

    asyncio.run(run())
