"""Small official-API experiment. Never saves credentials or stream URLs."""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx


HERE = Path(__file__).resolve().parent
TRACKS = json.loads((HERE / "tracks.json").read_text(encoding="utf-8"))
VERSION_WORDS = re.compile(r"\b(remix|cover|live|concert|acoustic|instrumental|karaoke|tribute|bootleg|edit|sped up|slowed|кавер|ремикс|концерт|живой)\b", re.I)
OFFICIAL_HOSTS = {
    # OpenAudio documents Cloudflare R2 as a node storage backend. Only follow
    # its URL when an official Audius endpoint redirects there.
    "audius": ("audius.co", "r2.cloudflarestorage.com"),
    "jamendo": ("jamendo.com",),
    "soundcloud": ("soundcloud.com", "sndcdn.com"),
}


def normal(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return " ".join(re.findall(r"[^\W_]+", value, flags=re.UNICODE))


def same_recording(ref: dict, artist: str, title: str, duration: int | float | None,
                   album: str | None = None) -> bool:
    if normal(artist) != normal(ref["artist"]):
        return False
    wanted = normal(re.sub(r"\s*\(feat\..*?\)", "", ref["title"], flags=re.I))
    candidate = normal(re.sub(r"\s*\(feat\..*?\)", "", title, flags=re.I))
    if candidate != wanted or VERSION_WORDS.search(title):
        return False
    if duration is None or abs(float(duration) - ref["duration_seconds"]) > 8:
        return False
    if album and normal(album) != normal(ref["album"]):
        return False
    return True


def allowed(url: str, provider: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.username is None and parsed.password is None and any(
        parsed.hostname == suffix or (parsed.hostname or "").endswith("." + suffix)
        for suffix in OFFICIAL_HOSTS[provider]
    )


def audio_signature(data: bytes, mime: str) -> bool:
    if mime == "audio/mpeg":
        return data.startswith(b"ID3") or (len(data) > 1 and data[0] == 0xff and data[1] & 0xe0 == 0xe0)
    if mime in ("audio/flac", "audio/x-flac"):
        return data.startswith(b"fLaC")
    if mime in ("audio/ogg", "application/ogg"):
        return data.startswith(b"OggS")
    if mime in ("audio/mp4", "audio/aac", "audio/x-m4a"):
        return b"ftyp" in data[:16] or data[:2] == b"\xff\xf1"
    return False


def first_bytes(client: httpx.Client, provider: str, url: str,
                *, headers: dict[str, str] | None = None, duration: int) -> dict:
    """Read at most 4 KiB through official redirects, then close the socket."""
    if not allowed(url, provider):
        return {"status": "UNVERIFIED", "note": "stream URL outside official domains"}
    entry_url = url
    started = time.perf_counter()
    request_headers = {**(headers or {}), "Range": "bytes=0-4095", "Accept": "audio/*"}
    try:
        for _ in range(4):
            with client.stream("GET", url, headers=request_headers, follow_redirects=False,
                               timeout=15) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        return {"status": "UNVERIFIED", "note": "redirect without target"}
                    url = urljoin(url, location)
                    if not allowed(url, provider):
                        return {"status": "UNVERIFIED", "note": "redirect outside official domains"}
                    continue
                if response.status_code in (401, 403):
                    return {"status": "AUTH_REQUIRED", "http_status": response.status_code}
                if response.status_code >= 400:
                    return {"status": "API_ERROR", "http_status": response.status_code}
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                content_range = response.headers.get("content-range", "")
                raw = b""
                for chunk in response.iter_bytes(chunk_size=4096):
                    raw = chunk[:4096]
                    if raw:
                        break
                elapsed_ms = round((time.perf_counter() - started) * 1000)
                result = {"status": "UNVERIFIED", "http_status": response.status_code,
                          "mime": content_type, "first_bytes_ms": elapsed_ms,
                          "bytes_read": len(raw), "range_supported": response.status_code == 206}
                if content_type.startswith("text/") or "html" in content_type:
                    result["note"] = "HTML, not audio"
                    return result
                if not raw or not (content_type.startswith("audio/") or content_type in
                                   ("application/ogg", "application/octet-stream")):
                    result["note"] = "no confirmed audio payload"
                    return result
                result["audio_signature"] = audio_signature(raw, content_type)
                if not result["audio_signature"]:
                    result["note"] = "audio signature unverified"
                    return result
                match = re.fullmatch(r"bytes\s+0-\d+/(\d+)", content_range)
                if response.status_code == 206 and match and int(match.group(1)) > max(524288, duration * 8000):
                    result["tail_verified"] = verify_tail(
                        client, provider, entry_url, int(match.group(1)), headers=headers)
                    if result["tail_verified"]:
                        result["status"] = "STREAM_OK"
                        result["note"] = "official stream, audio header and tail ranges of full-sized resource"
                    else:
                        result["note"] = "full track tail range unverified"
                else:
                    result["note"] = "initial HTTP response does not prove a full track"
                return result
        return {"status": "UNVERIFIED", "note": "too many redirects"}
    except httpx.RequestError as exc:
        return {"status": "API_ERROR", "note": type(exc).__name__}


def verify_tail(client: httpx.Client, provider: str, url: str, total: int,
                *, headers: dict[str, str] | None = None) -> bool:
    """Confirm the end of the same full-sized resource with at most 4 KiB."""
    request_headers = {**(headers or {}), "Range": "bytes=-4096", "Accept": "audio/*"}
    try:
        for _ in range(4):
            with client.stream("GET", url, headers=request_headers, follow_redirects=False,
                               timeout=15) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        return False
                    url = urljoin(url, location)
                    if not allowed(url, provider):
                        return False
                    continue
                if response.status_code != 206:
                    return False
                match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+)",
                                     response.headers.get("content-range", ""))
                if not match or int(match.group(2)) != total - 1 or int(match.group(3)) != total:
                    return False
                for chunk in response.iter_bytes(chunk_size=4096):
                    return bool(chunk[:4096])
                return False
        return False
    except httpx.RequestError:
        return False


