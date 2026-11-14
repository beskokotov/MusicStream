"""Original generated FLAC tone through the local Auto Play and Range path."""

import asyncio
import hashlib
import math
import socket
import struct
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import libtorrent as lt
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from playwright.sync_api import sync_playwright

from musicstream.torrent.auto import AutoTorrentPlayback, OwnedTorrentSource, router as auto_router
from musicstream.torrent.http import TorrentPlaybackRegistry, router as range_router
from musicstream.torrent.metadata import TorrentSettings
from musicstream.torrent.session import TorrentSessionManager
from musicstream.web import STATIC_DIR, router as web_router
from test_torrent_session import bencode


def _crc(data: bytes, width: int, polynomial: int) -> int:
    value = 0
    top = 1 << (width - 1)
    mask = (1 << width) - 1
    for byte in data:
        value ^= byte << (width - 8)
        for _ in range(8):
            value = ((value << 1) ^ polynomial) & mask if value & top else (value << 1) & mask
    return value


def original_flac() -> bytes:
    """Write a small mono FLAC with verbatim frames; no external encoder needed."""
    sample_rate, block, frame_count = 8000, 4096, 24
    total = block * frame_count
    samples = [int(6000 * math.sin(2 * math.pi * 440 * i / sample_rate)) for i in range(total)]
    pcm = struct.pack("<" + "h" * total, *samples)
    streaminfo = (struct.pack(">HH", block, block) + bytes(6)
                  + ((sample_rate << 44) | (15 << 36) | total).to_bytes(8, "big")
                  + hashlib.md5(pcm).digest())
    output = bytearray(b"fLaC\x80\x00\x00\x22" + streaminfo)
    for number in range(frame_count):
        header = bytes((0xff, 0xf8, 0x70, 0x00, number)) + (block - 1).to_bytes(2, "big")
        header += bytes((_crc(header, 8, 0x07),))
        frame = header + b"\x02" + struct.pack(">" + "h" * block, *samples[number * block:(number + 1) * block])
        output.extend(frame + _crc(frame, 16, 0x8005).to_bytes(2, "big"))
    return bytes(output)


def test_original_flac_auto_play_pause_seek(tmp_path):
    browser_path = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    if not browser_path.is_file():
        import pytest
        pytest.skip("Edge is unavailable")
    album = tmp_path / "Own Album"
    album.mkdir()
    payload = original_flac()
    (album / "01 - Own Song.flac").write_bytes(payload)
    piece_length = 16 * 1024
    info = {b"name": b"Own Album", b"piece length": piece_length,
            b"pieces": b"".join(hashlib.sha1(payload[i:i + piece_length]).digest()
                                for i in range(0, len(payload), piece_length)),
            b"files": [{b"length": len(payload), b"path": [b"01 - Own Song.flac"]}]}
    torrent = tmp_path / "own.torrent"
    torrent.write_bytes(bencode({b"info": info}))

    seeder = lt.session({"listen_interfaces": "127.0.0.1:0", "enable_dht": False,
                         "enable_upnp": False, "enable_natpmp": False, "enable_lsd": False,
                         "connections_limit": 2})
    params = lt.add_torrent_params()
    params.ti = lt.torrent_info(str(torrent))
    params.save_path = str(tmp_path)
    params.flags |= lt.torrent_flags.seed_mode | lt.torrent_flags.override_trackers
    params.trackers = []
    handle = seeder.add_torrent(params)
    deadline = time.monotonic() + 5
    while seeder.listen_port() == 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert seeder.listen_port() > 0

    source = OwnedTorrentSource(torrent)
    manager = TorrentSessionManager(settings=TorrentSettings(tmp_path / "cache", 2 * 1024 * 1024),
                                    max_sessions=1, max_connections=2)
    registry = TorrentPlaybackRegistry(manager, max_streams=1, block_size=16 * 1024,
                                       read_timeout_seconds=8)
    service = AutoTorrentPlayback(source, source, registry,
                                  peers=(("127.0.0.1", seeder.listen_port()),))

    @asynccontextmanager
    async def lifespan(app):
        app.state.torrent_auto = service
        app.state.torrent_registry = registry
        registry.start()
        try:
            yield
        finally:
            await service.aclose()
            await registry.aclose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(web_router)
    app.include_router(auto_router)
    app.include_router(range_router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/api/search")
    def search_stub():
        return [{"source": "deezer", "id": "own-flac", "artist": "Own Artist",
                 "title": "Own Song", "album": "Own Album", "duration_seconds": 12}]

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="on"))
    thread = threading.Thread(target=lambda: asyncio.run(server.serve(sockets=[listener])), daemon=True)
    thread.start()
    started = time.monotonic() + 5
    while not server.started and time.monotonic() < started:
        time.sleep(0.02)
    assert server.started

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=str(browser_path))
            try:
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{port}/app")
                assert page.evaluate("document.createElement('audio').canPlayType('audio/flac')")
                page.fill("#search-input", "Own Artist")
                page.press("#search-input", "Enter")
                page.locator(".track-title").first.wait_for(timeout=10000)
                page.locator(".track-title").first.click()
                page.locator("#player-play").click()
                page.wait_for_function("document.querySelector('#hitmos-audio').currentTime > 0.1", timeout=20000)
                page.locator("#player-play").click()
                assert page.eval_on_selector("#hitmos-audio", "audio => audio.paused")
                page.locator("#player-play").click()
                page.wait_for_function("!document.querySelector('#hitmos-audio').paused", timeout=5000)
                url = page.eval_on_selector("#hitmos-audio", "audio => new URL(audio.src).pathname")
                status, content_type, body = page.evaluate("""async url => {
                    const response = await fetch(url, {headers: {Range: 'bytes=100-199'}});
                    return [response.status, response.headers.get('content-type'),
                            Array.from(new Uint8Array(await response.arrayBuffer()))];
                }""", url)
                assert status == 206
                assert content_type.startswith("audio/flac")
                assert bytes(body) == payload[100:200]
                page.eval_on_selector("#player-seek", "input => { input.value = '50'; input.dispatchEvent(new Event('change', { bubbles: true })); }")
                page.wait_for_function("document.querySelector('#hitmos-audio').currentTime >= 5", timeout=10000)
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        seeder.pause()
        seeder.remove_torrent(handle)
    assert manager.active_sessions == 0
