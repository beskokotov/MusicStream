import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("playwright.async_api")

from playwright.async_api import async_playwright

from musicstream.audio_providers.hitmos import HitmosProvider
from musicstream.audio_providers.service import SourceSelectionService
from test_hitmos_discovery import REF, Reader


async def main():
    provider = HitmosProvider(Reader({
        "1": ("Sonne", "Rammstein", 272),
        "2": ("Sonne", "Rammstein", 272),
    }))
    automatic = (await SourceSelectionService({"hitmos": provider}).discover(REF)).model_dump()
    ambiguous = (await HitmosProvider(Reader({"1": ("Sonne", "Rammstein", None),
                                             "2": ("Sonne", "Rammstein", None)})).find(REF)).model_dump()
    unavailable = (await HitmosProvider(Reader(error="source_http_error")).find(REF)).model_dump()
    missing_duration_provider = HitmosProvider(Reader({"2": ("Sonne", "Rammstein", None)}))
    missing_duration = (await SourceSelectionService({"hitmos": missing_duration_provider}).discover(REF)).model_dump()
    explicit_provider = HitmosProvider(Reader({"2": ("Sonne", "Rammstein", 272)}))
    explicit_service = SourceSelectionService({"hitmos": explicit_provider})
    sequential_provider = HitmosProvider(Reader({
        "4": ("Sonne", "Rammstein", 272),
        "5": ("Sonne", "Rammstein", 274),
        "6": ("Sonne", "Rammstein", 271),
        "7": ("Sonne", "Rammstein", 275),
        "8": ("Sonne", "Rammstein", None),
        "9": ("Sonne Live", "Rammstein", 272),
    }))
    sequential = (await SourceSelectionService({"hitmos": sequential_provider}).discover(REF)).model_dump()
    sequential.update(selection="choose", selected_id=None, selected_source=None)
    mismatch = (await sequential_provider.lookup(REF, "9")).candidates[0]
    sequential["candidates"].append(mismatch.model_copy(update={"source": "hitmos"}).model_dump())
    sequential["candidates"].reverse()
    scenario = [automatic]
    requests = []
    serve_audio = False
    omit_duration = False
    explicit_track = False
    sequential_track = False
    sequential_success = False
    budget_stage = None
    audio_token = "/api/audio/hitmos-stream/" + "A" * 32
    broken_audio_token = "/api/audio/hitmos-stream/" + "B" * 32
    async with async_playwright() as runtime:
        try:
            browser = await runtime.chromium.launch(channel="chrome", headless=True)
        except Exception:
            pytest.skip("Optional UI test needs installed Chrome")
        try:
            page = await browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            timings = []
            page.on("console", lambda message: timings.append(message.args[1])
                    if message.text.startswith("MusicStream Auto timing ") and len(message.args) > 1 else None)

            async def route_handler(route):
                url = route.request.url
                if "/api/search?" in url:
                    return await route.fulfill(json=[{"id": "1", "title": "Sonne", "artist": "Rammstein",
                        "duration_seconds": 272, "explicit_lyrics": explicit_track, "source": "deezer"}])
                if "/api/audio/resolve?" in url:
                    requests.append(url)
                    params = parse_qs(urlsplit(url).query)
                    if budget_stage == ("source_metadata" if "hitmos_id" in params else "source_search"):
                        return await route.fulfill(json={"source": "hitmos", "status": "error",
                            "reason": "request_budget_exhausted", "stage": budget_stage,
                            "retry_after_seconds": 17, "candidates": []})
                    if "hitmos_id" in params:
                        song_id = params["hitmos_id"][0]
                        if sequential_track:
                            if song_id == "4" or (song_id == "5" and not sequential_success):
                                return await route.fulfill(status=503)
                            prepared = await sequential_provider.lookup(REF, song_id)
                            prepared.candidates[0].stream_url = broken_audio_token if song_id == "6" else audio_token
                            return await route.fulfill(json=prepared.model_dump())
                        if song_id == "1":
                            return await route.abort("failed")
                        source_provider = (explicit_provider if explicit_track else
                                           missing_duration_provider if omit_duration else provider)
                        prepared = await source_provider.lookup(REF.model_copy(update={"explicit_lyrics": explicit_track}), song_id)
                        if serve_audio:
                            prepared.candidates[0].stream_url = audio_token
                        return await route.fulfill(json=prepared.model_dump())
                    if explicit_track:
                        ranked = await explicit_service.discover(REF.model_copy(update={
                            "explicit_lyrics": True,
                            "allow_unknown_hitmos_explicit": params.get("allow_unknown_hitmos_explicit") == ["true"],
                        }))
                        return await route.fulfill(json=ranked.model_dump())
                    if serve_audio:
                        await asyncio.sleep(.160)
                    return await route.fulfill(json=scenario[0])
                if serve_audio and url.endswith(audio_token):
                    fixture = Path(__file__).resolve().parents[1] / "src/musicstream/static/audio-transport-test.mp3"
                    return await route.fulfill(path=fixture, content_type="audio/mpeg")
                if not url.startswith("http://127.0.0.1:8017/"):
                    return await route.abort()
                resources = {
                    "/app": ("index.html", "text/html"),
                    "/static/app.js": ("app.js", "application/javascript"),
                    "/static/style.css": ("style.css", "text/css"),
                    "/static/config.js": ("config.js", "application/javascript"),
                }
                resource = resources.get(url.removeprefix("http://127.0.0.1:8017"))
                if not resource:
                    return await route.abort()
                path = Path(__file__).resolve().parents[1] / "src/musicstream/static" / resource[0]
                await route.fulfill(body=path.read_bytes(), content_type=resource[1])

            await page.route("**/*", route_handler)
            await page.goto("http://127.0.0.1:8017/app")
            await page.locator("#search-input").fill("Sonne")
            await page.locator("#search-input").press("Enter")
            await page.locator(".track-title").click()
            # Selecting a tile now starts strict Hitmos Auto Play immediately.
            await page.wait_for_function("document.querySelector('.track-row').classList.contains('has-error')")
            assert await page.locator("#playback-section").get_attribute("aria-busy") == "false"
            assert await page.locator("#audio-source").input_value() == "auto"
            assert not await page.locator("#hitmos-advanced").evaluate("e => e.open")
            assert not await page.locator("#hitmos-url").is_visible()
            assert len(requests) == 3
            assert "provider=auto" in requests[0] and "allow_youtube=false" in requests[0]
            assert "hitmos_id=1" in requests[1] and "provider=hitmos" in requests[1]
            assert "hitmos_id=2" in requests[2] and "provider=hitmos" in requests[2]
            # The stubbed CDN request is blocked; the UI must not report playback.
            await page.wait_for_timeout(150)
            assert not await page.locator(".track-row").evaluate("e => e.classList.contains('is-playing')")

            scenario[0] = ambiguous
            await page.locator(".track-title").click()
            await page.wait_for_function("document.querySelectorAll('#source-candidates .source-candidate').length === 2")
            assert await page.locator("#playback-section").get_attribute("aria-busy") == "false"
            assert len(requests) == 4  # Ambiguous matches are shown without resolving either stream.
            assert await page.locator("#player-play").is_disabled()
            assert await page.locator("#source-candidates button").count() == 2
            assert not await page.locator(".track-row").evaluate("e => e.classList.contains('is-playing')")
            await page.locator("#source-candidates button").first.click()
            await page.wait_for_function("document.querySelector('.track-row').classList.contains('has-error')")
            assert "hitmos_id=1" in requests[-1]

            scenario[0] = unavailable
            await page.locator(".track-title").click()
            await page.wait_for_function("document.querySelector('#source-status').textContent.length > 0")
            assert await page.locator("#player-play").is_disabled()
            await page.locator("#hitmos-advanced summary").click()
            assert await page.locator("#hitmos-url").is_visible()

            scenario[0] = automatic
            await page.locator("#audio-source").select_option("youtube")
            assert not await page.locator("#hitmos-advanced").is_visible()
            assert await page.locator(".youtube-consent").is_visible()
            await page.locator("#audio-source").select_option("hitmos")
            await page.wait_for_function("document.querySelector('#player-play').disabled === false")
            for width in (320, 390, 1280):
                await page.set_viewport_size({"width": width, "height": 900})
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")

            async def select_after_reload():
                await page.goto("http://127.0.0.1:8017/app")
                await page.locator("#search-input").fill("Sonne")
                await page.locator("#search-input").press("Enter")
                await page.locator(".track-title").click()

            async def wait_for_playing():
                await page.wait_for_function("document.querySelector('.track-row').classList.contains('is-playing')")
                reports = [await entry.json_value() for entry in timings]
                return next(report["play_to_playing_ms"] for report in reversed(reports)
                            if "play_to_playing_ms" in report)

            scenario[0] = automatic
            serve_audio = True
            timings.clear()
            await select_after_reload()
            without_saved = await wait_for_playing()
            key = "musicstream.hitmos.match.v1.1"
            stored = json.loads(await page.evaluate("(key) => localStorage.getItem(key)", key))
            assert stored["songId"] == "2" and stored["artist"] == "rammstein"
            assert set(stored) == {"songId", "title", "artist", "duration", "sourceDuration", "explicit"}
            requests.clear()
            timings.clear()
            scenario[0] = ambiguous
            await select_after_reload()
            with_saved = await wait_for_playing()
            assert len(requests) == 1 and "hitmos_id=2" in requests[0]
            print(f"Fixture play_to_playing_ms: search={without_saved}, saved={with_saved}")

            serve_audio = False
            scenario[0] = automatic
            requests.clear()
            await select_after_reload()
            await page.wait_for_function("document.querySelector('.track-row').classList.contains('has-error')")
            assert "hitmos_id=2" in requests[0]
            assert "provider=auto" in requests[1] and "exclude_hitmos_id=2" in requests[1]
            assert await page.evaluate("(key) => localStorage.getItem(key)", key) is None

            serve_audio = True
            scenario[0] = ambiguous
            await select_after_reload()
            await page.wait_for_function("document.querySelectorAll('#source-candidates .source-candidate').length === 2")
            await page.locator("#hitmos-advanced summary").click()
            await page.locator("#hitmos-url").fill("https://ru.hitmox.org/song/2")
            await page.locator("#hitmos-manual-form button").click()
            await page.locator("#source-candidates button").click()
            await page.wait_for_function("document.querySelector('.track-row').classList.contains('is-playing')")
            assert json.loads(await page.evaluate("(key) => localStorage.getItem(key)", key))["songId"] == "2"

            omit_duration = True
            requests.clear()
            timings.clear()
            await select_after_reload()
            await wait_for_playing()
            assert len(requests) == 1 and "hitmos_id=2" in requests[0]

            await page.evaluate("(key) => localStorage.removeItem(key)", key)
            scenario[0] = missing_duration
            requests.clear()
            timings.clear()
            await select_after_reload()
            await wait_for_playing()
            assert len(requests) == 2
            assert "provider=auto" in requests[0] and "hitmos_id=2" in requests[1]

            explicit_track = True
            await select_after_reload()
            choice = page.get_by_role("button", name="Подтвердить запись и воспроизвести")
            await choice.wait_for()
            preference = page.locator("#hitmos-unknown-explicit")
            assert not await preference.is_checked()
            await preference.check()
            timings.clear()
            await select_after_reload()
            await wait_for_playing()
            assert await preference.is_checked()
            assert json.loads(await page.evaluate("(key) => localStorage.getItem(key)", key))["explicit"] is False
            if not await preference.is_visible():
                await page.locator("#developer-mode summary").first.click()
            await preference.uncheck()
            await select_after_reload()
            await choice.click()
            await page.wait_for_function("document.querySelector('.track-row').classList.contains('is-playing')")
            assert json.loads(await page.evaluate("(key) => localStorage.getItem(key)", key))["user_confirmed"] is True
            requests.clear()
            timings.clear()
            await select_after_reload()
            await wait_for_playing()
            assert len(requests) == 1 and "hitmos_id=2" in requests[0]

            explicit_track = False
            sequential_track = True
            scenario[0] = sequential
            await page.evaluate("(key) => localStorage.removeItem(key)", key)
            await select_after_reload()
            await page.locator("#source-candidates .source-candidate").first.wait_for()
            sequence_preference = page.locator("#hitmos-try-matching-candidates")
            assert not await sequence_preference.is_checked()
            await sequence_preference.check()

            def attempted_ids():
                return [parse_qs(urlsplit(url).query).get("hitmos_id", [None])[0] for url in requests]

            requests.clear()
            await select_after_reload()
            await page.wait_for_function("document.querySelector('.track-row').classList.contains('has-error')")
            assert attempted_ids() == [None, "4", "6", "5"]
            assert await page.evaluate("(key) => localStorage.getItem(key)", key) is None

            sequential_success = True
            requests.clear()
            timings.clear()
            await select_after_reload()
            await wait_for_playing()
            assert attempted_ids() == [None, "4", "6", "5"]
            assert await sequence_preference.is_checked()
            stored = json.loads(await page.evaluate("(key) => localStorage.getItem(key)", key))
            assert stored["songId"] == "5" and not stored.get("user_confirmed")

            budget_stage = "source_metadata"
            requests.clear()
            await select_after_reload()
            await page.get_by_text("Повторите попытку через 17 с.", exact=False).wait_for()
            assert attempted_ids() == ["5"]
            assert json.loads(await page.evaluate("(key) => localStorage.getItem(key)", key)) == stored
            await page.evaluate("(key) => localStorage.removeItem(key)", key)
            requests.clear()
            await select_after_reload()
            await page.get_by_text("Повторите попытку через 17 с.", exact=False).wait_for()
            assert attempted_ids() == [None, "4"]

            budget_stage = "source_search"
            requests.clear()
            await select_after_reload()
            await page.get_by_text("Повторите попытку через 17 с.", exact=False).wait_for()
            assert attempted_ids() == [None]
            assert not errors, errors
            print("BROWSER PASS: automatic discovery on selection, one-click Auto Play, ambiguous selection, source error fallback, advanced manual input, source switching, responsive layout, no fake playback")
        finally:
            await browser.close()