def audius(client: httpx.Client, ref: dict) -> dict:
    try:
        search_title = re.sub(r"\s*\(feat\..*?\)", "", ref["title"], flags=re.I)
        response = client.get("https://api.audius.co/v1/tracks/search",
                              params={"query": f'{ref["artist"]} {search_title}', "limit": 25}, timeout=15)
        if response.status_code in (401, 403):
            return {"status": "AUTH_REQUIRED", "http_status": response.status_code}
        if response.status_code != 200:
            return {"status": "API_ERROR", "http_status": response.status_code}
        rows = response.json().get("data", [])
        if not isinstance(rows, list):
            return {"status": "API_ERROR", "note": "invalid search response"}
        matches = [row for row in rows if same_recording(
            ref, (row.get("user") or {}).get("name", ""), row.get("title", ""), row.get("duration"))]
        if not matches:
            return {"status": "WRONG_VERSION" if rows else "NOT_FOUND",
                    "results": len(rows), "note": "no exact artist/title/duration/version match"}
        candidate = matches[0]
        if not candidate.get("is_streamable") or candidate.get("is_stream_gated"):
            return {"status": "UNVERIFIED", "results": len(rows), "note": "matched but stream is gated"}
        stream = first_bytes(client, "audius",
                             f'https://api.audius.co/v1/tracks/{candidate["id"]}/stream',
                             duration=ref["duration_seconds"])
        stream["results"] = len(rows)
        stream["matched_artist"] = (candidate.get("user") or {}).get("name", "")
        stream["matched_title"] = candidate.get("title", "")
        stream["matched_duration"] = candidate.get("duration")
        return stream
    except (httpx.RequestError, ValueError, KeyError, TypeError) as exc:
        return {"status": "API_ERROR", "note": type(exc).__name__}


