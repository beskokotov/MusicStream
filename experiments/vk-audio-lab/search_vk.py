"""Run the four VK Audio checks in a visible, temporary browser session."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from musicstream.audio_providers.base import RecordingReference
from musicstream.audio_providers.vk_browser import VKAudioBrowserReader


REFERENCES = (
    RecordingReference(artist="Rammstein", title="Sonne", duration_seconds=272),
    RecordingReference(artist="The Weeknd", title="Blinding Lights", duration_seconds=200),
    RecordingReference(artist="Кино", title="Группа крови", duration_seconds=285),
    RecordingReference(artist="Machine Gun Kelly", title="FIX UR FACE"),
)


async def main() -> None:
    reader = VKAudioBrowserReader()
    for batch, matches in await reader.search_many(REFERENCES):
        print(json.dumps({
            "query": batch.query,
            "candidate_count": batch.candidate_count,
            "results": [{
                "title": item.recording.title,
                "artist": item.recording.artist,
                "id": item.recording.id,
                "duration_seconds": item.recording.duration_seconds,
                "match": item.assessment.status,
                "reasons": item.assessment.reasons,
            } for item in matches],
        }, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
