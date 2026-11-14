"""Compare the existing HitmosBrowserReader on IDs selected by the experiment."""

from __future__ import annotations

import asyncio
import json

from musicstream.audio_providers.base import RecordingReference
from musicstream.audio_providers.hitmos import HitmosProvider
from musicstream.audio_providers.hitmos_browser import HitmosBrowserReader


CASES = (
    ("Sonne", "Rammstein", 272, "57658982"),
    ("Ты не бойся ночи", "ENZRO", 219, "81423717"),
    ("Ты не бойся ночи", "ENZRO", 214, "82566825"),
)


async def main() -> None:
    rows = []
    for title, artist, duration, song_id in CASES:
        result = await HitmosProvider(
            HitmosBrowserReader(), diagnostics_enabled=True
        ).lookup(RecordingReference(title=title, artist=artist,
                                    duration_seconds=duration), song_id)
        candidate = result.candidates[0] if result.candidates else None
        rows.append({
            "title": title,
            "artist": artist,
            "song_id": song_id,
            "status": result.status,
            "reason": result.reason,
            "stage": result.stage,
            "method": candidate.method if candidate else None,
            "has_stream": bool(candidate and candidate.stream_url),
            "match": candidate.match.status if candidate else None,
            "diagnostics": result.diagnostics.model_dump() if result.diagnostics else None,
        })
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    with open("experiments/hitmos-playwright/current-adapter.json", "w", encoding="utf-8") as stream:
        json.dump(rows, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    asyncio.run(main())
