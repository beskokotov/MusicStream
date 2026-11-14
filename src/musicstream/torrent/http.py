"""Local-only, opt-in HTTP byte-range adapter for registered torrent tracks."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from .metadata import TorrentFile, inspect_torrent, select_audio_file
from .session import CacheLimitError, SelectedTorrent, TorrentDiagnostics, TorrentSessionManager


CONTENT_TYPES = {".mp3": "audio/mpeg", ".flac": "audio/flac", ".ogg": "audio/ogg", ".m4a": "audio/mp4"}
router = APIRouter(prefix="/api/experimental/torrent", include_in_schema=False)
logger = logging.getLogger(__name__)


class _VerifiedRangeResponse(StreamingResponse):
    """On a later piece failure, leave Content-Length incomplete so clients reject it."""

    async def stream_response(self, send):
        await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
        try:
            async for chunk in self.body_iterator:
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
        except (IOError, TimeoutError, RuntimeError) as exc:
            # No terminal body: ASGI server closes an incomplete Content-Length
            # response. The browser must discard/retry the truncated range.
            logger.warning("Verified Web Seed Range interrupted: %s", type(exc).__name__)
            return
        await send({"type": "http.response.body", "body": b"", "more_body": False})


@dataclass
class _Track:
    path: Path
    file: TorrentFile
    peers: tuple[tuple[str, int], ...]
    transfer: SelectedTorrent | None = None
    users: int = 0
    idle_since: float = 0
    first_range_seconds: float | None = None
    interrupted_ranges: int = 0


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
        if not peers and self.manager.network_mode not in ("public_discovery", "safe_webseed"):
            raise ValueError("An explicit peer is required")
        self.manager.validate_peers(peers)
        self._tracks[playback_id] = _Track(path, file, peers)
        return file

    def file(self, playback_id: str) -> TorrentFile:
        try:
            return self._tracks[playback_id].file
        except KeyError as exc:
            raise HTTPException(404, "Unknown torrent playback") from exc

    def diagnostics(self, playback_id: str) -> TorrentDiagnostics | None:
        """Server-side aggregate counters; no HTTP route exposes them."""
        track = self._tracks.get(playback_id)
        if track is None:
            raise KeyError(playback_id)
        return track.transfer.diagnostics() if track.transfer is not None else None

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
                    if self.manager.network_mode == "safe_webseed":
                        # A new song revokes the old transfer and kills its
                        # in-flight HTTPS worker before taking the single slot.
                        for other in self._tracks.values():
                            if other is not track and other.transfer is not None:
                                await other.transfer.aclose()
                                other.transfer = None
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
            try:
                await self._reaper
            except asyncio.CancelledError:
                pass
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

    prepared_at = time.monotonic()
    track = await registry.acquire(playback_id)
    if registry.manager.network_mode == "safe_webseed":
        first_end = min(start + registry.block_size, end)
        read_task = asyncio.create_task(registry.read(track, start, first_end))
        try:
            while not read_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(read_task), timeout=0.1)
                except TimeoutError:
                    if await request.is_disconnected():
                        read_task.cancel()
                        await asyncio.gather(read_task, return_exceptions=True)
                        raise HTTPException(499, "Client disconnected")
            first = await read_task
            if track.first_range_seconds is None:
                track.first_range_seconds = time.monotonic() - prepared_at
                logger.info("First verified Web Seed Range prepared in %.3f s", track.first_range_seconds)
        except BaseException as exc:
            if not read_task.done():
                read_task.cancel()
                await asyncio.gather(read_task, return_exceptions=True)
            await registry.release(track)
            if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
                raise HTTPException(504, "Web Seed timed out") from exc
            if isinstance(exc, (IOError, RuntimeError)):
                raise HTTPException(502, "Verified Web Seed bytes unavailable") from exc
            raise
    else:
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
            if first:
                yield first
            position = first_end
            while position < end:
                if await request.is_disconnected():
                    break
                next_end = min(position + registry.block_size, end)
                try:
                    yield await registry.read(track, position, next_end)
                except (IOError, TimeoutError, RuntimeError):
                    track.interrupted_ranges += 1
                    raise
                position = next_end
        finally:
            await registry.release(track)

    response_type = _VerifiedRangeResponse if registry.manager.network_mode == "safe_webseed" else StreamingResponse
    return response_type(body(), status_code=status, headers=headers, media_type=content_type)


@router.get("/{playback_id}")
async def get_audio(request: Request, playback_id: str):
    return await _stream(request, playback_id, False)


@router.head("/{playback_id}")
async def head_audio(request: Request, playback_id: str):
    return await _stream(request, playback_id, True)
