"""Visible, metadata-only VK Web Audio observation. No secrets or audio saved."""

from __future__ import annotations

import json
import re
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from musicstream.audio_providers.base import RecordingReference
from musicstream.audio_providers.matching import assess


QUERIES = (
    RecordingReference(artist="Rammstein", title="Sonne", duration_seconds=272),
    RecordingReference(artist="The Weeknd", title="Blinding Lights", duration_seconds=200),
    RecordingReference(artist="Кино", title="Группа крови", duration_seconds=285),
    RecordingReference(artist="Machine Gun Kelly", title="FIX UR FACE"),
)
MEDIA_TYPES = (
    "audio/", "application/vnd.apple.mpegurl", "application/x-mpegurl",
    "application/dash+xml", "application/octet-stream",
)
MEDIA_SUFFIXES = (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav", ".m3u8", ".mpd")


def safe_route(url: str, resource_type: str) -> tuple[str, str]:
    """Return host and a coarse route class, never a URL or query string."""
    parts = urlsplit(url)
    host = (parts.hostname or "unknown-host").lower()
    path = parts.path.lower()
    media = resource_type == "media" or path.endswith(MEDIA_SUFFIXES)
    if media:
        return host, "media-path-redacted"
    if any(token in path for token in ("audio", "music", "track", "al_audio")):
        return host, "audio-api-path-redacted"
    if resource_type == "document":
        return host, "document"
    return host, resource_type[:24]


def safe_headers(headers: dict[str, str]) -> dict[str, object]:
    lowered = {key.lower(): value for key, value in headers.items()}
    return {
        "content_type": lowered.get("content-type", "").split(";", 1)[0][:100] or None,
        "content_length": lowered.get("content-length"),
        "accept_ranges": lowered.get("accept-ranges"),
        "content_range": lowered.get("content-range"),
        "range_request": "range" in lowered,
        "cookie_present": "cookie" in lowered,
        "authorization_present": "authorization" in lowered,
        "set_cookie_present": "set-cookie" in lowered,
    }


def candidate_rows(page, reference: RecordingReference | None = None) -> list[dict]:
    """Read visible result-row text only; do not inspect page scripts/storage."""
    try:
        if reference is None:
            rows = page.locator(
                '[class*="audio_row"], [data-testid*="audio"], [data-qa*="audio"]'
            ).evaluate_all("""els => els.filter(e => {
                const r=e.getBoundingClientRect(), s=getComputedStyle(e);
                return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden';
              }).map(e => ({
                text:(e.innerText||'').replace(/\\s+/g,' ').trim().slice(0,240),
                title:(e.querySelector('[class*=title]')?.innerText||'').trim().slice(0,120),
                artist:(e.querySelector('[class*=performer], [class*=artist]')?.innerText||'').trim().slice(0,120),
                duration:(e.querySelector('[class*=duration]')?.innerText||'').trim().slice(0,16),
                buttons:[...e.querySelectorAll('button,[role=button]')].map(b=>b.getAttribute('aria-label')||b.title||'').filter(Boolean).slice(0,6)
              }))""")
        else:
            rows = page.locator("body").evaluate("""(body, query) => {
              const norm = s => (s||'').toLocaleLowerCase().replace(/ё/g,'е').replace(/\\s+/g,' ').trim();
              const needles = [query.title, query.artist].map(norm).filter(Boolean);
              const visible = e => { const r=e.getBoundingClientRect(),s=getComputedStyle(e); return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'; };
              const matches = text => { const t=norm(text); return t && needles.some(n=>t.includes(n)); };
              const found = [...body.querySelectorAll('div,li,article,section,[role=listitem]')]
                .filter(e=>visible(e)&&matches(e.innerText)&&((e.innerText||'').trim().length<=500));
              const picked=[];
              for(const e of found){
                let node=e;
                for(let i=0;i<6&&node.parentElement;i++,node=node.parentElement){
                  const cls=String(node.className||'').toLowerCase();
                  const hasButton=!!node.querySelector('button,[role=button]');
                  if(cls.includes('audio')||cls.includes('track')||cls.includes('row')||node.getAttribute('role')==='listitem'||hasButton) break;
                }
                const text=(node.innerText||'').replace(/\\s+/g,' ').trim();
                if(text.length>=6&&text.length<=500)picked.push(node);
              }
              const seen=new Set();
              return picked.map(e=>{
                const text=(e.innerText||'').replace(/\\s+/g,' ').trim().slice(0,320);
                return {text,title:(e.querySelector('[class*=title]')?.innerText||'').trim().slice(0,120),
                  artist:(e.querySelector('[class*=performer], [class*=artist]')?.innerText||'').trim().slice(0,120),
                  duration:(e.querySelector('[class*=duration]')?.innerText||'').trim().slice(0,16),
                  row_class:String(e.className||'').slice(0,100),
                  buttons:[...e.querySelectorAll('button,[role=button]')].map(b=>b.getAttribute('aria-label')||b.title||'').filter(Boolean).slice(0,6)};
              }).filter(row=>row.text&&!seen.has(row.text)&&seen.add(row.text)).slice(0,40);
            }""", {"title": reference.title, "artist": reference.artist})
    except Exception:
        return []
    seen = set()
    result = []
    for row in rows:
        key = row.get("text", "")
        if key and key not in seen:
            seen.add(key)
            result.append(row)
    return result[:30]


def page_summary(page) -> dict:
    inputs = page.locator("input").evaluate_all("""els => els.slice(0,30).map(e => {
        const r=e.getBoundingClientRect(), s=getComputedStyle(e);
        return {type:e.type, name:e.name||'', placeholder:e.placeholder||'',
          aria:e.getAttribute('aria-label')||'', visible:r.width>0&&r.height>0&&s.display!=='none'}
      })""")
    return {
        "title": page.title()[:160],
        "path": urlsplit(page.url).path[:120],
        "password_field_present": page.locator('input[type="password"]').count() > 0,
        "inputs": inputs,
        "candidate_rows": candidate_rows(page),
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    profile = Path(tempfile.gettempdir()) / "MusicStream-VK-Audio-LAB-profile"
    events: list[dict] = []
    started: dict[int, float] = {}
    playing_events: list[dict] = []

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile), channel="chrome", headless=False,
            accept_downloads=False, viewport={"width": 1440, "height": 950},
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.add_init_script("""(() => {
          window.__vkLabPlaybackEvents=[];
          for (const name of ['play','playing','canplay','error']) {
            document.addEventListener(name, event => {
              const el=event.target;
              if (!(el instanceof HTMLMediaElement)) return;
              let host=null; try { host=el.currentSrc ? new URL(el.currentSrc).hostname : null; } catch {}
              window.__vkLabPlaybackEvents.push({event:name, epoch_ms:Date.now(), tag:el.tagName,
                paused:el.paused, ready_state:el.readyState, network_state:el.networkState,
                duration:Number.isFinite(el.duration)?el.duration:null, current_time:el.currentTime,
                source_host:host, error_code:el.error?.code||null});
            }, true);
          }
        })();""")

        def on_request(request):
            started[id(request)] = time.perf_counter()
            host, route = safe_route(request.url, request.resource_type)
            try:
                headers = request.all_headers()
            except Exception:
                return
            if (request.resource_type in {"media", "fetch", "xhr"}
                    or urlsplit(request.url).path.lower().endswith(MEDIA_SUFFIXES)):
                events.append({"kind": "request", "method": request.method,
                    "host": host, "route": route, "resource_type": request.resource_type,
                    "elapsed_from_probe_start_ms": round((time.perf_counter() - probe_started) * 1000),
                    **safe_headers(headers)})

        def on_response(response):
            request = response.request
            try:
                headers = response.all_headers()
            except Exception:
                return
            content_type = headers.get("content-type", "").split(";", 1)[0].lower()
            host, route = safe_route(response.url, request.resource_type)
            is_media = (request.resource_type == "media"
                        or any(content_type.startswith(kind) for kind in MEDIA_TYPES)
                        or urlsplit(response.url).path.lower().endswith(MEDIA_SUFFIXES))
            if is_media:
                events.append({"kind": "response", "method": request.method,
                    "host": host, "route": route, "resource_type": request.resource_type,
                    "status": response.status,
                    "request_to_response_ms": round((time.perf_counter() - started.get(id(request), time.perf_counter())) * 1000),
                    **safe_headers(headers)})

        probe_started = time.perf_counter()
        page.on("request", on_request)
        page.on("response", on_response)
        try:
            response = page.goto("https://vk.com/audio", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)
            print(json.dumps({"stage": "opened", "status": response.status if response else None,
                              **page_summary(page)}, ensure_ascii=False))
            input("If VK asks, sign in only in the visible Chrome window. Press Enter here after sign-in: ")
            page.wait_for_timeout(2500)
            if urlsplit(page.url).path != "/audio":
                page.goto("https://vk.com/audio", wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2000)
            print(json.dumps({"stage": "ready", **page_summary(page)}, ensure_ascii=False))

            for reference in QUERIES:
                print(f"\nUse VK audio search for: {reference.artist} - {reference.title}")
                input("Press Enter here once the results are visible: ")
                page.wait_for_timeout(1200)
                rows = candidate_rows(page, reference)
                assessments = []
                for row in rows:
                    title = row.get("title") or row.get("text", "")
                    artist = row.get("artist") or None
                    duration_text = row.get("duration", "")
                    duration = None
                    match = re.search(r"(?:(\d{1,2}):)?([0-5]?\d):([0-5]\d)", duration_text)
                    if match:
                        h, m, s = (int(part or 0) for part in match.groups())
                        duration = float(h * 3600 + m * 60 + s)
                    if title:
                        assessment = assess(reference, title, artist, duration)
                        assessments.append({**row, "match": assessment.status,
                                            "match_reasons": assessment.reasons})
                print(json.dumps({"stage": "search_results", "query": f"{reference.artist} - {reference.title}",
                                  "visible_candidate_count": len(rows), "candidates": assessments}, ensure_ascii=False))

            print("\nClick the standard Play control for a recording you may listen to; wait for sound, then press Enter here.")
            input()
            page.wait_for_timeout(1500)
            try:
                playback_events = page.evaluate("window.__vkLabPlaybackEvents || []")
                media_state = page.locator("audio,video").evaluate_all("""els => els.map(el=>{
                  let host=null; try { host=el.currentSrc ? new URL(el.currentSrc).hostname : null; } catch {}
                  return {tag:el.tagName, paused:el.paused, ready_state:el.readyState,
                    network_state:el.networkState, duration:Number.isFinite(el.duration)?el.duration:null,
                    current_time:el.currentTime, source_host:host,
                    can_play_mp3:el.canPlayType('audio/mpeg'), can_play_aac:el.canPlayType('audio/mp4')}
                })""")
            except Exception:
                playback_events, media_state = [], []
            print(json.dumps({"stage": "playback", "media_state": media_state,
                              "html_media_events": playback_events, "network_metadata": events}, ensure_ascii=False))
        finally:
            with suppress(Exception):
                context.close()


if __name__ == "__main__":
    main()
