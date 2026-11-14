"""Opt-in verified-piece adapter for the existing local HTTP Range router."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import struct
import sys
import time
from collections import OrderedDict
from pathlib import Path

from .metadata import _from_info, select_audio_file
from .safe_webseed import SafeWebSeedFetcher, WebSeedDiagnostics


class WebSeedWorkerError(IOError):
    def __init__(self, piece_index: int, code: int):
        super().__init__(f"Web Seed worker failed for piece {piece_index} (code {code})")
        self.code = code


class SafeWebSeedManager:
    network_mode = "safe_webseed"

    def __init__(
        self, *, approved_info_hashes: frozenset[str],
        archive_item: str = "amp021",
        cache_max_bytes: int = 4 * 1024 * 1024, worker_timeout_seconds: float = 40,
        max_download_bytes: int = 16 * 1024 * 1024,
        prefetch_pieces: int = 4,
    ) -> None:
        if not approved_info_hashes or not 1024 * 1024 <= cache_max_bytes <= 16 * 1024 * 1024:
            raise ValueError("A bounded cache and approved info hash are required")
        if not 5 <= worker_timeout_seconds <= 60:
            raise ValueError("Invalid worker timeout")
        if not 1024 * 1024 <= max_download_bytes <= 64 * 1024 * 1024:
            raise ValueError("Invalid download limit")
        if not 1 <= prefetch_pieces <= 4:
            raise ValueError("Invalid piece prefetch limit")
        self.approved_info_hashes = approved_info_hashes
        self.archive_item = archive_item
        self.cache_max_bytes = cache_max_bytes
        self.worker_timeout_seconds = worker_timeout_seconds
        self.max_download_bytes = max_download_bytes
        self.prefetch_pieces = prefetch_pieces
        self._slot = asyncio.Semaphore(1)
        self._active: set[SafeWebSeedTransfer] = set()

    @property
    def active_sessions(self) -> int:
        return len(self._active)

    def validate_peers(self, peers: tuple[tuple[str, int], ...]) -> None:
        if peers:
            raise ValueError("Safe Web Seed mode does not accept BitTorrent peers")

    def open_torrent(self, path: str | Path, file_index: int, *, peers=()) -> SafeWebSeedTransfer:
        self.validate_peers(peers)
        return SafeWebSeedTransfer(self, Path(path), file_index)

    async def aclose(self) -> None:
        for transfer in tuple(self._active):
            await transfer.aclose()


class SafeWebSeedTransfer:
    def __init__(self, manager: SafeWebSeedManager, path: Path, file_index: int) -> None:
        self.manager = manager
        self.path = path
        self.file_index = file_index
        self._fetcher: SafeWebSeedFetcher | None = None
        self._cache: OrderedDict[int, bytes] = OrderedDict()
        self._cache_bytes = 0
        self._lock = asyncio.Lock()
        self._closed = False
        self._reader: asyncio.Task | None = None
        self._reader_done = asyncio.Event()
        self._reader_done.set()
        self._started: float | None = None
        self._first_ready: float | None = None
        self._received_bytes = 0
        self._verified_pieces = 0
        self._attempted_bytes = 0
        self._connections = 0
        self._http_errors = 0
        self._redirects: list[str] = []
        self._timings = dict.fromkeys(("dns", "tcp", "tls", "response", "body", "sha1"), 0.0)
        self._retry_count = 0
        self._worker_seconds = 0.0
        self._failed_attempt_seconds = 0.0
        self._reused_connections = 0
        self._next_sequential_piece: int | None = None

    async def __aenter__(self) -> SafeWebSeedTransfer:
        await self.manager._slot.acquire()
        try:
            self._fetcher = SafeWebSeedFetcher(
                self.path, approved_info_hashes=self.manager.approved_info_hashes,
                archive_item=self.manager.archive_item, max_piece_bytes=1024 * 1024,
                max_redirects=3, max_connections=8, timeout_seconds=30,
            )
            self._info = self._fetcher._info
            self._storage = self._fetcher._storage
            self.file = select_audio_file(_from_info(self._info), self.file_index)
            self._hash = str(self._info.info_hashes().v1)
            self.manager._active.add(self)
            return self
        except BaseException:
            self.manager._slot.release()
            raise

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        reader = self._reader
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            # The read task signals this before it can call registry.release(),
            # avoiding a lock inversion with TorrentPlaybackRegistry.unregister.
            await self._reader_done.wait()
        self._cache.clear()
        self._cache_bytes = 0
        self.manager._active.discard(self)
        self.manager._slot.release()

    def diagnostics(self) -> WebSeedDiagnostics:
        base = self._fetcher.diagnostics()
        return WebSeedDiagnostics(
            base.allowed_web_seeds, base.rejected_web_seeds,
            self._connections, self._received_bytes, self._verified_pieces,
            self._http_errors, self._first_ready, tuple(self._redirects),
            *(self._timings[key] for key in ("dns", "tcp", "tls", "response", "body", "sha1")),
            self._retry_count,
            self._worker_seconds, self._failed_attempt_seconds,
            self._reused_connections,
        )

    def _remember(self, index: int, piece: bytes) -> None:
        if len(piece) > self.manager.cache_max_bytes:
            raise ValueError("Piece exceeds verified cache limit")
        while self._cache and self._cache_bytes + len(piece) > self.manager.cache_max_bytes:
            _, old = self._cache.popitem(last=False)
            self._cache_bytes -= len(old)
        self._cache[index] = piece
        self._cache_bytes += len(piece)

    async def _fetch_piece(self, index: int, timeout_seconds: float) -> bytes:
        return (await self._fetch_batch(index, 1, timeout_seconds))[0]

    async def _fetch_batch(self, index: int, count: int, timeout_seconds: float) -> tuple[bytes, ...]:
        environment = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "PYTHONPATH")
                       if key in os.environ}
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "musicstream.torrent.safe_webseed_worker",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=environment,
        )
        try:
            request = json.dumps({
                "torrent_path": str(self.path), "info_hash": self._hash,
                "piece_index": index, "piece_count": count,
                "archive_item": self.manager.archive_item,
            }).encode() + b"\n"
            process.stdin.write(request)
            await process.stdin.drain()
            process.stdin.close()

            async def receive() -> tuple[bytes, dict]:
                header = await process.stdout.readexactly(4)
                size = struct.unpack("!I", header)[0]
                expected_sizes = tuple(self._info.piece_size(number) for number in range(index, index + count))
                if size != sum(expected_sizes) or size > 4 * 1024 * 1024:
                    raise IOError("Invalid Web Seed worker piece size")
                piece = await process.stdout.readexactly(size)
                summary_size = struct.unpack("!H", await process.stdout.readexactly(2))[0]
                if summary_size > 1024:
                    raise IOError("Invalid Web Seed worker diagnostics")
                summary = json.loads(await process.stdout.readexactly(summary_size))
                if await process.wait() != 0:
                    raise IOError("Web Seed worker failed")
                return piece, summary

            piece, summary = await asyncio.wait_for(receive(), timeout=min(timeout_seconds,
                                                    self.manager.worker_timeout_seconds))
            pieces = []
            offset = 0
            for number in range(index, index + count):
                length = self._info.piece_size(number)
                current = piece[offset:offset + length]
                if hashlib.sha1(current).digest() != bytes(self._info.hash_for_piece(number)):
                    raise IOError("Web Seed worker returned an unverified piece")
                pieces.append(current)
                offset += length
            self._connections += int(summary["connections"])
            self._http_errors += int(summary["http_errors"])
            self._reused_connections += int(summary.get("reused_connections", 0))
            self._redirects.extend(str(host) for host in summary["redirects"])
            for key in self._timings:
                self._timings[key] += float(summary.get("timings", {}).get(key, 0.0))
            self._received_bytes += len(piece)
            self._verified_pieces += count
            return tuple(pieces)
        except asyncio.IncompleteReadError as exc:
            code = await process.wait()
            self._http_errors += 1
            raise WebSeedWorkerError(index, code) from exc
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def read_range(self, start: int, end: int, *, timeout_seconds: float = 40) -> bytes:
        if (self._closed or self._fetcher is None):
            raise RuntimeError("Web Seed transfer is closed")
        if (isinstance(start, bool) or isinstance(end, bool) or
                not isinstance(start, int) or not isinstance(end, int) or
                start < 0 or end <= start or end > self.file.size):
            raise ValueError("Invalid half-open byte range")
        async with self._lock:
            self._reader = asyncio.current_task()
            self._reader_done.clear()
            try:
                if self._closed:
                    raise RuntimeError("Web Seed transfer is closed")
                if self._started is None:
                    self._started = time.monotonic()
                absolute = self._storage.file_offset(self.file_index) + start
                absolute_end = self._storage.file_offset(self.file_index) + end
                length = self._storage.piece_length()
                deadline = time.monotonic() + timeout_seconds
                result = bytearray()
                for index in range(absolute // length, (absolute_end - 1) // length + 1):
                    if self._closed:
                        raise RuntimeError("Web Seed transfer was closed")
                    piece = self._cache.get(index)
                    if piece is None:
                        last_selected_piece = ((self._storage.file_offset(self.file_index) +
                                                self.file.size - 1) // length)
                        count = 1
                        if index == self._next_sequential_piece:
                            count = min(self.manager.prefetch_pieces,
                                        last_selected_piece - index + 1,
                                        max(1, self.manager.cache_max_bytes // length))
                        span_bytes = sum(self._info.piece_size(number) for number in range(index, index + count))
                        while count > 1 and self._attempted_bytes + span_bytes > self.manager.max_download_bytes:
                            count -= 1
                            span_bytes -= self._info.piece_size(index + count)
                        for attempt in range(2):
                            if self._attempted_bytes + span_bytes > self.manager.max_download_bytes:
                                raise IOError("Web Seed transfer download limit reached")
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise TimeoutError("Web Seed range deadline exceeded")
                            self._attempted_bytes += span_bytes
                            attempt_started = time.monotonic()
                            try:
                                pieces = ((await self._fetch_piece(index, remaining),) if count == 1 else
                                          await self._fetch_batch(index, count, remaining))
                                break
                            except (WebSeedWorkerError, TimeoutError) as exc:
                                self._failed_attempt_seconds += time.monotonic() - attempt_started
                                if attempt or (isinstance(exc, WebSeedWorkerError) and exc.code not in (11, 13)):
                                    raise
                                self._retry_count += 1
                            finally:
                                self._worker_seconds += time.monotonic() - attempt_started
                        for number, fetched in enumerate(pieces, index):
                            self._remember(number, fetched)
                        self._next_sequential_piece = index + count
                        piece = pieces[0]
                        if self._first_ready is None:
                            self._first_ready = time.monotonic() - self._started
                    else:
                        self._cache.move_to_end(index)
                    offset = index * length
                    result.extend(piece[max(absolute, offset) - offset:
                                        min(absolute_end, offset + len(piece)) - offset])
                if len(result) != end - start:
                    raise IOError("Verified pieces did not cover requested range")
                return bytes(result)
            finally:
                self._reader = None
                self._reader_done.set()
