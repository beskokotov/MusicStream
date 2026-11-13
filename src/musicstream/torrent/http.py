"""Local-only, opt-in HTTP byte-range adapter for registered torrent tracks."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from .metadata import TorrentFile, inspect_torrent, select_audio_file
from .session import CacheLimitError, SelectedTorrent, TorrentSessionManager


CONTENT_TYPES = {".mp3": "audio/mpeg", ".flac": "audio/flac", ".ogg": "audio/ogg", ".m4a": "audio/mp4"}
router = APIRouter(prefix="/api/experimental/torrent", include_in_schema=False)


@dataclass
class _Track:
    path: Path
    file: TorrentFile
    peers: tuple[tuple[str, int], ...]
    transfer: SelectedTorrent | None = None
    users: int = 0
    idle_since: float = 0


class TorrentPlaybackRegistry:
    """Server-side allowlist and leases for reusable, idle-expiring transfers."""

    def __init__(
        self, manager: TorrentSessionManager | None = None, *,
        max_streams: int = 2, block_size: int = 64 * 1024,
        idle_seconds: float = 30, read_timeout_seconds: float = 10,
    ) -> None:
        if max_streams < 1 or not 1 <= block_size <= 1024 * 1024 or idle_seconds <= 0 or read_timeout_seconds <= 0:
            raise ValueError("Invalid torrent HTTP limits")
        self.manager = manager or TorrentSessionManager()
        self.max_streams = max_streams
        self.block_size = block_size
        self.idle_seconds = idle_seconds
        self.read_timeout_seconds = read_timeout_seconds
        self._streams = asyncio.Semaphore(max_streams)
        self._lock = asyncio.Lock()
        self._tracks: dict[str, _Track] = {}
        self._reaper: asyncio.Task | None = None

    def register(
        self, playback_id: str, torrent_path: str | Path, file_index: int,
        *, peers: tuple[tuple[str, int], ...] = (),
    ) -> TorrentFile:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", playback_id) or playback_id in self._tracks:
            raise ValueError("Invalid or duplicate playback ID")
        path = Path(torrent_path)
        file = select_audio_file(inspect_torrent(path), file_index)
        if not peers or any(host not in ("127.0.0.1", "::1") or not 1 <= port <= 65535 for host, port in peers):
            raise ValueError("Explicit loopback peer is required")
        self._tracks[playback_id] = _Track(path, file, peers)
        return file

    def file(self, playback_id: str) -> TorrentFile:
        try:
            return self._tracks[playback_id].file
        except KeyError as exc:
            raise HTTPException(404, "Unknown torrent playback") from exc

    async def unregister(self, playback_id: str) -> None:
        """Revoke a prepared track and close its transfer on cancellation."""
        async with self._lock:
            track = self._tracks.pop(playback_id, None)
            if track is not None and track.transfer is not None:
                await track.transfer.aclose()
                track.transfer = None

    async def acquire(self, playback_id: str) -> _Track:
        self.file(playback_id)
        try:
            await asyncio.wait_for(self._streams.acquire(), timeout=0.1)
        except TimeoutError as exc:
            raise HTTPException(429, "Torrent stream limit reached") from exc
        try:
            async with self._lock:
                track = self._tracks.get(playback_id)
                if track is None:
                    raise HTTPException(404, "Unknown torrent playback")
                if track.transfer is None:
                    transfer = self.manager.open_torrent(track.path, track.file.index, peers=track.peers)
                    await transfer.__aenter__()
                    track.transfer = transfer
                track.users += 1
                return track
        except BaseException:
            self._streams.release()
            raise

    async def release(self, track: _Track) -> None:
        async with self._lock:
            track.users -= 1
            track.idle_since = time.monotonic()
        self._streams.release()

    async def read(self, track: _Track, start: int, end: int) -> bytes:
        try:
            return await track.transfer.read_range(start, end, timeout_seconds=self.read_timeout_seconds)
        except CacheLimitError:
            # A long playback can outgrow its bounded cache. Rotate only when no
            # other response is using this transfer.
            async with self._lock:
                if track.users != 1:
                    raise
                await track.transfer.aclose()
                transfer = self.manager.open_torrent(track.path, track.file.index, peers=track.peers)
                await transfer.__aenter__()
                track.transfer = transfer
            return await transfer.read_range(start, end, timeout_seconds=self.read_timeout_seconds)

    async def reap_idle(self) -> None:
        async with self._lock:
            now = time.monotonic()
            for track in self._tracks.values():
                if track.transfer is not None and track.users == 0 and now - track.idle_since >= self.idle_seconds:
                    await track.transfer.aclose()
                    track.transfer = None

    def start(self) -> None:
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_loop())

    async def _reap_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(min(self.idle_seconds / 2, 1))
                await self.reap_idle()
        except asyncio.CancelledError:
            pass

    async def aclose(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            await self._reaper
            self._reaper = None
        async with self._lock:
            for track in self._tracks.values():
                if track.transfer is not None:
                    await track.transfer.aclose()
                    track.transfer = None
        await self.manager.aclose()


def _range(header: str | None, size: int) -> tuple[int, int, bool]:
    if header is None:
        return 0, size, False
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not match or not any(match.groups()) or size == 0:
        raise ValueError("Unsatisfiable Range")
    first, last = match.groups()
    if first:
        start = int(first)
        end = min(int(last), size - 1) + 1 if last else size
        if start >= size or end <= start:
            raise ValueError("Unsatisfiable Range")
    else:
        suffix = int(last)
        if suffix == 0:
            raise ValueError("Unsatisfiable Range")
        start, end = max(size - suffix, 0), size
    return start, end, True


def _registry(request: Request) -> TorrentPlaybackRegistry:
    try:
        local = ipaddress.ip_address(request.client.host).is_loopback if request.client else False
    except ValueError:
        local = False
    if not local or not hasattr(request.app.state, "torrent_registry"):
        raise HTTPException(404, "Not found")
    return request.app.state.torrent_registry


async def _stream(request: Request, playback_id: str, head: bool):
    registry = _registry(request)
    file = registry.file(playback_id)
    try:
        start, end, partial = _range(request.headers.get("range"), file.size)
    except ValueError:
        return Response(status_code=416, headers={
            "Accept-Ranges": "bytes", "Content-Range": f"bytes */{file.size}", "Content-Length": "0",
        })
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start),
        "Cache-Control": "no-store",
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end - 1}/{file.size}"
    content_type = CONTENT_TYPES[file.extension]
    status = 206 if partial else 200
    if head or start == end:
        return Response(status_code=status, headers=headers, media_type=content_type)

    track = await registry.acquire(playback_id)
    try:
        first_end = min(start + registry.block_size, end)
        first = await registry.read(track, start, first_end)
    except BaseException as exc:
        await registry.release(track)
        if isinstance(exc, TimeoutError):
            raise HTTPException(504, "Torrent peer timed out") from exc
        if isinstance(exc, CacheLimitError):
            raise HTTPException(507, "Torrent disk cache limit reached") from exc
        raise

    async def body():
        try:
            yield first
            position = first_end
            while position < end:
                if await request.is_disconnected():
                    break
                next_end = min(position + registry.block_size, end)
                yield await registry.read(track, position, next_end)
                position = next_end
        finally:
            await registry.release(track)

    return StreamingResponse(body(), status_code=status, headers=headers, media_type=content_type)


@router.get("/{playback_id}")
async def get_audio(request: Request, playback_id: str):
    return await _stream(request, playback_id, False)


@router.head("/{playback_id}")
async def head_audio(request: Request, playback_id: str):
    return await _stream(request, playback_id, True)
