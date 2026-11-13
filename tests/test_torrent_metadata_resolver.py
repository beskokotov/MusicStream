"""Loopback HTTP serves only our own tiny torrent metadata, never public media."""

import asyncio
import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx
import libtorrent as lt
import pytest

from musicstream.torrent.metadata_resolver import TorrentMetadataResolver
from musicstream.torrent.prowlarr_discovery import parse_releases
from musicstream.torrent.track_matcher import TrackReference


def bencode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(map(bencode, value)) + b"e"
    return b"d" + b"".join(bencode(key) + bencode(value[key]) for key in sorted(value)) + b"e"


def own_torrent(tmp_path, names):
    files = []
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        content = b"ID3" + bytes([len(files) + 1]) * 32 if name.endswith(".mp3") else b"cover"
        path.write_bytes(content)
        files.append((name, content))
    joined = b"".join(content for _, content in files)
    info = {b"name": b"own-album", b"piece length": 16384,
            b"pieces": hashlib.sha1(joined).digest(),
            b"files": [{b"length": len(content), b"path": [part.encode() for part in name.split("/")]}
                       for name, content in files]}
    return bencode({b"info": info}), files


def local_release(base_url):
    row = {"title": "Кино - Группа крови MP3 tracks", "guid": "own-release",
           "protocol": "torrent", "indexerId": 1, "indexer": "Local test",
           "size": 1024, "seeders": 1, "categories": [{"id": 3010, "name": "Audio/MP3"}],
           "downloadUrl": base_url + "/1/download?apikey=not-forwarded&link=own-token"}
    return parse_releases([row], base_url=base_url)[0]


@pytest.fixture
def local_server():
    state = {"body": b"", "status": 200, "headers": {}, "requests": 0,
             "omit_length": False}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"] += 1
            assert urlsplit(self.path).path == "/1/download"
            assert self.headers.get("X-Api-Key") == "test-key"
            assert "apikey" not in self.path.lower()
            self.send_response(state["status"])
            if not state["omit_length"]:
                self.send_header("Content-Length", str(len(state["body"])))
            for key, value in state["headers"].items():
                self.send_header(key, value)
            self.end_headers()
            if state["status"] == 200:
                self.wfile.write(state["body"])

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_real_torrent_metadata_through_local_prowlarr_route(tmp_path, local_server, monkeypatch):
    base, state = local_server
    state["body"], files = own_torrent(tmp_path, [
        "Группа крови/cover.jpg", "Группа крови/01 - Группа крови.mp3",
        "Группа крови/02 - Закрой за мной дверь.mp3",
    ])
    monkeypatch.setattr(lt, "session", lambda *args, **kwargs: pytest.fail("No BitTorrent session allowed"))

    async def run():
        async with httpx.AsyncClient(trust_env=False) as client:
            resolver = TorrentMetadataResolver(client, api_key="test-key", base_url=base,
                                               allowed_indexer_ids=(1,))
            result = await resolver.resolve_track(
                TrackReference("Кино", "Группа крови", "Группа крови"), local_release(base))
            assert result.status == "matched" and result.file_index == 1
            assert [(f.index, f.name, f.size, f.extension) for f in result.metadata.files] == [
                (i, name.split("/")[-1], len(content), "." + name.rsplit(".", 1)[-1])
                for i, (name, content) in enumerate(files)]
            assert state["requests"] == 1

    asyncio.run(run())


@pytest.mark.parametrize("names,track,expected", [
    (["Album/01 - Другая песня.mp3"], "Группа крови", "missing"),
    (["Album/01 - Группа крови.mp3", "Album/02 - Группа крови.mp3"], "Группа крови", "ambiguous"),
    (["Album/cover.jpg"], "Группа крови", "missing"),
])
def test_match_diagnostics_from_real_torrent_file_list(tmp_path, local_server, names, track, expected):
    base, state = local_server
    state["body"], _ = own_torrent(tmp_path, names)

    async def run():
        async with httpx.AsyncClient(trust_env=False) as client:
            resolver = TorrentMetadataResolver(client, api_key="test-key", base_url=base,
                                               allowed_indexer_ids=(1,))
            result = await resolver.resolve_track(TrackReference("Кино", track), local_release(base))
            assert result.status == expected and result.file_index is None

    asyncio.run(run())


@pytest.mark.parametrize("status,body,expected", [
    (302, b"", "redirect_rejected"),
    (403, b"", "auth_error"),
    (503, b"", "unavailable"),
    (200, b"<html>not a torrent</html>", "invalid_torrent"),
])
def test_http_failures_are_diagnostic(local_server, status, body, expected):
    base, state = local_server
    state.update(status=status, body=body)
    if status == 302:
        state["headers"] = {"Location": "https://example.com/release.torrent"}

    async def run():
        async with httpx.AsyncClient(trust_env=False) as client:
            resolver = TorrentMetadataResolver(client, api_key="test-key", base_url=base,
                                               allowed_indexer_ids=(1,))
            assert (await resolver.resolve(local_release(base))).status == expected
            assert state["requests"] == 1

    asyncio.run(run())


def test_size_and_url_allowlist(local_server):
    base, state = local_server
    state["body"] = b"d" + b"x" * 2048

    async def run():
        async with httpx.AsyncClient(trust_env=False) as client:
            resolver = TorrentMetadataResolver(client, api_key="test-key", base_url=base,
                                               allowed_indexer_ids=(1,), max_bytes=1024)
            release = local_release(base)
            assert (await resolver.resolve(release)).status == "too_large"
            assert (await resolver.resolve(release.__class__(
                **{**release.__dict__, "metadata_url": "https://example.com/1/download?link=x"}))).status == "unsupported_source"
            assert (await resolver.resolve(release.__class__(
                **{**release.__dict__, "metadata_url": base + "/1/download?link=x&apikey=secret"}))).status == "unsupported_source"
            assert (await resolver.resolve(release.__class__(
                **{**release.__dict__, "indexer_id": 2}))).status == "unsupported_source"
            assert state["requests"] == 1

    asyncio.run(run())


def test_streaming_limit_without_content_length(local_server):
    base, state = local_server
    state["body"] = b"d" + b"x" * 2048
    state["omit_length"] = True

    async def run():
        async with httpx.AsyncClient(trust_env=False) as client:
            resolver = TorrentMetadataResolver(client, api_key="test-key", base_url=base,
                                               allowed_indexer_ids=(1,), max_bytes=1024)
            assert (await resolver.resolve(local_release(base))).status == "too_large"

    asyncio.run(run())


def test_missing_libtorrent_is_not_invalid_metadata(tmp_path, local_server, monkeypatch):
    import musicstream.torrent.metadata_resolver as module

    base, state = local_server
    state["body"], _ = own_torrent(tmp_path, ["01 - Группа крови.mp3"])
    monkeypatch.setattr(module, "_libtorrent", lambda: (_ for _ in ()).throw(RuntimeError("missing")))

    async def run():
        async with httpx.AsyncClient(trust_env=False) as client:
            resolver = TorrentMetadataResolver(client, api_key="test-key", base_url=base,
                                               allowed_indexer_ids=(1,))
            assert (await resolver.resolve(local_release(base))).status == "dependency_missing"

    asyncio.run(run())
