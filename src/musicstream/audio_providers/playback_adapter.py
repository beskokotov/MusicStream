"""Short-lived Range transport for the locally generated playback test fixture.

This adapter is deliberately unrelated to VK: it proves the MusicStream HTTP
audio path and shared HTMLAudioElement without implying that VK permits the
same handoff.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException


FIXTURE = Path(__file__).resolve().parent.parent / "static" / "audio-transport-test.mp3"
TOKEN_TTL_SECONDS = 120
MAX_TOKENS = 32


@dataclass(frozen=True)
class PlaybackGrant:
    token: str
    expires_at: float


class PlaybackAdapter:
    """Serve a fixed, self-generated MP3 with bounded in-memory playback grants."""

    def __init__(self, *, ttl_seconds: int = TOKEN_TTL_SECONDS):
        self.ttl_seconds = ttl_seconds
        self._grants: OrderedDict[str, float] = OrderedDict()
        self._lock = asyncio.Lock()

    async def prepare(self) -> PlaybackGrant:
        if not FIXTURE.is_file():
            raise HTTPException(503, detail={"reason": "test_audio_fixture_missing"})
        now = time.monotonic()
        async with self._lock:
            for token, expires in list(self._grants.items()):
                if expires <= now:
                    del self._grants[token]
            token = secrets.token_urlsafe(24)
            expiry = now + self.ttl_seconds
            self._grants[token] = expiry
            while len(self._grants) > MAX_TOKENS:
                self._grants.popitem(last=False)
        return PlaybackGrant(token, expiry)

    async def open(self, token: str, range_header: str | None) -> tuple[int, dict[str, str], int, int]:
        async with self._lock:
            expires = self._grants.get(token)
            if expires is None or expires <= time.monotonic():
                self._grants.pop(token, None)
                raise HTTPException(410, detail={"reason": "playback_source_expired"},
                                    headers={"Cache-Control": "no-store"})
            self._grants.move_to_end(token)

        size = FIXTURE.stat().st_size
        start, end = 0, size - 1
        status = 200
        if range_header:
            if not range_header.startswith("bytes=") or "," in range_header:
                raise self._range_error(size)
            raw_start, separator, raw_end = range_header[6:].partition("-")
            if not separator:
                raise self._range_error(size)
            try:
                if raw_start:
                    start = int(raw_start)
                    end = int(raw_end) if raw_end else size - 1
                else:
                    suffix_length = int(raw_end)
                    if suffix_length <= 0:
                        raise ValueError
                    start = max(0, size - suffix_length)
            except ValueError:
                raise self._range_error(size) from None
            if start < 0 or start >= size or end < start:
                raise self._range_error(size)
            end = min(end, size - 1)
            status = 206

        length = end - start + 1
        headers = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
            "Content-Length": str(length),
            "Content-Type": "audio/mpeg",
            "X-Content-Type-Options": "nosniff",
        }
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return status, headers, start, length

    @staticmethod
    def _range_error(size: int) -> HTTPException:
        return HTTPException(416, detail={"reason": "invalid_range"},
                            headers={"Content-Range": f"bytes */{size}", "Cache-Control": "no-store"})
