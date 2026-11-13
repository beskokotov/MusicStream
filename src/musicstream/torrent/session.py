"""Explicit, bounded BitTorrent piece transfers for a selected audio file.

Ranges use Python's half-open [start, end) convention. This module exposes no
HTTP endpoint and importing it never creates a libtorrent session.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

from .metadata import TorrentSettings, _from_info, _libtorrent, select_audio_file


class CacheLimitError(RuntimeError):
    """The requested pieces cannot fit within this session's disk budget."""


class TorrentSessionManager:
    def __init__(
        self, *, settings: TorrentSettings | None = None,
        max_sessions: int = 2, max_connections: int = 8,
    ) -> None:
        if max_sessions < 1 or max_connections < 1:
            raise ValueError("Session and connection limits must be positive")
        self.settings = settings or TorrentSettings.from_env()
        self.max_sessions = max_sessions
        self.max_connections = max_connections
        self._slots = asyncio.Semaphore(max_sessions)
        self._active: set[SelectedTorrent] = set()

    @property
    def active_sessions(self) -> int:
        return len(self._active)

    def open_torrent(
        self, path: str | Path, file_index: int,
        *, peers: tuple[tuple[str, int], ...] = (),
    ) -> SelectedTorrent:
        """Construct a context; the network session starts only on __aenter__."""
        return SelectedTorrent(self, Path(path), file_index, peers)

    async def aclose(self) -> None:
        for selected in tuple(self._active):
            await selected.aclose()


class SelectedTorrent:
    def __init__(
        self, manager: TorrentSessionManager, path: Path, file_index: int,
        peers: tuple[tuple[str, int], ...],
    ) -> None:
        self.manager = manager
        self.path = path
        self.file_index = file_index
        self.peers = peers
        self._session = None
        self._handle = None
        self._directory: Path | None = None
        self._lock = asyncio.Lock()
        self._reserved: set[int] = set()
        self._closed = False
        self._close_task: asyncio.Task | None = None

    async def __aenter__(self) -> SelectedTorrent:
        if self._closed:
            raise RuntimeError("Torrent session cannot be reopened")
        await self.manager._slots.acquire()
        try:
            if self.path.suffix.lower() != ".torrent" or not self.path.is_file():
                raise ValueError("Expected an existing .torrent file")
            lt = _libtorrent()
            info = lt.torrent_info(str(self.path))
            self.file = select_audio_file(_from_info(info), self.file_index)
            self._storage = info.files()
            self._piece_length = self._storage.piece_length()
            self._budget = self.manager.settings.cache_max_bytes // self.manager.max_sessions
            if self._piece_length > self._budget:
                raise CacheLimitError("Disk cache limit is smaller than one torrent piece")
            self.manager.settings.temp_dir.mkdir(parents=True, exist_ok=True)
            self._directory = Path(tempfile.mkdtemp(prefix="range-", dir=self.manager.settings.temp_dir))
            self._session = lt.session({
                "listen_interfaces": "127.0.0.1:0",
                "enable_dht": False,
                "enable_upnp": False,
                "enable_natpmp": False,
                "enable_lsd": False,
                "connections_limit": self.manager.max_connections,
                "close_redundant_connections": False,
                "min_reconnect_time": 1,
            })
            params = lt.add_torrent_params()
            params.ti = info
            params.save_path = str(self._directory)
            params.file_priorities = [1 if i == self.file_index else 0
                                      for i in range(self._storage.num_files())]
            params.flags |= (lt.torrent_flags.override_trackers |
                             lt.torrent_flags.override_web_seeds |
                             lt.torrent_flags.default_dont_download)
            params.flags &= ~(lt.torrent_flags.auto_managed | lt.torrent_flags.paused)
            params.trackers = []
            params.url_seeds = []
            params.http_seeds = []
            self._handle = self._session.add_torrent(params)
            self._handle.set_max_connections(self.manager.max_connections)
            self._handle.prioritize_pieces([0] * self._storage.num_pieces())
            for host, port in self.peers:
                if host not in ("127.0.0.1", "::1") or not 1 <= port <= 65535:
                    raise ValueError("Only explicit loopback peers are supported")
                self._handle.connect_peer((host, port))
            self.manager._active.add(self)
            return self
        except BaseException:
            await self.aclose()
            raise

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._finish_close())
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            await self._close_task
            raise

    async def _finish_close(self) -> None:
        async with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        session, handle = self._session, self._handle
        self._session = self._handle = None
        try:
            if session is not None:
                session.pause()
                if handle is not None:
                    session.remove_torrent(handle)
        finally:
            del handle, session
            if self._directory is not None:
                shutil.rmtree(self._directory)
                self._directory = None
            self.manager._active.discard(self)
            self.manager._slots.release()

    def pieces_for_range(self, start: int, end: int) -> tuple[int, ...]:
        if self._closed or self._handle is None:
            raise RuntimeError("Torrent session is not open")
        if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end <= start or end > self.file.size:
            raise ValueError("Invalid half-open byte range")
        absolute_start = self._storage.file_offset(self.file_index) + start
        absolute_end = self._storage.file_offset(self.file_index) + end
        return tuple(range(absolute_start // self._piece_length,
                           (absolute_end - 1) // self._piece_length + 1))

    def _disk_bytes(self) -> int:
        return sum(p.stat().st_size for p in self._directory.rglob("*") if p.is_file())

    async def read_range(self, start: int, end: int, *, timeout_seconds: float = 10) -> bytes:
        """Return bytes only after every intersecting piece passes libtorrent verification."""
        pieces = self.pieces_for_range(start, end)
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        async with self._lock:
            if self._closed:
                raise RuntimeError("Torrent session is closed")
            if len(set(pieces) | self._reserved) * self._piece_length > self._budget:
                raise CacheLimitError("Requested pieces exceed disk cache limit")
            self._reserved.update(pieces)
            try:
                for piece in pieces:
                    self._handle.piece_priority(piece, 7)
                for host, port in self.peers:
                    self._handle.connect_peer((host, port))
                async with asyncio.timeout(timeout_seconds):
                    while not all(self._handle.have_piece(piece) for piece in pieces):
                        if self._closed:
                            raise RuntimeError("Torrent session was closed")
                        if self._disk_bytes() > self._budget:
                            raise CacheLimitError("Disk cache limit exceeded")
                        await asyncio.sleep(0.02)
                if self._disk_bytes() > self._budget:
                    raise CacheLimitError("Disk cache limit exceeded")
                target = (self._directory / self.file.path).resolve()
                if not target.is_relative_to(self._directory.resolve()):
                    raise ValueError("Unsafe torrent file path")
                with target.open("rb") as stream:
                    stream.seek(start)
                    data = stream.read(end - start)
                if len(data) != end - start:
                    raise IOError("Verified piece data is not yet available on disk")
                return data
            finally:
                if self._handle is not None:
                    for piece in pieces:
                        self._handle.piece_priority(piece, 0)
