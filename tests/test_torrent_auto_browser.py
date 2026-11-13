"""Optional local browser Play/Pause/Seek check with the prepared test MP3 album."""

import asyncio
import socket
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import libtorrent as lt
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from playwright.sync_api import sync_playwright

from musicstream.torrent.auto import AutoTorrentPlayback, OwnedTorrentSource, router as auto_router
from musicstream.torrent.http import TorrentPlaybackRegistry, router as range_router
from musicstream.torrent.metadata import TorrentSettings
from musicstream.torrent.session import TorrentSessionManager
from musicstream.web import STATIC_DIR, router as web_router


def test_browser_play_pause_seek_and_switch(tmp_path):
    album = Path(__file__).resolve().parents[1] / "test_media" / "TorrentTestAlbum"
    torrent = album.parent / "TorrentTestAlbum.local.torrent"
    browser_path = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    if not (album.is_dir() and torrent.is_file() and browser_path.is_file()):
        pytest.skip("Optional local MP3 album or Edge is unavailable")

    seeder = lt.session({"listen_interfaces": "127.0.0.1:0", "enable_dht": False,
                         "enable_upnp": False, "enable_natpmp": False, "enable_lsd": False,
                         "connections_limit": 2})
    params = lt.add_torrent_params()
    params.ti = lt.torrent_info(str(torrent))
    params.save_path = str(album.parent)
    params.flags |= lt.torrent_flags.seed_mode | lt.torrent_flags.override_trackers
    params.trackers = []
    handle = seeder.add_torrent(params)
    deadline = time.monotonic() + 5
    while seeder.listen_port() == 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert seeder.listen_port() > 0

    source = OwnedTorrentSource(torrent)
    manager = TorrentSessionManager(settings=TorrentSettings(tmp_path / "cache", 16 * 1024 * 1024),
                                    max_sessions=1, max_connections=2)
    registry = TorrentPlaybackRegistry(manager, max_streams=1, block_size=32 * 1024,
                                       idle_seconds=1, read_timeout_seconds=8)
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
        return [
            {"source": "deezer", "id": "own-test-1", "artist": "DMXS", "title": "Death Desert",
             "album": "Death Desert", "duration_seconds": 180},
            {"source": "deezer", "id": "own-test-2", "artist": "No Artist", "title": "No Song",
             "album": "No Album", "duration_seconds": 180},
        ]

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
                page.fill("#search-input", "DMXS")
                page.press("#search-input", "Enter")
                page.locator(".track-title").first.wait_for(timeout=10000)
                page.locator(".track-title").first.click()
                page.locator("#player-play").click()
                page.wait_for_function("document.querySelector('#hitmos-audio').readyState >= 1", timeout=20000)
                page.wait_for_function("!document.querySelector('#hitmos-audio').paused", timeout=20000)
                page.wait_for_function("document.querySelector('#hitmos-audio').currentTime > 0.1", timeout=10000)
                page.locator("#player-play").click()
                assert page.eval_on_selector("#hitmos-audio", "audio => audio.paused")
                page.locator("#player-play").click()
                page.wait_for_function("!document.querySelector('#hitmos-audio').paused", timeout=5000)
                page.eval_on_selector("#player-seek", "input => { input.value = '10'; input.dispatchEvent(new Event('change', { bubbles: true })); }")
                page.wait_for_function("document.querySelector('#hitmos-audio').currentTime >= 9", timeout=10000)
                page.locator(".track-title").nth(1).click()
                assert page.eval_on_selector("#hitmos-audio", "audio => !audio.hasAttribute('src')")
                page.locator("#player-play").click()
                page.wait_for_function("document.querySelector('#player-play').disabled && document.querySelector('#source-status').textContent.includes('недоступна')", timeout=10000)
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        seeder.pause()
        seeder.remove_torrent(handle)
    assert manager.active_sessions == 0
