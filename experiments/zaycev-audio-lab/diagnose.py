"""Bounded, unauthenticated diagnostics for the historical Zaycev API.

This script deliberately does not implement the legacy auth handshake, follow
playback URLs, download media, print tokens, or persist responses.
"""

from __future__ import annotations

import json
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


BASE = "https://api.zaycev.net/external"
TIMEOUT_SECONDS = 6
MAX_BODY_BYTES = 256 * 1024
USER_AGENT = "MusicStream-Zaycev-LAB/1.0 (bounded public endpoint diagnostics)"
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


HTTP = build_opener(NoRedirect())

QUERIES = (
    ("Rammstein", "Sonne"),
    ("Rammstein", "Du hast"),
    ("The Weeknd", "Blinding Lights"),
    ("Кино", "Группа крови"),
    ("Machine Gun Kelly", "FIX UR FACE"),
)


def fetch(url: str) -> tuple[int | None, int, bytes | None, str | None]:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    started = time.perf_counter()
    try:
        with HTTP.open(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read(MAX_BODY_BYTES + 1)
            if len(body) > MAX_BODY_BYTES:
                return response.status, round((time.perf_counter() - started) * 1000), None, "response_too_large"
            return response.status, round((time.perf_counter() - started) * 1000), body, None
    except HTTPError as exc:
        # Keep only a bounded in-memory body; output is parsed and redacted below.
        body = exc.read(MAX_BODY_BYTES + 1)
        if len(body) > MAX_BODY_BYTES:
            body = None
        return exc.code, round((time.perf_counter() - started) * 1000), body, "http_error"
    except (TimeoutError, URLError, OSError) as exc:
        return None, round((time.perf_counter() - started) * 1000), None, type(exc).__name__


def decode_json(body: bytes | None):
    if body is None:
        return None
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def compact(value: object, limit: int = 300) -> str:
    return str(value).replace("\r", " ").replace("\n", " ")[:limit]


def safe_error(value: object) -> str:
    """Show only a short error description; redact URL/token-shaped values."""
    if isinstance(value, dict):
        parts = []
        for key in ("code", "text", "message"):
            if key in value and isinstance(value[key], (str, int, float)):
                parts.append(f"{key}={safe_error(value[key])}")
        return " ".join(parts) or "object_keys=" + ",".join(sorted(str(key) for key in value)[:20])
    text = compact(value, 240)
    text = re.sub(r"https?://\S+", "<url-redacted>", text)
    text = re.sub(r"(?i)(token|secret|cookie|authorization)\s*[:=]\s*\S+", r"\1=<redacted>", text)
    text = re.sub(r"\b[A-Za-z0-9_-]{40,}\b", "<opaque-value-redacted>", text)
    return text


def walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def track_candidates(payload):
    found = []
    seen = set()
    for item in walk_dicts(payload):
        track_id = item.get("id", item.get("trackId"))
        title = item.get("track", item.get("name", item.get("title")))
        artist = item.get("artistName", item.get("artist"))
        if track_id is None or not isinstance(title, str):
            continue
        if isinstance(artist, dict):
            artist = artist.get("name")
        identity = str(track_id)
        if identity in seen:
            continue
        seen.add(identity)
        found.append({
            "id": identity,
            "title": compact(title),
            "artist": compact(artist) if artist is not None else None,
            "duration": item.get("duration"),
            "bitrate": item.get("bitrate"),
        })
    return found


def report_json_endpoint(label: str, url: str, *, candidates: bool = False) -> list[dict]:
    status, elapsed_ms, body, error = fetch(url)
    payload = decode_json(body)
    print(f"{label}: status={status if status is not None else 'network_error'} elapsed_ms={elapsed_ms} error={error or 'none'}")
    if payload is None:
        print("  json=unavailable")
        return []
    if isinstance(payload, dict):
        # Never print token values or arbitrary URLs returned by the service.
        print(f"  top_level_keys={sorted(payload.keys())[:30]}")
        if "error" in payload:
            print("  error=" + safe_error(payload["error"]))
    if candidates:
        items = track_candidates(payload)
        print(f"  candidate_count={len(items)}")
        for item in items[:5]:
            print("  candidate=" + json.dumps(item, ensure_ascii=False, separators=(",", ":")))
        return items
    keys_with_url = [key for key, value in payload.items() if "url" in key.lower() and isinstance(value, str)] if isinstance(payload, dict) else []
    print(f"  playback_url_field_present={bool(keys_with_url)}")
    return []


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("mode=unauthenticated; requests=8; media_downloads=0; redirects_followed=0; secrets_logged=0")
    report_json_endpoint("hello", BASE + "/hello")

    all_candidates = []
    for artist, title in QUERIES:
        query = f"{artist} {title}"
        params = urlencode({"query": query, "page": 1, "type": "all", "sort": "", "style": ""})
        items = report_json_endpoint("search " + query, BASE + "/search?" + params, candidates=True)
        all_candidates.extend(items)

    # The historical article documents this ID and requires an access token for
    # both routes. These no-token probes test current endpoint behavior only.
    legacy_id = "1310964"
    report_json_endpoint("track_without_auth", f"{BASE}/track/{legacy_id}")
    report_json_endpoint("play_without_auth", f"{BASE}/track/{legacy_id}/play")

    print("summary=No auth exchange was attempted. No playback URL was followed and no audio bytes were requested.")
    print(f"summary_candidates_extracted={len(all_candidates)}")


if __name__ == "__main__":
    main()
