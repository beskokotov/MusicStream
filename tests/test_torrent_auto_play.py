"""Automatic Play against an original loopback swarm; no public torrents."""

import asyncio
import time
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from musicstream.torrent.auto import (
    ApprovedProwlarrSource, AutoTorrentPlayback, OwnedTorrentSource, router as auto_router,
)
from musicstream.torrent.http import TorrentPlaybackRegistry, router as range_router
from musicstream.torrent.metadata import TorrentFile, TorrentMetadata, TorrentSettings, inspect_torrent
from musicstream.torrent.metadata_resolver import MetadataResolution
from musicstream.torrent.prowlarr_discovery import MusicRelease
from musicstream.torrent.session import TorrentSessionManager
from test_torrent_session import local_swarm


def app_for_swarm(tmp_path, torrent_path, port, *, timeout=3):
    source = OwnedTorrentSource(torrent_path)
    manager = TorrentSessionManager(settings=TorrentSettings(tmp_path / "cache", 128 * 1024),
                                    max_sessions=1, max_connections=2)
    registry = TorrentPlaybackRegistry(manager, max_streams=1, block_size=1024,
                                       idle_seconds=0.5, read_timeout_seconds=timeout)
    service = AutoTorrentPlayback(source, source, registry, peers=(("127.0.0.1", port),),
                                  first_bytes=1024)

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
    return app, service, registry, manager


