"""Verified-piece range adapter checks using synthetic local metadata."""

import asyncio
import hashlib
import importlib
import struct
import sys
import time
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from musicstream.torrent.http import TorrentPlaybackRegistry, router
from musicstream.torrent.safe_webseed_stream import SafeWebSeedManager, SafeWebSeedTransfer, WebSeedWorkerError
from test_safe_webseed import _torrent


def test_range_crosses_piece_and_reuses_verified_cache(tmp_path, monkeypatch):
    path = tmp_path / "album.torrent"
    info_hash, (first, second) = _torrent(path)
    data = first + second
    requested = []

    async def fake_fetch(self, index, timeout_seconds):
        requested.append(index)
        return data[index * 16384:(index + 1) * 16384]

    monkeypatch.setattr(SafeWebSeedTransfer, "_fetch_piece", fake_fetch)
    manager = SafeWebSeedManager(approved_info_hashes=frozenset({info_hash}),
                                 archive_item="album", cache_max_bytes=1024 * 1024)

    async def run():
        async with manager.open_torrent(path, 1) as transfer:
            assert await transfer.read_range(0, 8000) == second[:8000]
            assert requested == [0, 1]
            assert await transfer.read_range(0, 100) == second[:100]
            assert await transfer.read_range(7000, 8100) == second[7000:8100]
            assert requested == [0, 1]
            assert transfer._cache_bytes <= manager.cache_max_bytes
        assert manager.active_sessions == 0

    asyncio.run(run())


def test_sequential_read_batches_only_selected_file_pieces(tmp_path, monkeypatch):
    import libtorrent as lt

    path = tmp_path / "album.torrent"
    data = b"ID3" + b"A" * (6 * 16384 - 3)
    info = {b"name": b"album", b"piece length": 16384,
            b"pieces": b"".join(hashlib.sha1(data[i:i + 16384]).digest()
                                 for i in range(0, len(data), 16384)),
            b"files": [{b"length": len(data), b"path": [b"one.mp3"]}]}
    path.write_bytes(lt.bencode({b"info": info, b"url-list": [b"https://archive.org/download/"]}))
    info_hash = str(lt.load_torrent_file(str(path)).ti.info_hashes().v1)
    manager = SafeWebSeedManager(approved_info_hashes=frozenset({info_hash}), archive_item="album")
    calls = []

    async def one(self, index, timeout_seconds):
        calls.append((index, 1))
        return data[index * 16384:(index + 1) * 16384]

    async def batch(self, index, count, timeout_seconds):
        calls.append((index, count))
        return tuple(data[i * 16384:(i + 1) * 16384] for i in range(index, index + count))

    monkeypatch.setattr(SafeWebSeedTransfer, "_fetch_piece", one)
    monkeypatch.setattr(SafeWebSeedTransfer, "_fetch_batch", batch)

    async def run():
        async with manager.open_torrent(path, 0) as transfer:
            assert await transfer.read_range(0, 1024) == data[:1024]
            assert await transfer.read_range(16384, 17408) == data[16384:17408]
            assert calls == [(0, 1), (1, 4)]
            assert await transfer.read_range(3 * 16384, 3 * 16384 + 1024) == data[3 * 16384:3 * 16384 + 1024]
            assert calls == [(0, 1), (1, 4)]
            assert transfer._cache_bytes <= manager.cache_max_bytes

    asyncio.run(run())


def test_http_range_pause_seek_and_switch(tmp_path, monkeypatch):
    path = tmp_path / "album.torrent"
    info_hash, (first, second) = _torrent(path)
    data = first + second
    fetched = []

    async def fake_fetch(self, index, timeout_seconds):
        fetched.append(index)
        return data[index * 16384:(index + 1) * 16384]

    monkeypatch.setattr(SafeWebSeedTransfer, "_fetch_piece", fake_fetch)
    manager = SafeWebSeedManager(approved_info_hashes=frozenset({info_hash}),
                                 archive_item="album")
    registry = TorrentPlaybackRegistry(manager, block_size=1024, idle_seconds=5,
                                       read_timeout_seconds=40)
    registry.register("song-one", path, 0)
    registry.register("song-two", path, 1)

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
        url = "/api/experimental/torrent/song-one"
        assert client.head(url).status_code == 200
        initial = client.get(url, headers={"Range": "bytes=0-2047"})
        assert initial.status_code == 206 and initial.content == first[:2048]
        assert initial.headers["content-range"] == f"bytes 0-2047/{len(first)}"
        assert initial.headers["content-length"] == "2048"
        assert initial.headers["content-type"] == "audio/mpeg"
        assert fetched == [0]
        # Pause is represented by no request; the same transfer and piece stay cached.
        time.sleep(0.05)
        assert client.get(url, headers={"Range": "bytes=100-199"}).content == first[100:200]
        assert fetched == [0]
        seek = client.get(url, headers={"Range": "bytes=9000-11000"})
        assert seek.status_code == 206 and seek.content == first[9000:11001]
        assert fetched == [0]
        assert client.get(url, headers={"Range": f"bytes={len(first)}-"}).status_code == 416
        next_track = client.get("/api/experimental/torrent/song-two", headers={"Range": "bytes=0-4095"})
        assert next_track.status_code == 206 and next_track.content == second[:4096]
    assert manager.active_sessions == 0


