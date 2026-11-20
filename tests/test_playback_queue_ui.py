import asyncio
import json
from itertools import groupby
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("playwright.async_api")

from playwright.async_api import Error, async_playwright, expect

from musicstream.audio_providers.base import RecordingReference
from musicstream.audio_providers.hitmos import HitmosProvider
from test_hitmos_discovery import Reader


@pytest.mark.parametrize("scenario", [
    "navigation", "late_response", "album_and_failure", "tile_toggle",
    "prefetch_ready", "prefetch_cancelled", "prefetch_budget", "prefetch_expired", "prefetch_error", "prefetch_next",
    "prefetch_pending_end",
])
def test_playback_queue_in_chrome(scenario):
    asyncio.run(check_queue(scenario))


async def check_queue(scenario):
    static = Path(__file__).resolve().parents[1] / "src/musicstream/static"
    audio_fixture = (static / "audio-transport-test.mp3").read_bytes()
    origin = "http://127.0.0.1:8017"
    tracks = [
        {"id": str(index), "title": f"Queue track {index}", "artist": "Queue artist",
         "duration_seconds": 170, "source": "deezer"}
        for index in range(1, 4)
    ]
    discoveries, preparations, tokens = {}, {}, {}
    for index, track in enumerate(tracks, 1):
        hitmos_id = str(index + 10)
        ref = RecordingReference(title=track["title"], artist=track["artist"], duration_seconds=170)
        provider = HitmosProvider(Reader({hitmos_id: (ref.title, ref.artist, 170)}))
        discoveries[track["id"]] = (await provider.find(ref)).model_dump()
        prepared = await provider.lookup(ref, hitmos_id)
        token = f"/api/audio/hitmos-stream/{str(index) * 32}"
        prepared.candidates[0].stream_url = token
        preparations[track["id"]] = prepared.model_dump()
        tokens[token] = track["id"]
    if scenario == "late_response":
        discoveries["3"].update(selection="choose", selected_id=None, selected_source=None)
        discoveries["3"]["candidates"][0]["selection_reason"] = "manual_confirmation_required"

    pending_search = asyncio.Event()
    release_search = asyncio.Event()
    search_finished = asyncio.Event()
    prefetch = scenario.startswith("prefetch_")
    saved = json.dumps({"songId": "12", "title": "queue track 2", "artist": "queue artist",
                        "duration": 170, "sourceDuration": 170, "explicit": None})
    prefetch_requests = []
    lookups, media_requests, album_pages, page_errors = [], [], [], []
    search_failure = False
    broken_audio = scenario == "album_and_failure"
    assets = {"/app": ("index.html", "text/html"),
              "/static/app.js": ("app.js", "application/javascript"),
              "/static/config.js": ("config.js", "application/javascript"),
              "/static/style.css": ("style.css", "text/css")}

    async def route_handler(route):
        url = urlsplit(route.request.url)
        path = url.path
        params = parse_qs(url.query)
        if path == "/api/search":
            await route.fulfill(json=tracks)
        elif path == "/api/audio/resolve":
            track_id = params["deezer_id"][0]
            candidate_id = params.get("hitmos_id", [None])[0]
            if prefetch and params.get("hitmos_trial") == ["true"]:
                prefetch_requests.append((track_id, candidate_id))
                if scenario in {"prefetch_cancelled", "prefetch_pending_end"}:
                    pending_search.set()
                    await release_search.wait()
                    try:
                        await route.fulfill(json=preparations[track_id])
                    except Error:
                        pass
                    finally:
                        search_finished.set()
                elif scenario == "prefetch_budget":
                    await route.fulfill(json={"reason": "request_budget_exhausted", "retry_after_seconds": 10})
                elif scenario == "prefetch_error":
                    await route.fulfill(status=429)
                else:
                    await route.fulfill(json=preparations[track_id])
                return
            lookups.append((track_id, candidate_id))
            if candidate_id:
                await route.fulfill(json=preparations[track_id])
            elif search_failure and track_id == "2":
                await route.fulfill(status=503)
            elif scenario == "late_response" and track_id == "2":
                pending_search.set()
                await release_search.wait()
                try:
                    await route.fulfill(json=discoveries[track_id])
                except Error:
                    pass  # The request can already have been cancelled by the player.
                finally:
                    search_finished.set()
            else:
                await route.fulfill(json=discoveries[track_id])
        elif path in tokens:
            media_requests.append(tokens[path])
            if broken_audio and tokens[path] == "2":
                await route.fulfill(status=502)
            else:
                byte_range = route.request.headers.get("range")
                headers = {"Accept-Ranges": "bytes", "Content-Type": "audio/mpeg"}
                if byte_range:
                    first, _, last = byte_range.removeprefix("bytes=").partition("-")
                    start = int(first)
                    end = min(int(last) if last else len(audio_fixture) - 1, len(audio_fixture) - 1)
                    headers["Content-Range"] = f"bytes {start}-{end}/{len(audio_fixture)}"
                    await route.fulfill(status=206, headers=headers, body=audio_fixture[start:end + 1])
                else:
                    await route.fulfill(headers=headers, body=audio_fixture)
        elif path == "/api/catalog/albums/20":
            await route.fulfill(json={"id": "20", "title": "Queue album", "track_count": 3})
        elif path == "/api/catalog/albums/20/tracks":
            offset = int(params["index"][0])
            album_pages.append(offset)
            await route.fulfill(json={"data": tracks[:1] if offset == 0 else tracks[1:], "has_more": offset == 0})
        elif path in assets and route.request.url.startswith(origin):
            file, content_type = assets[path]
            await route.fulfill(path=static / file, content_type=content_type)
        else:
            await route.fulfill(status=404)

    async with async_playwright() as runtime:
        try:
            browser = await runtime.chromium.launch(channel="chrome", headless=True)
        except Error:
            pytest.skip("Optional UI test needs installed Chrome")
        try:
            page = await browser.new_page()
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            if prefetch:
                await page.add_init_script(
                    f"localStorage.setItem('musicstream.hitmos.match.v1.2', {json.dumps(saved)})")
            await page.route("**/*", route_handler)
            await page.goto(origin + "/app")
            audio = page.locator("#hitmos-audio")
            previous = page.locator("#player-previous")
            next_track = page.locator("#player-next")
            source_status = page.locator("#source-status")

            def row(index):
                return page.locator(f'.track-row[data-track-id="{index}"]:visible')

            async def playing(index):
                await expect(row(index)).to_have_class("track-row selected is-playing")
                assert await audio.evaluate("(a) => !a.paused && !a.error")

            async def search():
                await page.locator("#search-input").fill("Queue artist")
                await page.locator("#search-input").press("Enter")
                await expect(row(3)).to_be_visible()

            async def open_album():
                await page.evaluate("""() => {
                    history.pushState({}, "", "#album/20");
                    dispatchEvent(new PopStateEvent("popstate"));
                }""")
                await expect(row(1)).to_be_visible()

            async def finish_audio():
                # Seeking near the end lets Chromium deliver the real ended event.
                await page.wait_for_function("document.querySelector('#hitmos-audio').seekable.length > 0")
                await audio.evaluate("(a) => { a.currentTime = a.duration - 0.05; }")

            await expect(previous).to_be_disabled()
            await expect(next_track).to_be_disabled()
            if scenario == "album_and_failure":
                await open_album()
            else:
                await search()
            await row(1).locator("button").click()
            await playing(1)

            if scenario == "prefetch_cancelled":
                await asyncio.wait_for(pending_search.wait(), 5)
                assert media_requests == ["1"]
                await open_album()
                await page.get_by_role("button", name="Загрузить следующий фрагмент").click()
                await row(3).locator("button").click()
                await playing(3)
                release_search.set()
                await asyncio.wait_for(search_finished.wait(), 5)
                assert prefetch_requests == [("2", "12")]
                assert all(track_id != "2" for track_id, _ in lookups)
                assert media_requests == ["1", "3"]

            elif scenario == "prefetch_pending_end":
                await asyncio.wait_for(pending_search.wait(), 5)
                assert media_requests == ["1"]
                await finish_audio()
                await playing(2)
                release_search.set()
                await asyncio.wait_for(search_finished.wait(), 5)
                assert prefetch_requests == [("2", "12")]
                assert lookups[-1] == ("2", "12")
                assert [track_id for track_id, _ in groupby(media_requests)] == ["1", "2"]

            elif prefetch:
                await page.wait_for_function("document.querySelector('#hitmos-audio').currentTime > 0.6")
                assert prefetch_requests == [("2", "12")]
                assert media_requests == ["1"]
                assert await page.evaluate("localStorage.getItem('musicstream.hitmos.match.v1.2')") == saved
                if scenario == "prefetch_expired":
                    await page.evaluate("() => { const now = performance.now.bind(performance); performance.now = () => now() + 46_000; }")
                if scenario == "prefetch_next":
                    await next_track.click()
                else:
                    await row(1).locator("button").click()
                    await expect(row(1)).to_have_class("track-row selected is-paused")
                    await audio.evaluate("(a) => { a.currentTime = 1; }")
                    await row(1).locator("button").click()
                    await playing(1)
                    assert prefetch_requests == [("2", "12")]
                    await finish_audio()
                await playing(2)
                assert prefetch_requests == [("2", "12")]
                assert len(await page.locator("audio").all()) == 1
                if scenario in {"prefetch_ready", "prefetch_next"}:
                    assert all(track_id != "2" for track_id, _ in lookups)
                    assert await page.evaluate("localStorage.getItem('musicstream.hitmos.match.v1.2')") == saved
                else:
                    assert [(track_id, candidate) for track_id, candidate in lookups if track_id == "2"] == [("2", "12")]
                assert [track_id for track_id, _ in groupby(media_requests)] == ["1", "2"]

            elif scenario == "navigation":
                await finish_audio()
                await playing(2)
                await previous.click()
                await playing(1)
                await next_track.click()
                await playing(2)
                assert await audio.evaluate("(a) => a.duration > 3.2")
                await audio.evaluate("(a) => { a.currentTime = 3.2; }")
                before_restart = list(lookups)
                await previous.click()
                await page.wait_for_function("document.querySelector('#hitmos-audio').currentTime < 1")
                assert lookups == before_restart
                await next_track.click()
                await playing(3)
                await expect(next_track).to_be_disabled()
                await finish_audio()
                await expect(source_status).to_have_text("Очередь воспроизведения завершена.")
                assert await audio.evaluate("(a) => a.ended && a.paused")
                assert [track_id for track_id, _ in groupby(media_requests)] == ["1", "2", "1", "2", "3"]
                assert lookups[:4] == [("1", None), ("1", "11"), ("2", None), ("2", "12")]

            elif scenario == "late_response":
                await next_track.click()
                await asyncio.wait_for(pending_search.wait(), 5)
                assert await audio.evaluate("(a) => a.paused && !a.hasAttribute('src')")
                await row(2).locator("button").dblclick()
                await next_track.click()
                button = page.locator("#source-candidates button").first
                await expect(button).to_be_visible()
                assert media_requests == ["1"]
                release_search.set()
                await asyncio.wait_for(search_finished.wait(), 5)
                await expect(row(3)).to_have_class("track-row selected")
                before_choice = list(lookups)
                await row(3).locator("button").dblclick()
                await expect(button).to_be_visible()
                assert lookups == before_choice
                await button.click()
                await playing(3)
                assert media_requests == ["1", "3"]
                assert [entry for entry in lookups if entry[0] == "2"] == [("2", None)]

            elif scenario == "tile_toggle":
                await page.wait_for_function("document.querySelector('#hitmos-audio').currentTime > 0.4")
                original_src = await audio.evaluate("(a) => a.currentSrc")
                before_toggle = list(lookups), list(media_requests)
                await row(1).locator("button").click()
                await expect(row(1)).to_have_class("track-row selected is-paused")
                paused = await audio.evaluate("(a) => a.currentTime")
                assert paused > 0.4 and await audio.evaluate("(a) => a.paused && !a.ended")

                await open_album()
                assert await audio.evaluate("(a) => a.currentTime") == paused
                await row(1).locator("button").click()
                await playing(1)
                assert await audio.evaluate("(a) => a.currentTime") >= paused
                assert await audio.evaluate("(a) => a.currentSrc") == original_src
                assert (lookups, media_requests) == before_toggle
                await next_track.click()
                await expect(page.locator("#player-title")).to_have_text(tracks[1]["title"])
                await page.wait_for_function("a => a.src.endsWith('/' + '2'.repeat(32)) && !a.paused",
                                             arg=await audio.element_handle())
                assert album_pages == [0]

                await search()
                await row(3).locator("button").click()
                await playing(3)
                await finish_audio()
                await expect(source_status).to_have_text("Очередь воспроизведения завершена.")
                before_ended_click = list(lookups), list(media_requests)
                await row(3).locator("button").click()
                assert await audio.evaluate("(a) => a.ended && a.paused")
                assert (lookups, media_requests) == before_ended_click
                assert set(media_requests) == {"1", "2", "3"}

            else:
                await finish_audio()
                await expect(row(2)).to_have_class("track-row selected has-error")
                assert album_pages == [0, 1]
                await expect(next_track).to_be_enabled()
                await next_track.click()
                await playing(3)
                assert [track_id for track_id, _ in groupby(media_requests)] == ["1", "2", "3"]

                # Choosing a track in a new list replaces the album queue.
                search_failure = True
                broken_audio = False
                await search()
                await row(2).locator("button").click()
                await expect(row(2)).to_have_class("track-row selected has-error")
                await expect(source_status).to_contain_text("hitmos_search_http_503")
                await next_track.click()
                await playing(3)
                assert album_pages == [0, 1]
            assert not page_errors, page_errors
        finally:
            release_search.set()
            await browser.close()
