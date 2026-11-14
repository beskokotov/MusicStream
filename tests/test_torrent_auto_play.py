"""Automatic Play against an original loopback swarm; no public torrents."""

import asyncio
import time
from contextlib import asynccontextmanager

import pytest
import libtorrent as lt
from fastapi import FastAPI
from fastapi.testclient import TestClient

from musicstream.torrent.auto import (
    ApprovedProwlarrSource, AutoTorrentPlayback, OwnedTorrentSource, router as auto_router,
)
from musicstream.torrent.http import TorrentPlaybackRegistry, router as range_router
from musicstream.torrent.metadata import TorrentFile, TorrentMetadata, TorrentSettings, inspect_torrent
from musicstream.torrent.metadata_resolver import MetadataResolution
from musicstream.torrent.prowlarr_discovery import MusicRelease, release_fingerprint
from musicstream.torrent.session import TorrentSessionManager
from musicstream.torrent.track_matcher import TrackReference
from musicstream.providers.base import Track
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
        fingerprinted = ApprovedProwlarrSource(Provider(), frozenset({release_fingerprint(good)}))
        assert await fingerprinted.search("Test Artist", "album") == (good,)
        assert fingerprinted.last_search_total == 2

    asyncio.run(run())


def test_catalog_search_covers_albums_and_singles_without_duplicate_releases():
    album = MusicRelease("prowlarr", "1:album", "Other Artist - Blue Album MP3",
                         ("Audio/MP3",), 1000, 2, 1, "NoNaMe Club")
    single = MusicRelease("prowlarr", "1:single", "Other Artist - Blue Song FLAC",
                          ("Audio/FLAC",), 1000, 3, 1, "NoNaMe Club")

    class Provider:
        def __init__(self):
            self.queries = []

        async def search(self, artist, album, *, limit=20):
            self.queries.append((artist, album, limit))
            return (album_release, single) if album == "Blue Album" else (single,)

    album_release = album

    async def run():
        provider = Provider()
        source = ApprovedProwlarrSource(provider, frozenset(), allow_unlisted=True)
        found = await source.search_track(TrackReference("Other Artist", "Blue Song", "Blue Album"))
        assert found == (album, single)
        assert source.last_search_total == 2
        assert provider.queries == [("Other Artist", "Blue Album", 50),
                                    ("Other Artist", "Blue Song", 50)]
        missing_album = await source.search_track(TrackReference("Other Artist", "Blue Song"))
        assert missing_album == (single,)

        class PartialProvider:
            async def search(self, artist, album, *, limit=20):
                if album == "Blue Album":
                    raise RuntimeError("album query failed")
                return (single,)

        partial = ApprovedProwlarrSource(PartialProvider(), frozenset(), allow_unlisted=True)
        assert await partial.search_track(TrackReference("Other Artist", "Blue Song", "Blue Album")) == (single,)

    asyncio.run(run())


@pytest.mark.parametrize("primary_failure", ["none", "wrong_version", "no_verified_bytes"])
def test_indexer_priority_falls_back_only_after_primary_fails(
        local_swarm, tmp_path, primary_failure):
    torrent_path, _, port = local_swarm
    _, service, registry, manager = app_for_swarm(tmp_path, torrent_path, port)
    primary = MusicRelease("prowlarr", "19:primary",
                           "Test Artist - album Live MP3" if primary_failure == "wrong_version"
                           else "Test Artist - album MP3",
                           ("Audio/MP3",), 1000, 30, 19, "The Pirate Bay")
    backup = MusicRelease("prowlarr", "7:backup", "Test Artist - album MP3",
                          ("Audio/MP3",), 1000, 1, 7, "NoNaMe Club")

    class Provider:
        def __init__(self):
            self.searched = []

        async def search(self, artist, album, *, limit=20, indexer_ids=None):
            self.searched.append(indexer_ids)
            return (primary,) if indexer_ids == (19,) else (backup,)

    class Resolver:
        async def resolve(self, release):
            return MetadataResolution("ok", inspect_torrent(torrent_path), torrent_path.read_bytes())

    provider = Provider()
    service.search = ApprovedProwlarrSource(
        provider, frozenset(), allow_unlisted=True,
        indexer_priority=((19, "The Pirate Bay"), (7, "NoNaMe Club")))
    service.resolver = Resolver()
    service.preparation_timeout_seconds = 120
    if primary_failure == "no_verified_bytes":
        original_read = registry.read
        calls = 0

        async def fail_primary(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError("no verified audio")
            return await original_read(*args, **kwargs)

        registry.read = fail_primary

    async def run():
        try:
            started = await service.start(Track(source="deezer", **track()))
            await asyncio.wait_for(service._job.task, timeout=10)
            assert service.status(started["job_id"])["status"] == "ready"
            expected = [(19,), (19,)]
            if primary_failure != "none":
                expected += [(7,), (7,)]
            assert provider.searched == expected
            assert service._job.playback_id in registry._tracks
            assert manager.active_sessions == 1
        finally:
            await service.aclose()
            await registry.aclose()

    asyncio.run(run())


def test_unapproved_info_hash_stops_auto_before_p2p(local_swarm, tmp_path, monkeypatch):
    torrent_path, _, _ = local_swarm
    source = OwnedTorrentSource(torrent_path)
    manager = TorrentSessionManager(
        settings=TorrentSettings(tmp_path / "unapproved-cache", 65536),
        max_sessions=1, max_connections=4, network_mode="public_discovery",
        public_bind_ip="10.0.0.42", approved_public_info_hashes=frozenset({"a" * 40}),
    )
    registry = TorrentPlaybackRegistry(manager)
    service = AutoTorrentPlayback(source, source, registry, peers=())
    monkeypatch.setattr(lt, "session", lambda *args: pytest.fail("Unapproved torrent started P2P"))

    async def run():
        try:
            result = await service.start(Track(source="deezer", **track()))
            await service._job.task
            assert service.status(result["job_id"]) == {"status": "unavailable"}
            assert manager.active_sessions == 0
            assert not (tmp_path / "unapproved-cache").exists()
        finally:
            await service.aclose()
            await registry.aclose()

    asyncio.run(run())


def test_switch_revokes_stuck_http_stream_lease(local_swarm, tmp_path):
    torrent_path, _, port = local_swarm
    _, service, registry, manager = app_for_swarm(tmp_path, torrent_path, port)

    async def run():
        try:
            first = await service.start(Track(source="deezer", **track()))
            await service._job.task
            assert service.status(first["job_id"])["status"] == "ready"
            lease = await registry.acquire(service._job.playback_id)
            second = await service.start(Track(source="deezer", **track()))
            await asyncio.wait_for(service._job.task, timeout=5)
            assert service.status(second["job_id"])["status"] == "ready"
            assert lease.revoked
            assert lease.users == 0
            await registry.release(lease)
            assert registry._streams._value == 1
            with pytest.raises(IOError, match="revoked"):
                await registry.read(lease, 0, 1)
        finally:
            await service.aclose()
            await registry.aclose()
        assert manager.active_sessions == 0

    asyncio.run(run())