def wait_status(client, job_id, wanted, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        response = client.get(f"/api/experimental/playback/prepare/{job_id}")
        if response.json()["status"] in wanted:
            return response.json()
        time.sleep(0.025)
    raise AssertionError("Preparation did not reach a final state")


def track(title="selected"):
    return {"id": "local-deezer-shape", "artist": "Test Artist", "album": "album",
            "title": title, "duration_seconds": 120}


def test_play_pause_seek_switch_and_private_response(local_swarm, tmp_path):
    torrent_path, selected, port = local_swarm
    app, service, registry, manager = app_for_swarm(tmp_path, torrent_path, port)
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        assert client.get("/api/experimental/playback/capabilities").json() == {"available": True}
        initial = client.post("/api/experimental/playback/prepare", json=track())
        assert initial.status_code == 200 and initial.json()["status"] == "preparing"
        ready = wait_status(client, initial.json()["job_id"], {"ready", "unavailable", "error"})
        assert ready["status"] == "ready"
        assert set(ready) == {"status", "stream_url"}
        assert "magnet" not in str(ready) and "apikey" not in str(ready)
        url = ready["stream_url"]
        first = client.get(url, headers={"Range": "bytes=0-999"})
        assert first.status_code == 206 and first.content == selected[:1000]
        transfer = next(iter(registry._tracks.values())).transfer
        assert transfer is not None and manager.active_sessions == 1
        # Pause is a browser media state: no request while paused, same lease on resume.
        time.sleep(0.05)
        middle = client.get(url, headers={"Range": "bytes=20000-20999"})
        assert middle.status_code == 206 and middle.content == selected[20000:21000]
        assert next(iter(registry._tracks.values())).transfer is transfer
        replacement = client.post("/api/experimental/playback/prepare", json=track("not present"))
        assert replacement.status_code == 200
        unavailable = wait_status(client, replacement.json()["job_id"], {"ready", "unavailable", "error"})
        assert unavailable["status"] == "unavailable"
        assert client.get(url, headers={"Range": "bytes=0-1"}).status_code == 404
        assert manager.active_sessions == 0
        assert client.delete(f"/api/experimental/playback/prepare/{replacement.json()['job_id']}").status_code == 204
    assert list((tmp_path / "cache").iterdir()) == []


def test_unavailable_peers_and_cancellation(local_swarm, tmp_path):
    torrent_path, _, _ = local_swarm
    app, service, registry, manager = app_for_swarm(tmp_path, torrent_path, 9, timeout=0.2)
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        initial = client.post("/api/experimental/playback/prepare", json=track()).json()
        final = wait_status(client, initial["job_id"], {"ready", "unavailable", "error"})
        assert final["status"] == "unavailable" and "stream_url" not in final
        assert manager.active_sessions == 0
        another = client.post("/api/experimental/playback/prepare", json=track()).json()
        assert client.delete(f"/api/experimental/playback/prepare/{another['job_id']}").status_code == 204
        assert client.get(f"/api/experimental/playback/prepare/{another['job_id']}").status_code == 404
        assert manager.active_sessions == 0
    assert list((tmp_path / "cache").iterdir()) == []


@pytest.mark.parametrize("first_status", ["missing", "error", "weak", "bad_torrent", "read_timeout"])
def test_candidate_fallback_and_no_remote_client(local_swarm, tmp_path, first_status):
    torrent_path, _, port = local_swarm
    app, service, registry, manager = app_for_swarm(tmp_path, torrent_path, port)
    first = MusicRelease("owned_local", "missing", "Test Artist - album MP3 tracks",
                         ("Audio/MP3",), 1000, 10, 0, "Owned")
    second = MusicRelease("owned_local", "owned:local", "Test Artist - album MP3 tracks",
                          ("Audio/MP3",), 1000, 1, 0, "Owned")

    class Search:
        async def search(self, artist, album, *, limit=20):
            return first, second

    class Resolver:
        async def resolve(self, release):
            if release == first:
                if first_status == "error":
                    return MetadataResolution("unavailable")
                if first_status == "read_timeout":
                    return MetadataResolution("ok", inspect_torrent(torrent_path), torrent_path.read_bytes())
                if first_status == "bad_torrent":
                    return MetadataResolution("ok", inspect_torrent(torrent_path), b"invalid torrent")
                name = "other.mp3" if first_status == "missing" else "selected live.mp3"
                wrong = TorrentMetadata("album", (TorrentFile(0, name, "album/" + name, 100, ".mp3"),))
                return MetadataResolution("ok", wrong, torrent_path.read_bytes())
            return await OwnedTorrentSource(torrent_path).resolve(second)

    service.search = Search()
    service.resolver = Resolver()
    if first_status == "read_timeout":
        original_read = registry.read
        calls = 0

        async def fail_first_read(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError("no verified bytes")
            return await original_read(*args, **kwargs)

        registry.read = fail_first_read
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        started = client.post("/api/experimental/playback/prepare", json=track()).json()
        assert wait_status(client, started["job_id"], {"ready", "unavailable", "error"})["status"] == "ready"
    with TestClient(app, client=("192.0.2.1", 12345)) as remote:
        assert remote.get("/api/experimental/playback/capabilities").status_code == 404
        assert remote.post("/api/experimental/playback/prepare", json=track()).status_code == 404


def test_provider_error_and_early_cancellation(local_swarm, tmp_path):
    torrent_path, _, port = local_swarm
    app, service, registry, manager = app_for_swarm(tmp_path, torrent_path, port)

    class BrokenSearch:
        async def search(self, artist, album, *, limit=20):
            raise RuntimeError("source offline")

    service.search = BrokenSearch()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        request_id = "12345678-1234-1234-1234-123456789abc"
        assert client.delete(f"/api/experimental/playback/prepare/{request_id}").status_code == 204
        cancelled = client.post("/api/experimental/playback/prepare",
                                json={**track(), "request_id": request_id}).json()
        assert cancelled == {"job_id": request_id, "status": "unavailable"}
        started = client.post("/api/experimental/playback/prepare", json=track()).json()
        assert wait_status(client, started["job_id"], {"ready", "unavailable", "error"})["status"] == "error"
        assert manager.active_sessions == 0


def test_prowlarr_approval_filters_unverified_releases():
    good = MusicRelease("prowlarr", "1:approved", "Test Artist - album MP3",
                        ("Audio/MP3",), 1000, 1, 1, "Local")
    other = MusicRelease("prowlarr", "1:other", "Test Artist - album MP3",
                         ("Audio/MP3",), 1000, 20, 1, "Local")

    class Provider:
        async def search(self, artist, album, *, limit=20):
            return other, good

    async def run():
        source = ApprovedProwlarrSource(Provider(), frozenset({good.source_id}))
        assert await source.search("Test Artist", "album") == (good,)

    asyncio.run(run())
