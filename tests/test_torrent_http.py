"""HTTP Range integration with a generated torrent and a loopback seeder."""

import time
from contextlib import asynccontextmanager
from pathlib import Path

import libtorrent as lt
import pytest
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient

from musicstream.torrent import TorrentSessionManager, TorrentSettings
from musicstream.torrent.http import TorrentPlaybackRegistry, router
from test_torrent_session import local_swarm


def test_prepared_local_album_range_if_available(tmp_path):
    import runpy

    album = Path(__file__).resolve().parents[1] / "test_media" / "TorrentTestAlbum"
    if not album.is_dir():
        pytest.skip("Optional local MP3 album is not present")
    prepare = runpy.run_path(str(album.parents[1] / "scripts" / "local_torrent_seeder.py"))["prepare"]
    torrent_path, files = prepare(album)
    source = files[1]
    seeder = lt.session({
        "listen_interfaces": "127.0.0.1:0", "enable_dht": False,
        "enable_upnp": False, "enable_natpmp": False, "enable_lsd": False,
        "connections_limit": 2,
    })
    params = lt.add_torrent_params()
    params.ti = lt.torrent_info(str(torrent_path))
    params.save_path = str(album.parent)
    params.flags |= lt.torrent_flags.seed_mode | lt.torrent_flags.override_trackers
    params.trackers = []
    handle = seeder.add_torrent(params)
    deadline = time.monotonic() + 5
    while seeder.listen_port() == 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert seeder.listen_port() > 0

    manager = TorrentSessionManager(settings=TorrentSettings(tmp_path / "cache", 64 * 1024 * 1024))
    registry = TorrentPlaybackRegistry(manager, block_size=16 * 1024, idle_seconds=0.2)
    registry.register("real-mp3", torrent_path, 1, peers=(("127.0.0.1", seeder.listen_port()),))

    @asynccontextmanager
    async def lifespan(app):
        app.state.torrent_registry = registry
        try:
            yield
        finally:
            await registry.aclose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    try:
        with TestClient(app, client=("127.0.0.1", 12345)) as client:
            url = "/api/experimental/torrent/real-mp3"
            for start, end in ((0, 32767), (source.stat().st_size // 2, source.stat().st_size // 2 + 32767)):
                response = client.get(url, headers={"Range": f"bytes={start}-{end}"})
                with source.open("rb") as stream:
                    stream.seek(start)
                    expected = stream.read(end - start + 1)
                assert response.status_code == 206
                assert response.content == expected
            assert manager.active_sessions == 1
    finally:
        seeder.pause()
        seeder.remove_torrent(handle)
    assert manager.active_sessions == 0
    assert list((tmp_path / "cache").iterdir()) == []


def test_http_range_seek_reuse_and_idle_cleanup(local_swarm, tmp_path):
    torrent_path, selected, port = local_swarm
    manager = TorrentSessionManager(settings=TorrentSettings(tmp_path / "cache", 128 * 1024))
    registry = TorrentPlaybackRegistry(manager, block_size=1024, idle_seconds=0.2)
    registry.register("demo", torrent_path, 1, peers=(("127.0.0.1", port),))

    @asynccontextmanager
    async def lifespan(app):
        app.state.torrent_registry = registry
        registry.start()
        try:
            yield
        finally:
            await registry.aclose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        url = "/api/experimental/torrent/demo"
        head = client.head(url)
        assert head.status_code == 200 and head.content == b""
        assert head.headers["content-length"] == str(len(selected))
        assert head.headers["accept-ranges"] == "bytes"
        head_range = client.head(url, headers={"Range": "bytes=100-199"})
        assert head_range.status_code == 206 and head_range.content == b""
        assert head_range.headers["content-range"] == f"bytes 100-199/{len(selected)}"
        assert manager.active_sessions == 0

        first = client.get(url, headers={"Range": "bytes=0-999"})
        assert first.status_code == 206 and first.content == selected[:1000]
        assert first.headers["content-range"] == f"bytes 0-999/{len(selected)}"
        assert first.headers["content-length"] == "1000"
        assert first.headers["content-type"] == "audio/mpeg"
        transfer = registry._tracks["demo"].transfer
        assert transfer is not None and manager.active_sessions == 1
        assert client.post(f"{url}/keepalive").status_code == 204

        middle = client.get(url, headers={"Range": "bytes=20000-22999"})
        assert middle.status_code == 206 and middle.content == selected[20000:23000]
        assert registry._tracks["demo"].transfer is transfer
        assert not transfer._handle.have_piece(0)
        assert not transfer._handle.have_piece(3)

        suffix = client.get(url, headers={"Range": "bytes=-16"})
        assert suffix.status_code == 206 and suffix.content == selected[-16:]
        full = client.get(url)
        assert full.status_code == 200 and full.content == selected
        assert full.headers["content-length"] == str(len(selected))
        assert "content-range" not in full.headers
        unsatisfied = client.get(url, headers={"Range": f"bytes={len(selected)}-"})
        assert unsatisfied.status_code == 416
        assert unsatisfied.headers["content-range"] == f"bytes */{len(selected)}"
        assert client.get(url, headers={"Range": "bytes=0-1,4-5"}).status_code == 416

        deadline = time.monotonic() + 2
        while manager.active_sessions and time.monotonic() < deadline:
            time.sleep(0.05)
        assert manager.active_sessions == 0
        assert list((tmp_path / "cache").iterdir()) == []


def test_playback_keepalive_prevents_idle_session_reap(local_swarm, tmp_path):
    import asyncio

    torrent_path, _, port = local_swarm
    manager = TorrentSessionManager(settings=TorrentSettings(tmp_path / "cache", 128 * 1024))
    registry = TorrentPlaybackRegistry(manager, idle_seconds=0.1)
    registry.register("demo", torrent_path, 1, peers=(("127.0.0.1", port),))

    async def run():
        track = await registry.acquire("demo")
        await registry.read(track, 0, 1000)
        await registry.release(track)
        transfer = track.transfer
        await asyncio.sleep(0.06)
        await registry.keepalive("demo")
        await asyncio.sleep(0.06)
        await registry.reap_idle()
        assert track.transfer is transfer
        assert manager.active_sessions == 1
        await asyncio.sleep(0.11)
        await registry.reap_idle()
        assert track.transfer is None
        assert manager.active_sessions == 0
        await registry.aclose()

    asyncio.run(run())


def test_timed_out_range_can_retry_on_same_playback_session(local_swarm, tmp_path):
    torrent_path, selected, port = local_swarm
    manager = TorrentSessionManager(settings=TorrentSettings(tmp_path / "cache", 128 * 1024))
    registry = TorrentPlaybackRegistry(manager, block_size=1024)
    registry.register("retry", torrent_path, 1, peers=(("127.0.0.1", port),))
    original_read = registry.read
    reads = 0

    async def one_transient_timeout(track, start, end, *, timeout_seconds=None):
        nonlocal reads
        reads += 1
        if reads == 1:
            raise TimeoutError("temporary missing piece")
        return await original_read(track, start, end, timeout_seconds=timeout_seconds)

    registry.read = one_transient_timeout

    @asynccontextmanager
    async def lifespan(app):
        app.state.torrent_registry = registry
        try:
            yield
        finally:
            await registry.aclose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        url = "/api/experimental/torrent/retry"
        failed = client.get(url, headers={"Range": "bytes=0-999"})
        assert failed.status_code == 504
        existing = registry._tracks["retry"].transfer
        assert existing is not None and manager.active_sessions == 1
        recovered = client.get(url, headers={"Range": "bytes=0-999"})
        assert recovered.status_code == 206
        assert recovered.content == selected[:1000]
        assert registry._tracks["retry"].transfer is existing
        assert manager.active_sessions == 1
        closed = client.delete(url)
        assert closed.status_code == 204
        assert "retry" not in registry._tracks
        assert manager.active_sessions == 0


def test_http_rejects_nonlocal_client(local_swarm, tmp_path):
    torrent_path, _, port = local_swarm
    registry = TorrentPlaybackRegistry(TorrentSessionManager(settings=TorrentSettings(tmp_path / "cache")))
    registry.register("demo", torrent_path, 1, peers=(("127.0.0.1", port),))
    app = FastAPI()
    app.state.torrent_registry = registry
    app.include_router(router)
    with TestClient(app, client=("192.0.2.1", 12345)) as client:
        assert client.get("/api/experimental/torrent/demo", headers={"Range": "bytes=0-1"}).status_code == 404
    assert registry.manager.active_sessions == 0


def test_disconnected_stream_releases_lease(local_swarm, tmp_path):
    import asyncio

    from starlette.requests import Request
    from musicstream.torrent.http import _stream

    torrent_path, _, port = local_swarm
    manager = TorrentSessionManager(settings=TorrentSettings(tmp_path / "cache", 128 * 1024))
    registry = TorrentPlaybackRegistry(manager, block_size=1024, idle_seconds=0.05)
    registry.register("demo", torrent_path, 1, peers=(("127.0.0.1", port),))
    app = FastAPI()
    app.state.torrent_registry = registry
    scope = {
        "type": "http", "method": "GET", "path": "/api/experimental/torrent/demo",
        "headers": [(b"range", b"bytes=0-20000")], "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000), "scheme": "http", "http_version": "1.1",
        "root_path": "", "asgi": {"version": "3.0", "spec_version": "2.3"}, "app": app,
    }

    async def run():
        disconnected = asyncio.Event()
        sent = []

        async def receive():
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                disconnected.set()

        response = await _stream(Request(scope, receive), "demo", False)
        await response(scope, receive, send)
        assert any(item["type"] == "http.response.body" and item.get("body") for item in sent)
        assert registry._tracks["demo"].users == 0
        await asyncio.sleep(0.1)
        await registry.reap_idle()
        assert manager.active_sessions == 0
        assert list((tmp_path / "cache").iterdir()) == []
        await registry.aclose()

    asyncio.run(run())


def test_stream_limit(local_swarm, tmp_path):
    import asyncio

    torrent_path, _, port = local_swarm
    registry = TorrentPlaybackRegistry(
        TorrentSessionManager(settings=TorrentSettings(tmp_path / "cache")), max_streams=1,
    )
    registry.register("demo", torrent_path, 1, peers=(("127.0.0.1", port),))

    async def run():
        track = await registry.acquire("demo")
        try:
            with pytest.raises(HTTPException) as error:
                await registry.acquire("demo")
            assert error.value.status_code == 429
        finally:
            await registry.release(track)
            await registry.aclose()
        assert registry.manager.active_sessions == 0

    asyncio.run(run())
