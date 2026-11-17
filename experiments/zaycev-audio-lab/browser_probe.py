"""Public-session Playwright probe for Zaycev's normal web playback UI.

Uses a fresh unauthenticated browser context. Logs only allowlisted request
metadata (never cookie values, tokens, signed URL queries, or response bodies).
Does not bypass challenges, click download controls, or save media. The optional
--play action uses the site's normal Play control; the site may answer an open
Range with the complete track, so do not repeat it when full transfer is out
of scope.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


HOME = "https://zaycev.net/"
TIMEOUT_MS = 45_000
MAX_EVENTS = 160


def safe_path(url: str) -> str:
    parts = urlsplit(url)
    # Drop query and fragment: playback URLs may carry short-lived signatures.
    return f"{parts.hostname}{parts.path}"


def safe_location(url: str, *, media: bool = False) -> str:
    parts = urlsplit(url)
    host = parts.hostname or "unknown-host"
    if media or host != "zaycev.net":
        return f"{host}/<media-path-redacted>"
    path = re.sub(r"(/api/external/track/play/)[^/]+", r"\1<opaque-id>", parts.path)
    return f"{host}{path}"


def safe_text(value: str, limit: int = 140) -> str:
    return re.sub(r"\s+", " ", value or "").strip()[:limit]


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="Rammstein Sonne")
    parser.add_argument("--executable", help="Optional installed Chrome/Chromium path when Playwright's browser bundle is absent")
    parser.add_argument("--inspect-only", action="store_true", help="Inspect public home page without searching or playing")
    parser.add_argument("--skip-search", action="store_true", help="Keep the homepage open for the ordinary public-track Play test")
    parser.add_argument("--play", action="store_true", help="Activate site Play; may cause the browser to transfer a complete track")
    args = parser.parse_args()

    events: list[dict] = []
    request_started: dict[int, float] = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=args.executable)
        context = browser.new_context(accept_downloads=False)
        page = context.new_page()

        def on_request(request):
            if len(events) >= MAX_EVENTS:
                return
            url = urlsplit(request.url)
            request_started[id(request)] = time.perf_counter()
            try:
                headers = request.all_headers()
            except Exception:
                headers = {}
            credential_headers = sorted(key for key in headers if any(name in key.lower() for name in ("cookie", "authorization", "token", "session")))
            path = url.path.lower()
            if (request.resource_type == "media" or any(value in path for value in (
                    "/api/", "/external/", "/track/play", ".mp3", ".m4a", ".aac", ".ogg", ".m3u8", ".mpd"))):
                events.append({
                    "kind": "request",
                    "method": request.method,
                    "location": safe_location(request.url, media=request.resource_type == "media"),
                    "resource_type": request.resource_type,
                    "cookie_header_present": "cookie" in headers,
                    "credential_header_names": credential_headers,
                    "range_header_present": "range" in headers,
                })

        def on_response(response):
            if len(events) >= MAX_EVENTS:
                return
            url = urlsplit(response.url)
            path = url.path.lower()
            try:
                headers = response.all_headers()
            except Exception:
                headers = {}
            content_type = headers.get("content-type", "").split(";", 1)[0]
            if (response.request.resource_type == "media" or any(value in path for value in ("/api/", "/external/", "/track/play", ".mp3", ".m4a", ".aac", ".ogg", ".m3u8", ".mpd"))
                    or content_type.startswith("audio/") or content_type in {"application/vnd.apple.mpegurl", "application/dash+xml"}):
                events.append({
                    "kind": "response",
                    "method": response.request.method,
                    "request_to_headers_ms": round((time.perf_counter() - request_started.get(id(response.request), time.perf_counter())) * 1000),
                    "status": response.status,
                    "location": safe_location(response.url, media=(response.request.resource_type == "media" or content_type.startswith("audio/") or content_type in {"application/vnd.apple.mpegurl", "application/dash+xml"})),
                    "content_type": content_type or None,
                    "content_length": headers.get("content-length"),
                    "accept_ranges": headers.get("accept-ranges"),
                    "content_range": headers.get("content-range"),
                    "redirected_from": safe_location(response.request.redirected_from.url, media=True) if response.request.redirected_from else None,
                    "cookie_set_present": "set-cookie" in headers,
                })

        page.on("request", on_request)
        page.on("response", on_response)

        def on_request_failed(request):
            if request.resource_type == "media" and len(events) < MAX_EVENTS:
                events.append({"kind": "request_failed", "method": request.method,
                               "location": safe_location(request.url, media=True),
                               "failure": safe_text(request.failure or "unknown", 120)})

        page.on("requestfailed", on_request_failed)

        try:
            response = page.goto(HOME, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
            page.wait_for_timeout(5000 if args.inspect_only else 1800)
            print(json.dumps({
                "stage": "home",
                "status": response.status if response else None,
                "title": safe_text(page.title()),
                "path": safe_path(page.url),
                "body": safe_text(page.locator("body").inner_text(timeout=5000), 700),
                "inputs": page.locator("input").evaluate_all("els => els.slice(0,30).map(e => ({type:e.type, name:e.name, placeholder:e.placeholder, aria:e.getAttribute('aria-label')}))"),
                "search_control": page.locator("input[name=query_search]").evaluate("e => ({formAction:e.form?.action, formMethod:e.form?.method, buttons:[...(e.form?.querySelectorAll('button')||[])].map(b=>({text:(b.innerText||'').trim(),aria:b.getAttribute('aria-label'),title:b.title}))})"),
                "track_buttons": page.locator('button[aria-label="Начать воспроизведение трека"]').evaluate_all("els => els.slice(0,8).map(e => {let n=e,a=[];for(let i=0;i<7&&n;i++,n=n.parentElement){let r=n.getBoundingClientRect(),s=getComputedStyle(n);a.push({tag:n.tagName,class:n.className,visible:!!(r.width&&r.height),display:s.display,visibility:s.visibility,opacity:s.opacity,text:(n.innerText||'').trim().slice(0,120),rect:{x:r.x,y:r.y,w:r.width,h:r.height}})}return a})"),
                "home_audio_element_count": page.locator("audio").count(),
                "home_audio_elements": page.locator("audio").evaluate_all("els => els.map(a => ({paused:a.paused,readyState:a.readyState,networkState:a.networkState,duration:Number.isFinite(a.duration)?a.duration:null,currentTime:a.currentTime,preload:a.preload,src:a.currentSrc?new URL(a.currentSrc).hostname+'/<media-path-redacted>':null}))"),
                "session_cookie_names": [{"name": c["name"], "domain": c["domain"], "httpOnly": c["httpOnly"], "secure": c["secure"], "sameSite": c["sameSite"]} for c in context.cookies() if any(domain in c.get("domain", "") for domain in ("zaycev", "yandex"))],
                "buttons": page.locator("button").evaluate_all("els => els.slice(0,40).map(e => ({text:(e.innerText||'').trim().slice(0,80), aria:e.getAttribute('aria-label'), title:e.getAttribute('title')}))"),
            }, ensure_ascii=False))

            if args.inspect_only:
                print("network_events=" + json.dumps(events, ensure_ascii=False, separators=(",", ":")))
                return

            # Prefer an input whose accessible hints indicate the visible site search.
            search_candidates = page.locator("input").evaluate_all("els => els.map((e,i) => ({i,type:e.type, name:e.name, placeholder:e.placeholder, aria:e.getAttribute('aria-label')}))")
            idx = next((item["i"] for item in search_candidates if item.get("type") != "hidden" and re.search(r"search|поиск|query", " ".join(str(item.get(k) or "") for k in ("name", "placeholder", "aria")), re.I)), None)
            if args.skip_search:
                print("search_action=skipped_for_homepage_play_test")
            elif idx is None:
                print("search_action=not_attempted_no_search_input")
            else:
                search = page.locator("input").nth(idx)
                search.fill(args.query)
                submit = search.locator("xpath=ancestor::form").locator("button[type=submit],button").first
                submit.click(timeout=5000)
                page.wait_for_timeout(300)
                find_button = page.get_by_text("Найти", exact=True).last
                if find_button.count() and find_button.is_visible():
                    find_button.click(timeout=3000)
                page.wait_for_timeout(3000)
                title_hits = page.get_by_text(re.compile(re.escape(args.query.split()[-1]), re.I)).evaluate_all(
                    "els => els.slice(0,10).map(e => ({tag:e.tagName, text:(e.innerText||'').trim().slice(0,120), parent:e.parentElement?.tagName, parentText:(e.parentElement?.innerText||'').trim().slice(0,180), buttons:[...(e.closest('article,li,[role=article]')||e.parentElement?.parentElement||document.body).querySelectorAll('button')].slice(0,5).map(b=>({text:(b.innerText||'').trim().slice(0,50),aria:b.getAttribute('aria-label'),title:b.title}))}))"
                )
                print(json.dumps({
                    "stage": "search",
                    "title": safe_text(page.title()),
                    "path": safe_path(page.url),
                    "body": safe_text(page.locator("body").inner_text(timeout=5000), 1000),
                    "matching_elements": title_hits,
                    "audio_elements": page.locator("audio").count(),
                    "video_elements": page.locator("video").count(),
                }, ensure_ascii=False))

            if not args.play:
                print("play_action=not_attempted; pass --play only if a complete-track transfer is acceptable")
                print("network_events=" + json.dumps(events, ensure_ascii=False, separators=(",", ":")))
                return

                # Activate the site's own track Play control. The card's image
                # overlay intercepts pointer clicks in headless Chrome, so use
                # the control's normal keyboard activation after hover.
            all_play_buttons = page.locator('button[aria-label="Начать воспроизведение трека"]')
            play_buttons = page.locator('button[aria-label="Начать воспроизведение трека"]')
            print("play_button_summary=" + json.dumps(all_play_buttons.evaluate_all("els => els.slice(0,20).map(e => {let r=e.getBoundingClientRect(),n=e.parentElement;return {visible:!!(r.width&&r.height),rect:{w:r.width,h:r.height},text:(n?.innerText||'').trim().slice(0,120)}})"), ensure_ascii=False))
            if play_buttons.count() == 0:
                print("play_action=not_attempted_no_public_track_button")
            else:
                first_button = play_buttons.first
                track_context = first_button.evaluate("e => {let n=e; let a=[]; for(let i=0;i<5&&n;i++,n=n.parentElement)a.push({tag:n.tagName,role:n.getAttribute('role'),text:(n.innerText||'').trim().slice(0,180)}); return a}")
                before_events = len(events)
                card = first_button.locator("xpath=ancestor::*[3]")
                card.scroll_into_view_if_needed(timeout=5000)
                card.hover(timeout=5000)
                page.wait_for_timeout(250)
                print("hover_play_button=" + json.dumps(first_button.evaluate("e => {let r=e.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2,n=document.elementFromPoint(x,y);return {visible:!!(r.width&&r.height),rect:{x:r.x,y:r.y,w:r.width,h:r.height},topAtCenter:n?{tag:n.tagName,class:n.className,qa:n.getAttribute('data-qa')}:null,buttonZ:getComputedStyle(e).zIndex,topZ:n?getComputedStyle(n).zIndex:null}}"), ensure_ascii=False))
                first_button.focus(timeout=5000)
                first_button.press("Enter", timeout=5000)
                page.wait_for_timeout(6500)
                audio_state = page.locator("audio").evaluate_all("els => els.map(a => ({src:a.currentSrc ? new URL(a.currentSrc).hostname + '/<media-path-redacted>' : null, paused:a.paused, readyState:a.readyState, networkState:a.networkState, duration:Number.isFinite(a.duration)?a.duration:null, currentTime:a.currentTime, buffered:a.buffered.length, canPlayMp3:a.canPlayType('audio/mpeg'), canPlayAac:a.canPlayType('audio/mp4')}))")
                video_state = page.locator("video").evaluate_all("els => els.map(v => ({src:v.currentSrc ? new URL(v.currentSrc).hostname + '/<media-path-redacted>' : null, paused:v.paused, readyState:v.readyState}))")
                print(json.dumps({
                    "stage": "play",
                    "clicked": "Начать воспроизведение трека",
                    "track_context": track_context,
                    "audio_elements": audio_state,
                    "video_elements": video_state,
                    "new_network_events": events[before_events:],
                }, ensure_ascii=False, separators=(",", ":")))

                # Exercise a normal HTMLAudioElement seek only if one exists and
                # reports a finite duration; media URL values are never printed.
                seek_result = "not_tested_no_finite_audio_element"
                before_seek_events = len(events)
                if audio_state and audio_state[0]["duration"] and audio_state[0]["duration"] > 15:
                    before = audio_state[0]["currentTime"]
                    page.locator("audio").first.evaluate("a => {a.currentTime = Math.min(12, a.duration / 2)}")
                    page.wait_for_timeout(3500)
                    after = page.locator("audio").first.evaluate("a => ({currentTime:a.currentTime, seeking:a.seeking, paused:a.paused})")
                    seek_result = {"before_seconds": round(before, 2), "after": after}
                print("seek_result=" + json.dumps(seek_result, ensure_ascii=False))
                print("seek_network_events=" + json.dumps(events[before_seek_events:], ensure_ascii=False, separators=(",", ":")))

            print("network_events=" + json.dumps(events, ensure_ascii=False, separators=(",", ":")))
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