def jamendo(client: httpx.Client, ref: dict, client_id: str | None) -> dict:
    if not client_id:
        return {"status": "AUTH_REQUIRED", "note": "Jamendo client_id unavailable"}
    try:
        response = client.get("https://api.jamendo.com/v3.0/tracks/", params={
            "client_id": client_id, "format": "json", "namesearch": ref["title"],
            "type": "single albumtrack", "audioformat": "mp32", "limit": 25,
        }, timeout=15)
        if response.status_code != 200:
            return {"status": "API_ERROR", "http_status": response.status_code}
        body = response.json()
        api_headers = body.get("headers", {})
        if (api_headers.get("code") == 11 and
                "suspended" in str(api_headers.get("error_message", "")).lower()):
            return {"status": "AUTH_REQUIRED", "note": "published test application is suspended"}
        if api_headers.get("status") != "success":
            return {"status": "API_ERROR", "note": "API search rejected"}
        rows = body.get("results", [])
        matches = [row for row in rows if same_recording(
            ref, row.get("artist_name", ""), row.get("name", ""),
            row.get("duration"), row.get("album_name"))]
        if not matches:
            return {"status": "WRONG_VERSION" if rows else "NOT_FOUND", "results": len(rows)}
        candidate = matches[0]
        result = first_bytes(client, "jamendo", "https://api.jamendo.com/v3.0/tracks/file/" +
                             f'?client_id={client_id}&id={candidate["id"]}&action=stream&audioformat=mp32',
                             duration=ref["duration_seconds"])
        result["results"] = len(rows)
        return result
    except (httpx.RequestError, ValueError, KeyError, TypeError) as exc:
        return {"status": "API_ERROR", "note": type(exc).__name__}


def soundcloud(client: httpx.Client, ref: dict, token: str | None) -> dict:
    if not token:
        return {"status": "AUTH_REQUIRED", "note": "registered app and OAuth token unavailable"}
    headers = {"Authorization": f"OAuth {token}"}
    try:
        response = client.get("https://api.soundcloud.com/tracks", params={
            "q": f'{ref["artist"]} {ref["title"]}', "access": "playable,preview,blocked",
            "limit": 25, "linked_partitioning": "true",
        }, headers=headers, timeout=15)
        if response.status_code in (401, 403):
            return {"status": "AUTH_REQUIRED", "http_status": response.status_code}
        if response.status_code != 200:
            return {"status": "API_ERROR", "http_status": response.status_code}
        body = response.json()
        rows = body.get("collection", []) if isinstance(body, dict) else body
        matches = [row for row in rows if same_recording(
            ref, (row.get("user") or {}).get("username", ""), row.get("title", ""),
            (row.get("duration") or 0) / 1000)]
        if not matches:
            return {"status": "WRONG_VERSION" if rows else "NOT_FOUND", "results": len(rows)}
        candidate = matches[0]
        access = candidate.get("access")
        if access == "preview":
            return {"status": "PREVIEW_ONLY", "results": len(rows)}
        if access != "playable":
            return {"status": "UNVERIFIED", "results": len(rows), "note": "not playable"}
        result = first_bytes(client, "soundcloud",
                             f'https://api.soundcloud.com/tracks/{candidate["id"]}/stream',
                             headers=headers, duration=ref["duration_seconds"])
        result["results"] = len(rows)
        return result
    except (httpx.RequestError, ValueError, KeyError, TypeError) as exc:
        return {"status": "API_ERROR", "note": type(exc).__name__}


def main() -> None:
    output = []
    jamendo_id = os.environ.get("JAMENDO_CLIENT_ID")
    soundcloud_token = os.environ.get("SOUNDCLOUD_ACCESS_TOKEN")
    with httpx.Client(headers={"User-Agent": "MusicStream audio-provider experiment/1.0"}) as client:
        for ref in TRACKS:
            row = {"deezer_id": ref["deezer_id"], "audius": audius(client, ref),
                   "jamendo": jamendo(client, ref, jamendo_id),
                   "soundcloud": soundcloud(client, ref, soundcloud_token)}
            if row["jamendo"].get("note") == "published test application is suspended":
                jamendo_id = None
            output.append(row)
            print(json.dumps({"track": ref["artist"] + " — " + ref["title"],
                              "audius": row["audius"]["status"],
                              "jamendo": row["jamendo"]["status"],
                              "soundcloud": row["soundcloud"]["status"]}, ensure_ascii=True), flush=True)
            time.sleep(0.2)
    (HERE / "results.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n",
                                       encoding="utf-8")


if __name__ == "__main__":
    main()
