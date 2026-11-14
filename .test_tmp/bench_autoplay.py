"""Temporary browser timing probe; prints no torrent URLs or credentials."""

import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


query = sys.argv[1]
edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, executable_path=str(edge))
    try:
        page = browser.new_page()
        responses = []
        page.on("response", lambda response: responses.append({
            "status": response.status,
            "type": response.headers.get("content-type", ""),
        }) if "/api/experimental/torrent/" in response.url else None)
        page.goto("http://127.0.0.1:8000/app")
        page.fill("#search-input", query)
        page.press("#search-input", "Enter")
        page.locator(".track-title").first.wait_for(timeout=15000)
        title = page.locator(".track-title").first.inner_text()
        page.locator(".track-title").first.click()
        page.evaluate("""() => {
          window.__playBench = {start: performance.now(), waiting: 0, stalled: 0, playing: null, sound: null};
          const audio = document.querySelector('#hitmos-audio');
          audio.addEventListener('waiting', () => window.__playBench.waiting++);
          audio.addEventListener('stalled', () => window.__playBench.stalled++);
          audio.addEventListener('playing', () => window.__playBench.playing ??= performance.now());
          audio.addEventListener('timeupdate', () => {
            if (audio.currentTime > 0.2 && window.__playBench.sound === null) {
              window.__playBench.sound = performance.now();
              window.__playBench.waitingAtSound = window.__playBench.waiting;
              window.__playBench.stalledAtSound = window.__playBench.stalled;
            }
          });
        }""")
        started = time.monotonic()
        page.locator("#player-play").click()
        try:
            page.wait_for_function("""() => window.__playBench.sound !== null ||
              /недоступен|Ошибка/.test(document.querySelector('#playback-state').textContent)
            """, timeout=140000)
        except Exception:
            pass
        elapsed = time.monotonic() - started
        if page.evaluate("window.__playBench.sound !== null"):
            page.wait_for_timeout(10000)
        state = page.locator("#playback-state").inner_text()
        data = page.evaluate("window.__playBench")
        result = {"query": query, "selected_title": title, "state": state,
                  "wall_seconds": round(elapsed, 3),
                  "playing_seconds": round((data["playing"] - data["start"]) / 1000, 3)
                  if data["playing"] else None,
                  "first_audio_seconds": round((data["sound"] - data["start"]) / 1000, 3)
                  if data["sound"] else None,
                  "waiting_events": data["waiting"], "stalled_events": data["stalled"],
                  "post_start_waiting": data["waiting"] - data.get("waitingAtSound", data["waiting"]),
                  "post_start_stalled": data["stalled"] - data.get("stalledAtSound", data["stalled"]),
                  "range_responses": responses}
        print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        browser.close()
