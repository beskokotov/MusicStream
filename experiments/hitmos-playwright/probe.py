"""Playwright-only Hitmos experiment.

The script uses a temporary browser context, performs ordinary UI interaction,
and records only coarse request facts. It never writes cookies, tokens, or URLs
that may contain session data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Page, Response, async_playwright


ROOT = Path(__file__).resolve().parent
BASE_URL = "https://ru.hitmox.org"
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
DEFAULT_QUERIES = ("Rammstein Sonne", "Ты не бойся ночи")
SENSITIVE_QUERY = re.compile(r"(?:token|key|sig|auth|pass|cookie|session)", re.I)


def safe_url_facts(url: str) -> dict[str, str]:
    """Retain host and a coarse path class only; omit query and exact IDs."""
    try:
        parsed = urlsplit(url)
        path = parsed.path.lower()
        if "/song/" in path:
            category = "/song/<id>"
        elif re.search(r"/get/(?:cuts|music)/", path):
            category = "/get/<audio>"
        elif path.endswith(".mp3"):
            category = "/<mp3>"
        elif path.endswith(".m3u8"):
            category = "/<hls>"
        else:
            category = path[:80] or "/"
        return {"scheme": parsed.scheme, "host": parsed.hostname or "", "path_class": category}
    except ValueError:
        return {"scheme": "", "host": "", "path_class": "invalid"}


def audio_like(url: str, resource_type: str, content_type: str = "") -> bool:
    path = urlsplit(url).path.lower()
    return resource_type in {"media", "manifest"} or any(
        marker in path or marker in content_type.lower()
        for marker in (".mp3", ".m3u8", ".mp4", "audio/", "application/vnd.apple.mpegurl")
    )


async def visible_inputs(page: Page) -> list[dict[str, str]]:
    return await page.locator("input").evaluate_all(
        "els => els.slice(0, 20).map(e => ({type:e.type || '', name:e.name || '', "
        "placeholder:e.placeholder || '', aria:e.getAttribute('aria-label') || ''}))"
    )


async def find_search_box(page: Page):
    selectors = (
        "input[type=search]", "input[name=q]", "input[name=query]",
        "input[placeholder*='Поиск' i]", "input[placeholder*='Search' i]",
    )
    for selector in selectors:
        locator = page.locator(selector).first
        if await locator.count():
            return locator
    return None


async def song_links(page: Page) -> list[dict[str, str]]:
    links = []
    for locator in await page.locator("a[href*='/song/']").all():
        href = await locator.get_attribute("href") or ""
        text = " ".join((await locator.inner_text()).split())[:180]
        if re.search(r"/song/[0-9]{1,12}/?$", href) and text:
            links.append({"facts": safe_url_facts(href), "text": text})
    return links[:20]


def candidate_score(text: str, query: str) -> int:
    wanted = [part.casefold() for part in query.split() if len(part) > 2]
    lowered = text.casefold()
    score = sum(2 for part in wanted if part in lowered)
    if re.search(r"\b(remix|live|cover|karaoke|edit|mix)\b", lowered, re.I):
        score -= 5
    return score


async def inspect_audio(page: Page) -> dict:
    return await page.evaluate(
        """() => {
          const audio = document.querySelector('audio');
          if (!audio) return {present:false};
          const source = audio.currentSrc || audio.src || '';
          return {present:true, src_kind: source.startsWith('blob:') ? 'blob' :
            (source ? 'url' : 'none'), paused: audio.paused, ready_state: audio.readyState,
            network_state: audio.networkState, current_time: audio.currentTime,
            duration: Number.isFinite(audio.duration) ? audio.duration : null,
            buffered_end: audio.buffered.length ? audio.buffered.end(audio.buffered.length - 1) : 0,
            media_source: typeof MediaSource !== 'undefined' && source.startsWith('blob:')};
        }"""
    )


def chrome_metrics() -> dict:
    """Best-effort aggregate of Chrome processes; no command lines or URLs."""
    command = "Get-Process chrome -ErrorAction SilentlyContinue | " \
              "Select-Object Id,CPU,WorkingSet64 | ConvertTo-Json -Compress"
    try:
        completed = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                                   capture_output=True, text=True, timeout=5)
        if completed.returncode:
            return {"available": False}
        raw = completed.stdout.strip()
        if not raw:
            return {"available": True, "processes": 0, "rss_mb": 0.0, "cpu_seconds": 0.0}
        rows = json.loads(raw)
        if isinstance(rows, dict):
            rows = [rows]
        return {"available": True, "processes": len(rows),
                "rss_mb": round(sum(float(item.get("WorkingSet64") or 0) for item in rows) / 1048576, 1),
                "cpu_seconds": round(sum(float(item.get("CPU") or 0) for item in rows), 2)}
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return {"available": False}


async def run_case(query: str) -> dict:
    started = time.perf_counter()
    result: dict = {"query": query, "site": BASE_URL, "status": "ERROR",
                    "requests": [], "audio_responses": [], "inputs": []}
    responses: list[Response] = []
    audio_started_at: float | None = None
    first_audio_response_at: float | None = None

    async with async_playwright() as runtime:
        browser = await runtime.chromium.launch(
            executable_path=str(CHROME) if CHROME.is_file() else None,
            headless=True, timeout=15000,
        )
        context = await browser.new_context(java_script_enabled=True, accept_downloads=False)
        page = await context.new_page()
        result["chrome_before"] = chrome_metrics()

        def remember_request(request):
            facts = safe_url_facts(request.url)
            facts.update({"resource_type": request.resource_type, "method": request.method,
                          "has_range": bool(request.headers.get("range"))})
            if audio_like(request.url, request.resource_type):
                result["requests"].append(facts)

        async def remember_response(response):
            nonlocal first_audio_response_at
            if not audio_like(response.url, response.request.resource_type,
                              response.headers.get("content-type", "")):
                return
            if audio_started_at is not None and first_audio_response_at is None:
                first_audio_response_at = time.perf_counter()
            responses.append(response)
            result["audio_responses"].append({
                **safe_url_facts(response.url), "status": response.status,
                "mime": response.headers.get("content-type", "").split(";", 1)[0],
                "has_content_range": bool(response.headers.get("content-range")),
                "accept_ranges": response.headers.get("accept-ranges", ""),
            })

        page.on("request", remember_request)
        page.on("response", lambda response: asyncio.create_task(remember_response(response)))
        page.on("console", lambda message: None)
        try:
            response = await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=20000)
            result["page_status"] = response.status if response else None
            result["page_title"] = (await page.title())[:120]
            result["inputs"] = await visible_inputs(page)
            search = await find_search_box(page)
            if search is None:
                result["status"] = "SEARCH_UI_NOT_FOUND"
                result["page_text_sample"] = (await page.locator("body").inner_text())[:500]
                return result

            search_started = time.perf_counter()
            await search.fill(query)
            await search.press("Enter")
            await page.wait_for_timeout(1500)
            links = await song_links(page)
            result["search_seconds"] = round(time.perf_counter() - search_started, 3)
            result["candidates"] = links
            if not links:
                result["status"] = "NO_SONG_LINKS"
                return result
            selected = max(links, key=lambda item: candidate_score(item["text"], query))
            result["selected"] = selected
            selected_link = page.locator("a[href*='/song/']").filter(has_text=selected["text"][:60]).first
            if not await selected_link.count():
                selected_link = page.locator("a[href*='/song/']").first
            selected_href = await selected_link.get_attribute("href") or ""
            match = re.search(r"/song/([0-9]{1,12})/?$", selected_href)
            if match:
                result["selected_song_id"] = match.group(1)
            await selected_link.click()
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
            result["song_page_status"] = await page.locator("body").count()
            result["song_page_title"] = (await page.title())[:120]
            result["song_ready_seconds"] = round(time.perf_counter() - search_started, 3)

            play_selectors = (
                "button.jp-play", ".jp-play", "button[aria-label*='play' i]",
                "button:has-text('Play')", "button:has-text('Воспроизвести')",
                "[role=button][aria-label*='play' i]",
            )
            play = None
            for selector in play_selectors:
                candidate = page.locator(selector).first
                if await candidate.count() and await candidate.is_visible():
                    play = candidate
                    break
            if play is None:
                result["status"] = "PLAY_BUTTON_NOT_FOUND"
                result["audio_state"] = await inspect_audio(page)
                return result
            audio_started_at = time.perf_counter()
            await play.click(timeout=5000)
            result["play_clicked"] = True
            try:
                await page.wait_for_function(
                    """() => { const a = document.querySelector('audio');
                    return a && !a.paused && a.currentTime > 0.1; }""",
                    timeout=10000,
                )
                result["play_start_seconds"] = round(time.perf_counter() - audio_started_at, 3)
            except Exception:
                result["play_start_seconds"] = None
            if first_audio_response_at is not None:
                result["first_audio_response_seconds"] = round(first_audio_response_at - audio_started_at, 3)
            await page.wait_for_timeout(2000)
            audio_state = await inspect_audio(page)
            result["audio_state"] = audio_state
            result["chrome_after"] = chrome_metrics()
            before = result["chrome_before"]
            after = result["chrome_after"]
            if before.get("available") and after.get("available"):
                result["chrome_delta"] = {
                    "processes": after["processes"] - before["processes"],
                    "rss_mb": round(after["rss_mb"] - before["rss_mb"], 1),
                    "cpu_seconds": round(after["cpu_seconds"] - before["cpu_seconds"], 2),
                }
            result["observe_after_play_seconds"] = round(time.perf_counter() - audio_started_at, 3)
            result["audio_request_count"] = len(result["requests"])
            result["audio_response_count"] = len(result["audio_responses"])
            started_audio = any(item["status"] in (200, 206) and
                                (item["mime"].startswith("audio/") or item["path_class"] == "/<mp3>")
                                for item in result["audio_responses"])
            playing = bool(audio_state.get("present") and not audio_state.get("paused") and
                           (audio_state.get("current_time") or 0) > 0)
            if playing and started_audio:
                result["status"] = "PLAY_STARTED"
            elif started_audio:
                result["status"] = "AUDIO_REQUESTED_NOT_PLAYING"
            else:
                result["status"] = "NO_AUDIO_RESPONSE"
            result["delivery"] = "MP3" if any(item["mime"] == "audio/mpeg" for item in result["audio_responses"]) else \
                "HLS" if any(item["path_class"] == "/<hls>" for item in result["audio_responses"]) else \
                "MediaSource/blob" if audio_state.get("media_source") else "unknown"
        except Exception as exc:
            result["status"] = "ERROR"
            result["error_type"] = type(exc).__name__
            result["error_message"] = str(exc)[:180]
        finally:
            result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
            await context.close()
            await browser.close()
    return result


async def main(queries: tuple[str, ...]) -> None:
    output = []
    for query in queries:
        row = await run_case(query)
        output.append(row)
        print(json.dumps({"query": query, "status": row["status"],
                          "search_seconds": row.get("search_seconds"),
                          "play_start_seconds": row.get("play_start_seconds"),
                          "first_audio_response_seconds": row.get("first_audio_response_seconds"),
                          "audio_response_count": row.get("audio_response_count"),
                          "delivery": row.get("delivery")}, ensure_ascii=False), flush=True)
    (ROOT / "results.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n",
                                        encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("queries", nargs="*", default=DEFAULT_QUERIES)
    args = parser.parse_args()
    asyncio.run(main(tuple(args.queries)))