def test_discovery_interface_without_network():
    asyncio.run(main())


def test_multi_source_interface_without_network():
    async def check():
        from urllib.parse import parse_qs, urlsplit
        from musicstream.audio_providers.service import SourceSelectionService
        from test_source_service import REF, Provider, item

        service = SourceSelectionService({
            "hitmos": Provider("hitmos", status="ok", reason="no_results"),
            "youtube": Provider("youtube", [item("youtube")]),
        })
        calls = []
        async with async_playwright() as runtime:
            try:
                browser = await runtime.chromium.launch(channel="chrome", headless=True)
            except Exception:
                pytest.skip("Optional UI test needs installed Chrome")
            try:
                page = await browser.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))

                async def route_handler(route):
                    url = route.request.url
                    if "/api/search?" in url:
                        return await route.fulfill(json=[{"id": "1", **REF.model_dump(), "source": "deezer"}])
                    if "/api/audio/resolve?" in url:
                        params = parse_qs(urlsplit(url).query)
                        calls.append(params)
                        if "candidate_id" in params:
                            result = await service.prepare(REF, params["provider"][0], params["candidate_id"][0])
                        else:
                            provider = params.get("provider", ["auto"])[0]
                            result = await service.discover(REF, allow_youtube=(provider == "youtube" or params.get("allow_youtube") == ["true"]))
                        return await route.fulfill(json=result.model_dump())
                    if url == "https://www.youtube.com/iframe_api":
                        # The mock emits readiness only; it must not claim real playback.
                        return await route.fulfill(content_type="application/javascript", body="""
                            window.testYT = { cues: 0, plays: 0, destroyed: 0 };
                            window.YT = { Player: class {
                              constructor(host, options) { this.options = options; setTimeout(() => options.events.onReady({target:this}), 0); }
                              setVolume() {}
                              cueVideoById() { window.testYT.cues++; }
                              playVideo() { window.testYT.plays++; }
                              pauseVideo() {}
                              destroy() { window.testYT.destroyed++; }
                              getPlayerState() {return 5;}
                              getCurrentTime() {return 0;}
                              getDuration() {return 272;}
                            }};
                            window.onYouTubeIframeAPIReady();
                        """)
                    resources = {
                        "http://127.0.0.1:8017/app": ("index.html", "text/html"),
                        "http://127.0.0.1:8017/static/app.js": ("app.js", "application/javascript"),
                        "http://127.0.0.1:8017/static/style.css": ("style.css", "text/css"),
                        "http://127.0.0.1:8017/static/config.js": ("config.js", "application/javascript"),
                    }
                    resource = resources.get(url)
                    if not resource:
                        return await route.abort()
                    path = Path(__file__).resolve().parents[1] / "src/musicstream/static" / resource[0]
                    await route.fulfill(body=path.read_bytes(), content_type=resource[1])

                await page.route("**/*", route_handler)
                await page.goto("http://127.0.0.1:8017/app")
                await page.locator("#search-input").fill("Sonne")
                await page.locator("#search-input").press("Enter")
                await page.locator(".track-title").click()
                await page.wait_for_function("document.querySelectorAll('#source-reports p').length === 2")
                assert calls[-1]["provider"] == ["auto"]
                assert calls[-1]["allow_youtube"] == ["false"]
                assert service.providers["youtube"].calls == 0
                assert await page.locator("#player-play").is_disabled()
                assert not await page.locator("#youtube-url").is_visible()

                await page.locator("#hitmos-advanced summary").click()
                await page.locator("#audio-source").select_option("youtube")
                assert await page.locator(".youtube-consent").is_visible()
                await page.locator("#youtube-consent").check()
                await page.wait_for_function("document.querySelectorAll('#source-candidates .source-candidate').length === 1")
                assert calls[-1]["provider"] == ["youtube"]
                assert service.providers["youtube"].calls == 1
                assert await page.evaluate("!window.YT")  # Discovery does not load the player.
                await page.locator("#source-candidates button").click()
                await page.wait_for_function("window.testYT && window.testYT.plays === 1")
                assert calls[-1]["candidate_id"] == ["abcdefghijk"]
                assert calls[-1]["provider"] == ["youtube"]
                assert await page.locator("#youtube-frame-area").is_visible()
                assert not await page.locator(".track-row").evaluate("e => e.classList.contains('is-playing')")

                await page.locator("#youtube-consent").uncheck()
                assert await page.locator("#source-reports p").count() == 0
                assert await page.evaluate("window.testYT.destroyed === 1")
                assert not await page.locator("#youtube-frame-area").is_visible()
                assert await page.locator("#player-play").is_disabled()
                assert not errors, errors
            finally:
                await browser.close()
    asyncio.run(check())
