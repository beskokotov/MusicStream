"""Measure five Hitmos searches/details in rate-limit-compliant windows."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from musicstream.audio_providers.base import RecordingReference, SourceDocument  # noqa: E402
from musicstream.audio_providers.hitmos import parse_search_ids, parse_search_metadata, parse_song  # noqa: E402
from musicstream.audio_providers.hitmos_browser import HitmosBrowserReader  # noqa: E402
from musicstream.audio_providers.hitmos_stream import HitmosStreamProxy  # noqa: E402

TRACKS = (
    RecordingReference(title="Sonne", artist="Rammstein", duration_seconds=272),
    RecordingReference(title="Du hast", artist="Rammstein", duration_seconds=235),
    RecordingReference(title="Mutter", artist="Rammstein", duration_seconds=268),
    RecordingReference(title="Blinding Lights", artist="The Weeknd", duration_seconds=200),
    RecordingReference(title="Группа крови", artist="Кино", duration_seconds=284),
)


async def main() -> None:
    reader = HitmosBrowserReader(diagnostics_enabled=True, lab_mode=True, request_limit=5)
    stream_proxy = HitmosStreamProxy()
    out = []
    ids_by_title = {}
    try:
        for pass_name in ("cold", "repeat"):
            if pass_name == "repeat":
                # Five detail calls were just made; wait for the reader quota.
                await asyncio.sleep(61)
            for track in TRACKS:
                started = time.perf_counter()
                try:
                    document = await reader.read_search(f"{track.artist} {track.title}")
                    html = document.html if isinstance(document, SourceDocument) else document
                    ids = parse_search_ids(html)
                    metadata = parse_search_metadata(html, ids)
                    ids_by_title[(pass_name, track.title)] = ids[0] if ids else None
                    candidates = [
                        {"id": song_id, "title": metadata.get(song_id, (None, None, None))[0],
                         "artist": metadata.get(song_id, (None, None, None))[1],
                         "duration_seconds": metadata.get(song_id, (None, None, None))[2]}
                        for song_id in ids[:5]
                    ]
                    row = {"pass": pass_name, "stage": "search", "title": track.title,
                           "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                           "requests": 1, "candidates": candidates, "error": None}
                except Exception as exc:
                    row = {"pass": pass_name, "stage": "search", "title": track.title,
                           "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                           "error": type(exc).__name__}
                out.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)

            # Respect the reader's five-per-minute limit between 5-call batches.
            if pass_name == "cold":
                await asyncio.sleep(61)
                for track in TRACKS:
                    started = time.perf_counter()
                    song_id = ids_by_title.get(("cold", track.title))
                    try:
                        if not song_id:
                            raise RuntimeError("candidate_unavailable")
                        reader_started = time.perf_counter()
                        document = await reader.read_song(song_id)
                        reader_ms = round((time.perf_counter() - reader_started) * 1000, 1)
                        title, artist, duration = parse_song(document.html, song_id)
                        diag = document.diagnostics
                        range_row = {"status": None, "content_type": None}
                        range_ms = None
                        if document.stream_url:
                            range_started = time.perf_counter()
                            validated = await stream_proxy.probe_and_register(document.stream_url)
                            range_ms = round((time.perf_counter() - range_started) * 1000, 1)
                            range_row = {"status": 206, "content_type": validated.content_type}
                        row = {"pass": "cold", "stage": "metadata_and_audio_redirect",
                               "title": track.title, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                               "reader_ms": reader_ms, "range_probe_ms": range_ms, "requests": 3,
                               "song_metadata": {"title": title, "artist": artist,
                                                 "duration_seconds": duration},
                               "metadata_status": diag.metadata_status if diag else None,
                               "redirect_status": diag.redirect_status if diag else None,
                               "redirect_valid": diag.redirect_valid if diag else None,
                               "range": range_row, "error": None}
                    except Exception as exc:
                        row = {"pass": "cold", "stage": "metadata_and_audio_redirect",
                               "title": track.title, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                               "error": type(exc).__name__}
                    out.append(row)
                    print(json.dumps(row, ensure_ascii=False), flush=True)
                    await asyncio.sleep(1.2)

        # Repeat detail pages are measured in a fresh rate-limit window too.
        await asyncio.sleep(61)
        for track in TRACKS:
            started = time.perf_counter()
            song_id = ids_by_title.get(("repeat", track.title))
            try:
                if not song_id:
                    raise RuntimeError("candidate_unavailable")
                reader_started = time.perf_counter()
                document = await reader.read_song(song_id)
                reader_ms = round((time.perf_counter() - reader_started) * 1000, 1)
                title, artist, duration = parse_song(document.html, song_id)
                diag = document.diagnostics
                range_row = {"status": None, "content_type": None}
                range_ms = None
                if document.stream_url:
                    range_started = time.perf_counter()
                    validated = await stream_proxy.probe_and_register(document.stream_url)
                    range_ms = round((time.perf_counter() - range_started) * 1000, 1)
                    range_row = {"status": 206, "content_type": validated.content_type}
                row = {"pass": "repeat", "stage": "metadata_and_audio_redirect",
                       "title": track.title, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                       "reader_ms": reader_ms, "range_probe_ms": range_ms, "requests": 3,
                       "song_metadata": {"title": title, "artist": artist,
                                         "duration_seconds": duration},
                       "metadata_status": diag.metadata_status if diag else None,
                       "redirect_status": diag.redirect_status if diag else None,
                       "redirect_valid": diag.redirect_valid if diag else None,
                       "range": range_row, "error": None}
            except Exception as exc:
                row = {"pass": "repeat", "stage": "metadata_and_audio_redirect",
                       "title": track.title, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                       "error": type(exc).__name__}
            out.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            await asyncio.sleep(1.2)
    finally:
        await reader.close()
    target = ROOT / "experiments" / "hitmos-http-resolver" / "browser-results.json"
    target.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
