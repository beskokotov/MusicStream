"""Auto Play and local Range integration with synthetic approved torrents."""

import asyncio
import importlib
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from musicstream.torrent.auto import AutoTorrentPlayback, OwnedTorrentSource, router as auto_router
from musicstream.torrent.http import TorrentPlaybackRegistry, router as range_router
from musicstream.torrent.safe_webseed_stream import SafeWebSeedManager, SafeWebSeedTransfer
from test_safe_webseed import _torrent


def _app(path, info_hash):
    source = OwnedTorrentSource(path)
    manager = SafeWebSeedManager(approved_info_hashes=frozenset({info_hash}), archive_item="album")
    registry = TorrentPlaybackRegistry(manager, max_streams=1, block_size=1024,
                                       read_timeout_seconds=5)
    service = AutoTorrentPlayback(source, source, registry, peers=(), first_bytes=1024)

    @asynccontextmanager
    async def lifespan(app):
        app.state.torrent_registry = registry
        app.state.torrent_auto = service
        registry.start()
        try:
            yield
        finally:
            await service.aclose()
            await registry.aclose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(auto_router)
    app.include_router(range_router)
    return app, manager


def _track(title):
    return {"id": "deezer-shaped-test", "artist": "Test Artist", "album": "album",
            "title": title, "duration_seconds": 100}


def _status(client, job_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = client.get(f"/api/experimental/playback/prepare/{job_id}").json()
        if result["status"] in {"ready", "unavailable", "error"}:
            return result
        time.sleep(0.02)
    raise AssertionError("Preparation did not finish")


def test_auto_play_webseed_range_switch_and_cleanup(tmp_path, monkeypatch):
    path = tmp_path / "album.torrent"
    info_hash, (first, second) = _torrent(path)
    data = first + second
    requested = []

    async def approved_piece(self, index, timeout_seconds):
        requested.append(index)
        return data[index * 16384:(index + 1) * 16384]

    monkeypatch.setattr(SafeWebSeedTransfer, "_fetch_piece", approved_piece)
    app, manager = _app(path, info_hash)
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        started = client.post("/api/experimental/playback/prepare", json=_track("one")).json()
        ready = _status(client, started["job_id"])
        assert ready["status"] == "ready" and set(ready) == {"status", "stream_url"}
        assert "archive.org" not in str(ready) and info_hash not in str(ready)
        old_url = ready["stream_url"]
        response = client.get(old_url, headers={"Range": "bytes=0-1023"})
        assert response.status_code == 206 and response.content == first[:1024]
        assert response.headers["content-range"] == f"bytes 0-1023/{len(first)}"
        assert requested == [0]

        switched = client.post("/api/experimental/playback/prepare", json=_track("two")).json()
        next_ready = _status(client, switched["job_id"])
        assert next_ready["status"] == "ready"
        assert client.get(old_url).status_code == 404
        assert client.get(next_ready["stream_url"], headers={"Range": "bytes=0-511"}).content == second[:512]
        assert manager.active_sessions == 1
        assert client.delete(f"/api/experimental/playback/prepare/{switched['job_id']}").status_code == 204
        assert manager.active_sessions == 0
        assert client.get(next_ready["stream_url"]).status_code == 404


def test_unapproved_hash_is_unavailable_without_web_request(tmp_path, monkeypatch):
    path = tmp_path / "album.torrent"
    _, _ = _torrent(path)
    app, manager = _app(path, "0" * 40)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Unapproved Web Seed request")

    monkeypatch.setattr(SafeWebSeedTransfer, "_fetch_piece", forbidden)
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        started = client.post("/api/experimental/playback/prepare", json=_track("one")).json()
        result = _status(client, started["job_id"])
        assert result == {"status": "unavailable"}
        assert manager.active_sessions == 0


def test_switch_cancels_inflight_webseed_preparation(tmp_path, monkeypatch):
    path = tmp_path / "album.torrent"
    info_hash, (first, second) = _torrent(path)
    data = first + second
    begun = threading.Event()
    cancelled = threading.Event()
    calls = 0

    async def piece(self, index, timeout_seconds):
        nonlocal calls
        calls += 1
        if calls == 1:
            begun.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        return data[index * 16384:(index + 1) * 16384]

    monkeypatch.setattr(SafeWebSeedTransfer, "_fetch_piece", piece)
    app, manager = _app(path, info_hash)
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        first_job = client.post("/api/experimental/playback/prepare", json=_track("one")).json()
        assert begun.wait(2)
        second_job = client.post("/api/experimental/playback/prepare", json=_track("two")).json()
        assert cancelled.wait(2)
        assert _status(client, second_job["job_id"])["status"] == "ready"
        assert client.get(f"/api/experimental/playback/prepare/{first_job['job_id']}").status_code == 404
        assert manager.active_sessions == 1
    assert manager.active_sessions == 0


def test_main_lifespan_wires_approved_auto_webseed_without_starting_network(tmp_path, monkeypatch):
    main = importlib.import_module("musicstream.main")
    path = tmp_path / "album.torrent"
    info_hash, _ = _torrent(path)
    monkeypatch.setattr(main, "TORRENT_HTTP_ENABLED", True)
    monkeypatch.setattr(main, "TORRENT_AUTO_ENABLED", True)
    monkeypatch.setenv("MUSICSTREAM_TORRENT_NETWORK_MODE", "safe_webseed")
    monkeypatch.setenv("MUSICSTREAM_TORRENT_PUBLIC_APPROVED_INFO_HASHES", f'["{info_hash}"]')
    monkeypatch.setenv("MUSICSTREAM_TORRENT_AUTO_TEST_SOURCE", str(path))

    async def run():
        async with main.lifespan(main.app):
            assert main.app.state.torrent_auto.registry.manager.network_mode == "safe_webseed"
            assert main.app.state.torrent_registry.manager.active_sessions == 0

    asyncio.run(run())
