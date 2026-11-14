"""Bounded search and candidate race without touching a public swarm."""

import asyncio
import time

from musicstream.providers.base import Track
from musicstream.torrent.auto import (
    ApprovedProwlarrSource, AutoTorrentPlayback, _ReadyCandidate,
)
from musicstream.torrent.cache import TTLCache
from musicstream.torrent.http import TorrentPlaybackRegistry
from musicstream.torrent.metadata import TorrentSettings, inspect_torrent
from musicstream.torrent.metadata_resolver import MetadataResolution
from musicstream.torrent.prowlarr_discovery import MusicRelease
from musicstream.torrent.session import TorrentSessionManager
from test_torrent_session import local_swarm


def _release(indexer_id: int, indexer: str) -> MusicRelease:
    return MusicRelease("prowlarr", f"{indexer_id}:test", "Test Artist - album MP3",
                        ("Audio/MP3",), 1000, 2, indexer_id, indexer)


def test_ttl_cache_expires_and_stays_bounded(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr("musicstream.torrent.cache.time.monotonic", lambda: clock[0])
    cache = TTLCache[str](ttl_seconds=5, max_entries=2, max_bytes=4)
    cache.put("a", "one", size=2)
    cache.put("b", "two", size=2)
    cache.put("c", "three", size=2)
    assert cache.get("a") is None
    assert cache.get("b") == "two"
    clock[0] += 6
    assert cache.get("b") is None
    assert cache.get("c") is None


def test_parallel_search_is_bounded_and_cached():
    class Provider:
        active = 0
        peak = 0
        calls = 0

        async def search(self, artist, term, *, limit, indexer_ids=None):
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls += 1
            try:
                await asyncio.sleep(0.01)
                return (_release(indexer_ids[0], "TPB"),)
            finally:
                self.active -= 1

    async def run():
        provider = Provider()
        source = ApprovedProwlarrSource(provider, frozenset(), allow_unlisted=True)
        await asyncio.gather(*(source._search_term("artist", str(i), 20, 9)
                               for i in range(4)))
        assert provider.peak == 2
        await source._search_term("artist", "0", 20, 9)
        assert provider.calls == 4

    asyncio.run(run())


def test_parallel_candidates_leave_only_winner_session(local_swarm, tmp_path):
    torrent_path, _, peer_port = local_swarm

    class Provider:
        async def search(self, artist, term, *, limit, indexer_ids):
            return (_release(indexer_ids[0], "The Pirate Bay" if indexer_ids[0] == 19
                             else "NoNaMe Club"),)

    class Resolver:
        async def resolve(self, release):
            return MetadataResolution("ok", inspect_torrent(torrent_path),
                                      torrent_path.read_bytes())

    async def run():
        manager = TorrentSessionManager(
            settings=TorrentSettings(tmp_path / "cache", 256 * 1024),
            max_sessions=2, max_connections=2)
        registry = TorrentPlaybackRegistry(manager, max_streams=2, block_size=1024)
        source = ApprovedProwlarrSource(
            Provider(), frozenset(), allow_unlisted=True,
            indexer_priority=((19, "The Pirate Bay"), (7, "NoNaMe Club")))
        service = AutoTorrentPlayback(source, Resolver(), registry,
                                      peers=(("127.0.0.1", peer_port),),
                                      first_bytes=1024, preparation_timeout_seconds=10)
        try:
            result = await service.start(Track(id="1", source="deezer", artist="Test Artist",
                                               album="album", title="selected", duration_seconds=120))
            await asyncio.wait_for(service._job.task, 10)
            assert service.status(result["job_id"])["status"] == "ready"
            assert service._job.selected_release.indexer_id == 19
            assert manager.active_sessions == 1
            assert len(registry._tracks) == 1
        finally:
            await service.aclose()
            await registry.aclose()
        assert manager.active_sessions == 0

    asyncio.run(run())


def test_fast_race_uses_ready_backup_and_cleans_loser(tmp_path):
    primary = _release(19, "The Pirate Bay")
    backup = _release(7, "NoNaMe Club")

    class Manager:
        network_mode = "public_discovery"
        max_sessions = 2

        def validate_peers(self, peers):
            pass

    class Registry:
        manager = Manager()
        max_streams = 2

        def __init__(self):
            self.revoked = []

        async def unregister(self, token):
            self.revoked.append(token)

    async def run():
        registry = Registry()
        source = ApprovedProwlarrSource(object(), frozenset(), allow_unlisted=True,
                                        indexer_priority=((19, "The Pirate Bay"),
                                                          (7, "NoNaMe Club")))
        service = AutoTorrentPlayback(source, object(), registry, peers=(),
                                      preparation_timeout_seconds=5)
        started = time.monotonic()
        async def prepare_indexer(job, reference, indexer, deadline, limit):
            release = primary if indexer[0] == 19 else backup
            # The primary keeps preparing, but the verified backup can play.
            await asyncio.sleep(2 if indexer[0] == 19 else .02)
            path = tmp_path / f"{indexer[0]}.torrent"
            path.write_bytes(b"test")
            return _ReadyCandidate(str(indexer[0]), path, release)

        service._prepare_fast_indexer = prepare_indexer
        try:
            result = await service.start(Track(id="1", source="deezer", artist="Test Artist",
                                               album="album", title="selected", duration_seconds=120))
            await service._job.task
            assert service.status(result["job_id"])["status"] == "ready"
            assert service._job.selected_release == backup
            assert time.monotonic() - started < 1.5
            assert not (tmp_path / "19.torrent").exists()
        finally:
            await service.aclose()

    asyncio.run(run())
