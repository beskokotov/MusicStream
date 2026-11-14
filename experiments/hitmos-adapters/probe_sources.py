"""Small live metadata probe. It never requests media bytes or plays audio."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from musicstream.audio_providers.base import RecordingReference  # noqa: E402
from providers import AdapterFailure, HitmozAdapter, HitmosFmAdapter, SafeSiteClient  # noqa: E402


TRACKS = (
    RecordingReference(title="Sonne", artist="Rammstein", duration_seconds=272),
    RecordingReference(title="Mutter", artist="Rammstein", duration_seconds=268),
    RecordingReference(title="???????", artist="?????? ????????", duration_seconds=236),
)


async def run() -> None:
    for site, adapter_type in (("hitmoz", HitmozAdapter), ("hitmos_fm", HitmosFmAdapter)):
        client = SafeSiteClient(site)
        adapter = adapter_type(client)
        for track in TRACKS:
            row = {"site": site, "query": f"{track.artist} — {track.title}"}
            try:
                candidates = await adapter.search(track)
                row["status"] = "search_results" if candidates else "not_found"
                row["candidates"] = [
                    {"id": item.id, "artist": item.artist, "title": item.title,
                     "duration_seconds": item.duration_seconds, "match": item.match,
                     "match_reasons": item.match_reasons, "media_present": item.media.present,
                     "media_kind": item.media.kind, "media_host": item.media.host,
                     "media_host_approved": item.media.allowed_host}
                    for item in candidates[:5]
                ]
                exact = next((item for item in candidates if item.match == "likely"), None)
                if site == "hitmoz" and exact:
                    detail = await adapter.inspect_song(track, exact.id)
                    row["song_page"] = {"status": "metadata_inspected", "match": detail.match,
                        "media_present": detail.media.present, "media_kind": detail.media.kind,
                        "media_host": detail.media.host, "media_host_approved": detail.media.allowed_host}
                row["playback"] = "not_attempted_media_rights_unverified"
            except AdapterFailure as exc:
                row["status"] = exc.code
                row["playback"] = "not_attempted"
            print(json.dumps(row, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-live", action="store_true", help="query public HTML pages only")
    args = parser.parse_args()
    if not args.run_live:
        parser.error("pass --run-live to make six rate-limited metadata searches")
    asyncio.run(run())


if __name__ == "__main__":
    main()