def test_worker_sha1_is_rechecked(tmp_path, monkeypatch):
    path = tmp_path / "album.torrent"
    info_hash, _ = _torrent(path)
    manager = SafeWebSeedManager(approved_info_hashes=frozenset({info_hash}), archive_item="album")

    class Writer:
        def write(self, data):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

    class Reader:
        def __init__(self):
            summary = b'{"connections":1,"received_bytes":16384,"http_errors":0,"redirects":[]}'
            self.parts = [struct.pack("!I", 16384), b"X" * 16384,
                          struct.pack("!H", len(summary)), summary]

        async def readexactly(self, n):
            part = self.parts.pop(0)
            assert len(part) == n
            return part

    class Process:
        stdin = Writer()
        stdout = Reader()
        returncode = 0

        async def wait(self):
            return 0

    async def fake_process(*args, **kwargs):
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_process)

    async def run():
        async with manager.open_torrent(path, 0) as transfer:
            with pytest.raises(IOError, match="unverified"):
                await transfer.read_range(0, 100)
            assert not transfer._cache

    asyncio.run(run())


def test_cancellation_kills_dns_or_http_worker(tmp_path, monkeypatch):
    path = tmp_path / "album.torrent"
    info_hash, _ = _torrent(path)
    manager = SafeWebSeedManager(approved_info_hashes=frozenset({info_hash}), archive_item="album")
    actual_spawn = asyncio.create_subprocess_exec
    processes = []

    async def sleeping_process(*args, **kwargs):
        process = await actual_spawn(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", sleeping_process)

    async def run():
        transfer = manager.open_torrent(path, 0)
        await transfer.__aenter__()
        task = asyncio.create_task(transfer.read_range(0, 100))
        deadline = time.monotonic() + 3
        while not processes and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert processes
        started = time.monotonic()
        await transfer.aclose()
        assert time.monotonic() - started < 2
        with pytest.raises(asyncio.CancelledError):
            await task
        assert processes[0].returncode is not None
        assert manager.active_sessions == 0

    asyncio.run(run())


def test_cancellation_closes_active_worker_socket(tmp_path, monkeypatch):
    path = tmp_path / "album.torrent"
    info_hash, _ = _torrent(path)
    manager = SafeWebSeedManager(approved_info_hashes=frozenset({info_hash}), archive_item="album")
    actual_spawn = asyncio.create_subprocess_exec
    process = None

    async def run():
        nonlocal process
        accepted = asyncio.Event()
        peer_closed = asyncio.Event()

        async def handle(reader, writer):
            await reader.readexactly(1)
            accepted.set()
            try:
                await reader.read()
            except ConnectionResetError:
                pass  # Windows may reset a socket when its process is killed.
            finally:
                peer_closed.set()
                writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        async def connected_process(*args, **kwargs):
            nonlocal process
            program = ("import socket; s=socket.create_connection(('127.0.0.1', "
                       f"{port})); s.sendall(b'x'); s.recv(1)")
            process = await actual_spawn(sys.executable, "-c", program, **kwargs)
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", connected_process)
        try:
            transfer = manager.open_torrent(path, 0)
            await transfer.__aenter__()
            task = asyncio.create_task(transfer.read_range(0, 100))
            await asyncio.wait_for(accepted.wait(), 2)
            await transfer.aclose()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(peer_closed.wait(), 2)
            assert process.returncode is not None
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_http_disconnect_cancels_inflight_read(tmp_path, monkeypatch):
    from starlette.requests import Request
    from musicstream.torrent.http import _stream

    path = tmp_path / "album.torrent"
    info_hash, _ = _torrent(path)
    manager = SafeWebSeedManager(approved_info_hashes=frozenset({info_hash}), archive_item="album")
    registry = TorrentPlaybackRegistry(manager, block_size=1024)
    registry.register("song", path, 0)
    app = FastAPI()
    app.state.torrent_registry = registry
    began = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_fetch(self, index, timeout_seconds):
        began.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(SafeWebSeedTransfer, "_fetch_piece", blocked_fetch)
    scope = {
        "type": "http", "method": "GET", "path": "/api/experimental/torrent/song",
        "headers": [(b"range", b"bytes=0-1023")], "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000), "scheme": "http", "http_version": "1.1",
        "root_path": "", "asgi": {"version": "3.0", "spec_version": "2.3"}, "app": app,
    }

    async def run():
        disconnect = asyncio.Event()

        async def receive():
            await disconnect.wait()
            return {"type": "http.disconnect"}

        task = asyncio.create_task(_stream(Request(scope, receive), "song", False))
        await asyncio.wait_for(began.wait(), 2)
        disconnect.set()
        with pytest.raises(Exception) as exc:
            await asyncio.wait_for(task, 2)
        assert getattr(exc.value, "status_code", None) == 499
        assert cancelled.is_set()
        assert registry._tracks["song"].users == 0
        await registry.aclose()

    asyncio.run(run())


def test_first_piece_failure_precedes_http_headers(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from starlette.requests import Request
    from musicstream.torrent.http import _stream

    path = tmp_path / "album.torrent"
    info_hash, _ = _torrent(path)
    manager = SafeWebSeedManager(approved_info_hashes=frozenset({info_hash}), archive_item="album")
    registry = TorrentPlaybackRegistry(manager, block_size=1024)
    registry.register("song", path, 0)
    app = FastAPI()
    app.state.torrent_registry = registry
    scope = {"type": "http", "method": "GET", "path": "/api/experimental/torrent/song",
             "headers": [(b"range", b"bytes=0-1023")], "client": ("127.0.0.1", 1),
             "server": ("127.0.0.1", 8000), "scheme": "http", "http_version": "1.1",
             "root_path": "", "asgi": {"version": "3.0", "spec_version": "2.4"}, "app": app}

    async def fail(self, index, timeout_seconds):
        raise WebSeedWorkerError(index, 10)

    monkeypatch.setattr(SafeWebSeedTransfer, "_fetch_piece", fail)

    async def run():
        async def receive():
            await asyncio.Event().wait()

        with pytest.raises(HTTPException) as exc:
            await _stream(Request(scope, receive), "song", False)
        assert exc.value.status_code == 502
        assert registry._tracks["song"].users == 0
        await registry.aclose()

    asyncio.run(run())


def test_later_piece_failure_leaves_range_incomplete(tmp_path, monkeypatch):
    from starlette.requests import Request
    from musicstream.torrent.http import _stream

    path = tmp_path / "album.torrent"
    info_hash, (first, second) = _torrent(path)
    manager = SafeWebSeedManager(approved_info_hashes=frozenset({info_hash}), archive_item="album")
    registry = TorrentPlaybackRegistry(manager, block_size=6000)
    registry.register("song", path, 1)
    app = FastAPI()
    app.state.torrent_registry = registry
    scope = {"type": "http", "method": "GET", "path": "/api/experimental/torrent/song",
             "headers": [(b"range", b"bytes=0-7999")], "client": ("127.0.0.1", 1),
             "server": ("127.0.0.1", 8000), "scheme": "http", "http_version": "1.1",
             "root_path": "", "asgi": {"version": "3.0", "spec_version": "2.4"}, "app": app}
    calls = 0

    async def fail_later(self, index, timeout_seconds):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise IOError("server failure")
        return (first + second)[:16384]

    monkeypatch.setattr(SafeWebSeedTransfer, "_fetch_piece", fail_later)

    async def run():
        messages = []

        async def send(message):
            messages.append(message)

        async def receive():
            await asyncio.Event().wait()

        response = await _stream(Request(scope, receive), "song", False)
        await response(scope, receive, send)
        assert messages[0]["status"] == 206
        assert b"".join(m["body"] for m in messages[1:]) == second[:6000]
        assert all(m.get("more_body") for m in messages[1:])
        assert registry._tracks["song"].interrupted_ranges == 1
        assert registry._tracks["song"].users == 0
        await registry.aclose()

    asyncio.run(run())


def test_disconnect_after_headers_cancels_next_piece(tmp_path, monkeypatch):
    from starlette.requests import Request
    from musicstream.torrent.http import _stream

    path = tmp_path / "album.torrent"
    info_hash, (first, second) = _torrent(path)
    manager = SafeWebSeedManager(approved_info_hashes=frozenset({info_hash}), archive_item="album")
    registry = TorrentPlaybackRegistry(manager, block_size=6000)
    registry.register("song", path, 1)
    app = FastAPI()
    app.state.torrent_registry = registry
    scope = {"type": "http", "method": "GET", "path": "/api/experimental/torrent/song",
             "headers": [(b"range", b"bytes=0-7999")], "client": ("127.0.0.1", 1),
             "server": ("127.0.0.1", 8000), "scheme": "http", "http_version": "1.1",
             "root_path": "", "asgi": {"version": "3.0", "spec_version": "2.3"}, "app": app}
    began = asyncio.Event()
    cancelled = asyncio.Event()

    async def fetch(self, index, timeout_seconds):
        if index == 0:
            return (first + second)[:16384]
        began.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(SafeWebSeedTransfer, "_fetch_piece", fetch)

    async def run():
        disconnect = asyncio.Event()
        messages = []

        async def receive():
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)

        response = await _stream(Request(scope, receive), "song", False)
        task = asyncio.create_task(response(scope, receive, send))
        await asyncio.wait_for(began.wait(), 2)
        assert messages[0]["status"] == 206
        assert messages[1]["body"] == second[:6000]
        disconnect.set()
        await asyncio.wait_for(task, 2)
        assert cancelled.is_set()
        assert registry._tracks["song"].users == 0
        await registry.aclose()

    asyncio.run(run())


def test_transient_piece_error_retries_once_and_cancellation_does_not_retry(tmp_path, monkeypatch):
    path = tmp_path / "album.torrent"
    info_hash, (first, second) = _torrent(path)
    manager = SafeWebSeedManager(approved_info_hashes=frozenset({info_hash}), archive_item="album")
    calls = []

    async def transient(self, index, timeout_seconds):
        calls.append(index)
        if len(calls) == 1:
            raise WebSeedWorkerError(index, 11)
        return (first + second)[:16384]

    monkeypatch.setattr(SafeWebSeedTransfer, "_fetch_piece", transient)

    async def run():
        async with manager.open_torrent(path, 0) as transfer:
            assert await transfer.read_range(0, 100) == first[:100]
            assert calls == [0, 0]
            assert transfer._retry_count == 1

    asyncio.run(run())


def test_persistent_piece_error_stops_after_two_attempts(tmp_path, monkeypatch):
    path = tmp_path / "album.torrent"
    info_hash, _ = _torrent(path)
    manager = SafeWebSeedManager(approved_info_hashes=frozenset({info_hash}), archive_item="album")
    calls = []

    async def fail(self, index, timeout_seconds):
        calls.append(index)
        raise WebSeedWorkerError(index, 11)

    monkeypatch.setattr(SafeWebSeedTransfer, "_fetch_piece", fail)

    async def run():
        async with manager.open_torrent(path, 0) as transfer:
            with pytest.raises(WebSeedWorkerError):
                await transfer.read_range(0, 100)
            assert calls == [0, 0]
            assert transfer._retry_count == 1
            assert transfer._attempted_bytes == 2 * 16384
            assert not transfer._cache

    asyncio.run(run())


def test_safe_webseed_fastapi_start_is_lazy(tmp_path, monkeypatch):
    import libtorrent as lt

    main = importlib.import_module("musicstream.main")
    path = tmp_path / "album.torrent"
    info_hash, _ = _torrent(path)
    monkeypatch.setattr(main, "TORRENT_HTTP_ENABLED", True)
    monkeypatch.setattr(main, "TORRENT_AUTO_ENABLED", False)
    monkeypatch.setenv("MUSICSTREAM_TORRENT_NETWORK_MODE", "safe_webseed")
    monkeypatch.setenv("MUSICSTREAM_TORRENT_PUBLIC_APPROVED_INFO_HASHES", f'["{info_hash}"]')
    monkeypatch.setattr(lt, "session", lambda *args: pytest.fail("BitTorrent session started"))

    async def run():
        async with main.lifespan(main.app):
            registry = main.app.state.torrent_registry
            assert registry.manager.network_mode == "safe_webseed"
            assert registry.manager.active_sessions == 0

    asyncio.run(run())


def test_failed_piece_attempts_consume_download_budget(tmp_path, monkeypatch):
    path = tmp_path / "album.torrent"
    info_hash, _ = _torrent(path)
    manager = SafeWebSeedManager(approved_info_hashes=frozenset({info_hash}), archive_item="album")
    manager.max_download_bytes = 16384
    calls = []

    async def fails(self, index, timeout_seconds):
        calls.append(index)
        raise IOError("Remote server failed")

    monkeypatch.setattr(SafeWebSeedTransfer, "_fetch_piece", fails)

    async def run():
        async with manager.open_torrent(path, 0) as transfer:
            with pytest.raises(IOError, match="Remote server failed"):
                await transfer.read_range(0, 100)
            with pytest.raises(IOError, match="download limit"):
                await transfer.read_range(0, 100)
            assert calls == [0]

    asyncio.run(run())
