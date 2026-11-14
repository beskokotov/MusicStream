import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("playwright.async_api")

from playwright.async_api import async_playwright

from musicstream.audio_providers.hitmos import HitmosProvider
from test_hitmos_discovery import REF, Reader


async def main():
    provider = HitmosProvider(Reader())
    automatic = (await provider.find(REF)).model_dump()
    ambiguous = (await HitmosProvider(Reader({"1": ("Sonne", "Rammstein", None),
                                             "2": ("Sonne", "Rammstein", None)})).find(REF)).model_dump()
    unavailable = (await HitmosProvider(Reader(error="source_http_error")).find(REF)).model_dump()
    scenario = [automatic]
    requests = []
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
                    return await route.fulfill(json=[{"id": "1", "title": "Sonne", "artist": "Rammstein",
                        "duration_seconds": 272, "explicit_lyrics": False, "source": "deezer"}])
                if "/api/audio/resolve?" in url:
                    requests.append(url)
                    if "hitmos_id=" in url:
                        song_id = "2" if "hitmos_id=2" in url else "1"
                        return await route.fulfill(json=(await provider.lookup(REF, song_id)).model_dump())
                    return await route.fulfill(json=scenario[0])
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
            await page.wait_for_function("document.querySelector('#player-play').disabled === false")
            assert len(requests) == 1 and "hitmos_id=" not in requests[0]
            assert await page.locator("#audio-source").input_value() == "auto"
            assert await page.locator("#hitmos-advanced summary").inner_text() == "Дополнительно / Диагностика"
            assert not await page.locator("#hitmos-advanced").evaluate("e => e.open")
            assert not await page.locator("#hitmos-url").is_visible()
            assert await page.locator("#hitmos-audio").evaluate("e => !e.getAttribute('src')")
            await page.locator("#player-play").click()
            await page.wait_for_function("document.querySelector('#hitmos-audio').hasAttribute('src')")
            assert "hitmos_id=1" in requests[-1]
            # This deliberately blocked fixture must never claim real playback.
            await page.wait_for_timeout(150)
            assert "Играет" not in await page.locator("#playback-state").inner_text()

            scenario[0] = ambiguous
            await page.locator(".track-title").click()
            await page.wait_for_function("document.querySelectorAll('#source-candidates .source-candidate').length === 2")
            assert await page.locator("#player-play").is_disabled()
            assert await page.locator("#source-candidates button").count() == 2
            assert "Выберите запись" in await page.locator("#playback-state").inner_text()

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
            assert not errors, errors
            print("BROWSER PASS: automatic discovery on selection, fresh resolve on Play, ambiguous selection, source error fallback, advanced manual input, source switching, responsive layout, no fake playback")
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
                            result = await service.discover(REF, allow_youtube=params.get("allow_youtube") == ["true"])
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

                await page.locator("#youtube-consent").check()
                await page.wait_for_function("!document.querySelector('#player-play').disabled")
                assert calls[-1]["allow_youtube"] == ["true"]
                assert await page.locator("#source-candidates .source-candidate").count() == 1
                assert await page.evaluate("!window.YT")  # Metadata prefetch does not load the player.
                await page.locator("#player-play").click()
                await page.wait_for_function("window.testYT && window.testYT.plays === 1")
                assert calls[-1]["candidate_id"] == ["abcdefghijk"]
                assert calls[-1]["provider"] == ["youtube"]
                assert await page.locator("#youtube-frame-area").is_visible()
                assert "Играет" not in await page.locator("#playback-state").inner_text()

                await page.locator("#youtube-consent").uncheck()
                await page.wait_for_function("document.querySelectorAll('#source-reports p').length === 2")
                assert await page.evaluate("window.testYT.destroyed === 1")
                assert not await page.locator("#youtube-frame-area").is_visible()
                assert await page.locator("#player-play").is_disabled()
                assert not errors, errors
            finally:
                await browser.close()
    asyncio.run(check())
