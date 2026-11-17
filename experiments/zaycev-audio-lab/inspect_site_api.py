"""Inspect a few first-party Zaycev page scripts for public API route names.

No cookies/auth, no redirects, no media requests, and no response files.
"""

from __future__ import annotations

import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


PAGE = "https://zaycev.net/search?query_search=Rammstein%20Sonne&type=all"
LIMIT_BYTES = 1024 * 1024
TIMEOUT_SECONDS = 8
MAX_SCRIPTS = 6


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


HTTP = build_opener(NoRedirect())


def fetch(url: str):
    request = Request(url, headers={"User-Agent": "MusicStream-Zaycev-LAB/1.0", "Accept": "text/html,application/javascript,*/*;q=0.5"})
    started = time.perf_counter()
    try:
        with HTTP.open(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read(LIMIT_BYTES + 1)
            if len(body) > LIMIT_BYTES:
                return response.status, round((time.perf_counter() - started) * 1000), None, "response_too_large"
            return response.status, round((time.perf_counter() - started) * 1000), body, None
    except HTTPError as exc:
        return exc.code, round((time.perf_counter() - started) * 1000), None, "http_error"
    except (TimeoutError, URLError, OSError) as exc:
        return None, round((time.perf_counter() - started) * 1000), None, type(exc).__name__


def is_first_party(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == "zaycev.net" or host.endswith(".zaycev.net")


def safe_location(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.hostname}{parts.path}"


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    status, elapsed, body, error = fetch(PAGE)
    print(f"search_page: status={status if status is not None else 'network_error'} elapsed_ms={elapsed} error={error or 'none'}")
    if body is None:
        return

    html = body.decode("utf-8", errors="replace")
    scripts = []
    for src in re.findall(r"<script\b[^>]*\bsrc=[\"']([^\"']+)[\"']", html, flags=re.IGNORECASE):
        absolute = urljoin(PAGE, src)
        if is_first_party(absolute) and absolute not in scripts:
            scripts.append(absolute)
    page_scripts = [url for url in scripts if "/_next/static/chunks/pages/" in url or "/_next/static/chunks/app/" in url]
    selected_scripts = (page_scripts or scripts[-MAX_SCRIPTS:])[:MAX_SCRIPTS]
    print(f"first_party_scripts_found={len(scripts)} inspected={len(selected_scripts)} limit={MAX_SCRIPTS}")
    print("selected_script_locations=" + ", ".join(safe_location(url) for url in selected_scripts))

    # Match route-shaped strings only; never print query strings, tokens, or URLs.
    route_pattern = re.compile(r"(?:https?://api\.zaycev\.net)?(/(?:external|api|v[0-9]+)/[A-Za-z0-9_./{}:-]{1,100})")
    hits = set()
    contexts = []
    for url in selected_scripts:
        status, elapsed, js_body, error = fetch(url)
        print(f"script: location={safe_location(url)} status={status if status is not None else 'network_error'} elapsed_ms={elapsed} error={error or 'none'}")
        if js_body is None:
            continue
        source = js_body.decode("utf-8", errors="replace")
        for route in route_pattern.findall(source):
            hits.add(route.split("?", 1)[0])
        for marker in ("/api/external", "/api/single", "this.api({", "/search", "/track/", "/play"):
            start = 0
            marker_hits = 0
            while marker_hits < 3:
                index = source.find(marker, start)
                if index < 0:
                    break
                snippet = source[max(0, index - 100):index + 180]
                snippet = re.sub(r"https?://[^\s\"']+", "<url-redacted>", snippet)
                snippet = re.sub(r"(?i)(token|secret|cookie|authorization)([=:])[^,;\"']+", r"\1\2<redacted>", snippet)
                contexts.append(" ".join(snippet.split()))
                start = index + len(marker)
                marker_hits += 1

    print("api_routes=" + (", ".join(sorted(hits)[:30]) if hits else "none_found_in_sampled_scripts"))
    for snippet in contexts:
        print("route_context=" + snippet[:300])
    print(f"summary=Fetched one public search page and up to {MAX_SCRIPTS} same-site JS assets; no auth, redirects, or audio.")


if __name__ == "__main__":
    main()
