"""Verify that the official Audius endpoint can deliver a full-sized range."""

import json
from pathlib import Path

import httpx

from probe import first_bytes


def main() -> None:
    with httpx.Client(headers={"User-Agent": "MusicStream audio-provider experiment/1.0"}) as client:
        response = client.get("https://api.audius.co/v1/tracks/trending",
                              params={"limit": 1}, timeout=15)
        response.raise_for_status()
        track = response.json()["data"][0]
        if not track.get("is_streamable") or track.get("is_stream_gated"):
            result = {"status": "UNVERIFIED", "note": "control track is gated"}
        else:
            result = first_bytes(client, "audius",
                                 f'https://api.audius.co/v1/tracks/{track["id"]}/stream',
                                 duration=track["duration"])
    (Path(__file__).resolve().parent / "control.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
