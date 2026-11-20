from __future__ import annotations

import os
import socket
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from musicstream.audio_providers.playback_adapter import PlaybackAdapter
from musicstream.audio_providers.routes import router
from musicstream.web import STATIC_DIR, router as web_router


def make_app() -> FastAPI:
    app = FastAPI()
    app.state.playback_test_adapter = PlaybackAdapter()
    app.state.playback_test_enabled = True
    app.include_router(web_router)
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def test_playback_test_route_is_hidden_when_adapter_is_disabled():
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        assert client.get("/api/audio/playback-test/prepare").status_code == 404


def test_playback_test_supports_head_range_and_expiry():
    with TestClient(make_app()) as client:
        prepared = client.get("/api/audio/playback-test/prepare").json()
        url = prepared["playback_url"]
        head = client.head(url)
        assert head.status_code == 200
        assert head.headers["content-type"] == "audio/mpeg"
        assert head.headers["accept-ranges"] == "bytes"
        assert int(head.headers["content-length"]) > 1000

        ranged = client.get(url, headers={"Range": "bytes=0-1023"})
        assert ranged.status_code == 206
        assert ranged.headers["content-range"] == f"bytes 0-1023/{head.headers['content-length']}"
        assert len(ranged.content) == 1024
        assert ranged.content[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}

        bad_range = client.get(url, headers={"Range": "bytes=999999999-"})
        assert bad_range.status_code == 416

        grant_token = url.rsplit("/", 1)[-1]
        client.app.state.playback_test_adapter._grants[grant_token] = time.monotonic() - 1
        expired = client.get(url)
        assert expired.status_code == 410
        assert expired.json()["detail"]["reason"] == "playback_source_expired"


@pytest.mark.skipif(not os.path.isfile(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
                    reason="Chrome is not installed")
def test_existing_musicstream_audio_element_plays_pauses_and_seeks():
    playwright = pytest.importorskip("playwright.sync_api")
    import uvicorn

    app = make_app()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                headless=True,
                args=["--autoplay-policy=no-user-gesture-required"],
            )
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{port}/app", wait_until="domcontentloaded")
            page.locator("#developer-mode").evaluate("details => { details.open = true; }")
            page.wait_for_function("!document.querySelector('#playback-test-button').hidden")
            page.evaluate("""() => {
              window.testPlayingEvents = [];
              document.querySelector('#hitmos-audio').addEventListener('playing', () => window.testPlayingEvents.push('playing'));
            }""")
            page.locator("#playback-test-button").click()
            page.wait_for_function("document.querySelector('#hitmos-audio').currentTime >= 1.2", timeout=10000)
            pcm_rms = page.locator("#hitmos-audio").evaluate("""async audio => {
              const context = new AudioContext();
              window.testAudioContext = context;
              const source = context.createMediaElementSource(audio);
              const analyser = context.createAnalyser();
              analyser.fftSize = 2048;
              source.connect(analyser);
              analyser.connect(context.destination);
              await context.resume();
              const samples = new Float32Array(analyser.fftSize);
              await new Promise(resolve => setTimeout(resolve, 150));
              analyser.getFloatTimeDomainData(samples);
              const rms = Math.sqrt(samples.reduce((sum, sample) => sum + sample * sample, 0) / samples.length);
              return rms;
            }""")
            before_pause = page.locator("#hitmos-audio").evaluate("audio => audio.currentTime")
            page.locator("#player-play").click()
            page.wait_for_function("document.querySelector('#hitmos-audio').paused")
            page.locator("#player-seek").evaluate("seek => { seek.value = '12'; seek.dispatchEvent(new Event('change', { bubbles: true })); }")
            page.wait_for_function("Math.abs(document.querySelector('#hitmos-audio').currentTime - 12) < 0.25")
            seeked = page.locator("#hitmos-audio").evaluate("audio => audio.currentTime")
            page.locator("#player-volume").evaluate("volume => { volume.value = '25'; volume.dispatchEvent(new Event('input', { bubbles: true })); }")
            volume_level = page.locator("#hitmos-audio").evaluate("audio => audio.volume")
            page.locator("#player-play").click()
            page.wait_for_function("document.querySelector('#hitmos-audio').currentTime > 12.4", timeout=5000)
            result = page.evaluate("""() => ({events: window.testPlayingEvents,
              paused: document.querySelector('#hitmos-audio').paused,
              resumedTime: document.querySelector('#hitmos-audio').currentTime,
              duration: document.querySelector('#hitmos-audio').duration})""")
            page.evaluate("window.testAudioContext.close()")
            browser.close()
        assert "playing" in result["events"]
        assert pcm_rms > 0.002
        assert before_pause >= 1.2
        assert result["paused"] is False
        assert seeked == pytest.approx(12, abs=0.25)
        assert volume_level == pytest.approx(0.25, abs=0.01)
        assert result["resumedTime"] > seeked
        assert result["duration"] == pytest.approx(14.088, abs=0.2)
    finally:
        server.should_exit = True
        server_thread.join(timeout=5)
