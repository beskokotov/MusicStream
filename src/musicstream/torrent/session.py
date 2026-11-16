"""Explicit, bounded BitTorrent piece transfers for a selected audio file.

Ranges use Python's half-open [start, end) convention. This module exposes no
HTTP endpoint and importing it never creates a libtorrent session.
"""

from __future__ import annotations

import asyncio
import ipaddress
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .metadata import TorrentSettings, _from_info, _libtorrent, select_audio_file
from .timing import current_timing


class CacheLimitError(RuntimeError):
    """The requested pieces cannot fit within this session's disk budget."""


@dataclass(frozen=True)
class TorrentDiagnostics:
    """Aggregate transfer counters only; no peer addresses or tracker URLs."""

    discovered_peers: int
    connected_peers: int
    downloaded_payload_bytes: int
    verified_piece_bytes: int
    first_audio_fragment_seconds: float | None
    tracker_replies: int = 0
    tracker_errors: int = 0
    last_tracker_error_code: int | None = None
    dht_nodes: int = 0
    dht_bootstrap_complete: bool = False
    connected_seeds: int = 0
    handshakes_complete: int = 0
    available_requested_pieces: int = 0
    unchoked_piece_peers: int = 0
    no_transfer_reason: str | None = None
    listen_port: int = 0
    listen_succeeded: int = 0
    listen_failed: int = 0
    interested_peers: int = 0
    choking_piece_peers: int = 0
    queued_block_requests: int = 0
    finished_blocks: int = 0
    timed_out_blocks: int = 0
    dropped_requests: int = 0
    sent_block_requests: int = 0
    handshake_pending_peers: int = 0
    discovered_seeds: int = 0
    download_payload_rate_bytes_per_second: int = 0
    verified_requested_pieces: int = 0


class TorrentSessionManager:
    def __init__(
        self, *, settings: TorrentSettings | None = None,
        max_sessions: int = 2, max_connections: int = 8,
        network_mode: Literal["loopback", "remote_tcp", "public_discovery"] = "loopback",
        peer_connect_timeout_seconds: int = 8,
        approved_public_info_hashes: frozenset[str] = frozenset(),
        public_bind_ip: str | None = None,
        allow_unlisted_public: bool = False,
    ) -> None:
        if max_sessions < 1 or max_connections < 1:
            raise ValueError("Session and connection limits must be positive")
        if network_mode not in ("loopback", "remote_tcp", "public_discovery"):
            raise ValueError("Invalid BitTorrent network mode")
        if not 2 <= peer_connect_timeout_seconds <= 30:
            raise ValueError("Peer connection timeout must be 2-30 seconds")
        if network_mode == "public_discovery" and not approved_public_info_hashes and not allow_unlisted_public:
            raise ValueError("Public discovery requires approved info hashes")
        if allow_unlisted_public and network_mode != "public_discovery":
            raise ValueError("Unlisted public torrents require public discovery")
        if network_mode == "public_discovery":
            try:
                bind_address = ipaddress.IPv4Address(public_bind_ip)
            except (ValueError, ipaddress.AddressValueError) as exc:
                raise ValueError("Public discovery requires an explicit local IPv4 bind address") from exc
            if (bind_address.is_loopback or bind_address.is_unspecified or
                    bind_address.is_multicast or bind_address.is_link_local):
                raise ValueError("Public discovery requires a non-loopback local IPv4 address")
        elif public_bind_ip is not None:
            raise ValueError("Public bind address applies only to public discovery")
        if any(len(value) not in (40, 64) or any(char not in "0123456789abcdef" for char in value)
               for value in approved_public_info_hashes):
            raise ValueError("Invalid approved info hash")
        self.settings = settings or TorrentSettings.from_env()
        self.max_sessions = max_sessions
        self.max_connections = max_connections
        self.network_mode = network_mode
        self.peer_connect_timeout_seconds = peer_connect_timeout_seconds
        self.approved_public_info_hashes = approved_public_info_hashes
        self.allow_unlisted_public = allow_unlisted_public
        self.public_bind_ip = public_bind_ip
        self._slots = asyncio.Semaphore(max_sessions)
        self._active: set[SelectedTorrent] = set()

    def validate_peers(self, peers: tuple[tuple[str, int], ...]) -> None:
        if self.network_mode == "public_discovery" and peers:
            raise ValueError("Public discovery does not accept explicit peers")
        if len(peers) > min(self.max_connections, 4):
            raise ValueError("At most four explicit peers are supported")
        for host, port in peers:
            if not isinstance(host, str) or not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
                raise ValueError("Invalid explicit peer")
            try:
                address = ipaddress.ip_address(host)
            except ValueError as exc:
                raise ValueError("Peer must be an IP literal") from exc
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
                address = address.ipv4_mapped
            if self.network_mode == "loopback":
                if not address.is_loopback:
                    raise ValueError("Only explicit loopback peers are supported")
            elif (address.is_loopback or address.is_unspecified or address.is_multicast
                  or address.is_link_local or address.is_reserved):
                raise ValueError("Remote mode requires a routable non-loopback IP")

    def session_options(self) -> dict:
        """Only public discovery opens a UDP socket for DHT and UDP trackers."""
        settings = {
            "listen_interfaces": (f"{self.public_bind_ip}:0" if self.network_mode == "public_discovery" else
                                  "" if self.network_mode == "remote_tcp" else "127.0.0.1:0"),
            "enable_dht": self.network_mode == "public_discovery",
            "enable_upnp": False,
            "enable_natpmp": False,
            "enable_lsd": False,
            "connections_limit": self.max_connections,
            "close_redundant_connections": False,
            "min_reconnect_time": 3 if self.network_mode != "loopback" else 1,
            "disable_hash_checks": False,
        }
        if self.network_mode != "loopback":
            settings.update({
                "enable_outgoing_tcp": True,
                "enable_outgoing_utp": False,
                "enable_incoming_tcp": False,
                "enable_incoming_utp": False,
                "peer_connect_timeout": self.peer_connect_timeout_seconds,
                "handshake_timeout": self.peer_connect_timeout_seconds,
                "connection_speed": min(self.max_connections, 4),
                "max_failcount": 2,
                "max_peerlist_size": 32,
                "max_queued_disk_bytes": 2 * 1024 * 1024,
                "max_out_request_queue": 16,
                "send_buffer_watermark": 128 * 1024,
                "file_pool_size": 8,
            })
        if self.network_mode == "public_discovery":
            lt = _libtorrent()
            # Public swarms may advertise uTP-only peers. Incoming connections
            # remain disabled; no router or firewall rule is requested.
            settings["enable_outgoing_utp"] = True
            settings["max_failcount"] = 4
            settings["max_peerlist_size"] = 64
            settings["connection_speed"] = min(self.max_connections, 8)
            settings["outgoing_interfaces"] = self.public_bind_ip
            settings["alert_mask"] = (lt.alert.category_t.tracker_notification |
                                      lt.alert.category_t.dht_notification |
                                      lt.alert.category_t.connect_notification |
                                      lt.alert.category_t.block_progress_notification |
                                      lt.alert.category_t.error_notification |
                                      lt.alert.category_t.status_notification)
            settings["alert_queue_size"] = 256
        return settings

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
        self._timing = current_timing.get()
        self._peer_seen = False
        self._handshake_seen = False
        self._piece_seen = False
        self._next_milestone_sample = 0.0
        self._session = None
        self._handle = None
        self._directory: Path | None = None
        self._lock = asyncio.Lock()
        self._reserved: set[int] = set()
        self._closed = False
        self._close_task: asyncio.Task | None = None
        self._verified_pieces: set[int] = set()
        self._first_range_started: float | None = None
        self._first_audio_fragment_seconds: float | None = None
        self._requested_pieces: tuple[int, ...] = ()
        self._tracker_replies = 0
        self._tracker_errors = 0
        self._last_tracker_error_code: int | None = None
        self._dht_bootstrap_complete = False
        self._listen_succeeded = 0
        self._listen_failed = 0
        self._finished_blocks = 0
        self._timed_out_blocks = 0
        self._dropped_requests = 0
        self._sent_block_requests = 0

    async def __aenter__(self) -> SelectedTorrent:
        if self._closed:
            raise RuntimeError("Torrent session cannot be reopened")
        session_started = time.monotonic()
        await self.manager._slots.acquire()
        try:
            if self.path.suffix.lower() != ".torrent" or not self.path.is_file():
                raise ValueError("Expected an existing .torrent file")
            self.manager.validate_peers(self.peers)
            lt = _libtorrent()
            if self.manager.network_mode == "public_discovery":
                params = lt.load_torrent_file(str(self.path))
                info = params.ti
                if info.priv():
                    raise ValueError("Private torrents cannot use public discovery")
                hashes = info.info_hashes()
                identifiers = {str(hashes.v1)} if hashes.has_v1() else set()
                if hashes.has_v2():
                    identifiers.add(str(hashes.v2))
                if not (self.manager.allow_unlisted_public or
                        identifiers & self.manager.approved_public_info_hashes):
                    raise ValueError("Torrent is not approved for public discovery")
            else:
                info = lt.torrent_info(str(self.path))
                params = lt.add_torrent_params()
                params.ti = info
            self.file = select_audio_file(_from_info(info), self.file_index)
            self._info = info
            self._storage = info.files()
            self._piece_length = self._storage.piece_length()
            self._budget = self.manager.settings.cache_max_bytes // self.manager.max_sessions
            if self._piece_length > self._budget:
                raise CacheLimitError("Disk cache limit is smaller than one torrent piece")
            self.manager.settings.temp_dir.mkdir(parents=True, exist_ok=True)
            self._directory = Path(tempfile.mkdtemp(prefix="range-", dir=self.manager.settings.temp_dir))
            self._session = lt.session(self.manager.session_options())
            params.save_path = str(self._directory)
            params.storage_mode = lt.storage_mode_t.storage_mode_sparse
            params.file_priorities = [1 if i == self.file_index else 0
                                      for i in range(self._storage.num_files())]
            params.flags |= (lt.torrent_flags.override_web_seeds |
                             lt.torrent_flags.disable_lsd |
                             lt.torrent_flags.default_dont_download)
            if self.manager.network_mode == "public_discovery":
                params.flags &= ~(lt.torrent_flags.override_trackers |
                                  lt.torrent_flags.disable_dht | lt.torrent_flags.disable_pex)
            else:
                params.flags |= (lt.torrent_flags.override_trackers |
                                 lt.torrent_flags.disable_dht | lt.torrent_flags.disable_pex)
                params.trackers = []
            params.flags &= ~(lt.torrent_flags.auto_managed | lt.torrent_flags.paused)
            params.url_seeds = []
            params.http_seeds = []
            self._handle = self._session.add_torrent(params)
            if self._timing:
                self._timing.mark("p2p_session_started", operation_started=session_started)
            self._handle.set_max_connections(self.manager.max_connections)
            self._handle.prioritize_pieces([0] * self._storage.num_pieces())
            for host, port in self.peers:
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

    def next_piece_boundary(self, start: int, end: int) -> int:
        """Bound one HTTP chunk to already verifiable torrent piece data."""
        absolute = self._storage.file_offset(self.file_index) + start
        remaining = self._piece_length - absolute % self._piece_length
        return min(end, start + remaining)

    def _disk_bytes(self) -> int:
        if self._handle is None:
            return 0
        # File stat sizes include sparse zero-filled gaps before a Seek. Count
        # the bytes libtorrent has actually written for pieces instead of the
        # file's logical length, so a late Range does not exceed the cache cap.
        return sum(max(0, int(value)) for value in self._handle.file_progress())

    def _sample_first_milestones(self, pieces: tuple[int, ...], *, force: bool = False) -> None:
        if not self._timing or self._handle is None:
            return
        now = time.monotonic()
        if not force and now < self._next_milestone_sample:
            return
        self._next_milestone_sample = now + 0.1
        status = self._handle.status()
        if not self._peer_seen and status.list_peers > 0:
            self._peer_seen = True
            self._timing.mark("first_peer_discovered")
        if not self._handshake_seen and self.manager.network_mode == "public_discovery":
            lt = _libtorrent()
            if any(not peer.flags & (lt.peer_info.handshake | lt.peer_info.connecting)
                   for peer in self._handle.get_peer_info()):
                self._handshake_seen = True
                self._timing.mark("first_handshake")
        if not self._piece_seen and any(self._handle.have_piece(piece) for piece in pieces):
            self._piece_seen = True
            self._timing.mark("first_verified_piece")

    def diagnostics(self) -> TorrentDiagnostics:
        """Read only counters suitable for local diagnostics, never peer details."""
        if self._handle is None:
            raise RuntimeError("Torrent session is not open")
        status = self._handle.status()
        dht_nodes = handshakes = pending_handshakes = unchoked = interested = choking = queued = 0
        available: set[int] = set()
        if self.manager.network_mode == "public_discovery":
            lt = _libtorrent()
            for alert in self._session.pop_alerts():
                if isinstance(alert, lt.tracker_reply_alert):
                    self._tracker_replies += 1
                elif isinstance(alert, lt.tracker_error_alert):
                    self._tracker_errors += 1
                    self._last_tracker_error_code = alert.error.value()
                elif isinstance(alert, lt.dht_bootstrap_alert):
                    self._dht_bootstrap_complete = True
                elif isinstance(alert, lt.listen_succeeded_alert):
                    self._listen_succeeded += 1
                elif isinstance(alert, lt.listen_failed_alert):
                    self._listen_failed += 1
                elif isinstance(alert, lt.block_finished_alert):
                    self._finished_blocks += 1
                elif isinstance(alert, lt.block_downloading_alert):
                    self._sent_block_requests += 1
                elif isinstance(alert, lt.block_timeout_alert):
                    self._timed_out_blocks += 1
                elif isinstance(alert, lt.request_dropped_alert):
                    self._dropped_requests += 1
            dht_nodes = self._session.status().dht_nodes
            for peer in self._handle.get_peer_info():
                if peer.flags & (lt.peer_info.handshake | lt.peer_info.connecting):
                    pending_handshakes += 1
                    continue
                handshakes += 1
                queued += peer.download_queue_length
                has_wanted = False
                for piece in self._requested_pieces:
                    try:
                        if peer.pieces[piece]:
                            available.add(piece)
                            has_wanted = True
                    except (IndexError, TypeError, AttributeError):
                        continue
                if has_wanted:
                    if peer.flags & lt.peer_info.interesting:
                        interested += 1
                    if peer.flags & lt.peer_info.remote_choked:
                        choking += 1
                    else:
                        unchoked += 1
        reason = None
        if self.manager.network_mode == "public_discovery" and status.total_payload_download == 0:
            if not self._requested_pieces:
                reason = "no_range_requested"
            elif status.num_peers == 0:
                reason = "no_connected_peers"
            elif handshakes == 0:
                reason = "handshake_pending"
            elif not available:
                reason = "requested_pieces_not_advertised"
            elif interested == 0:
                reason = "not_interested_in_available_piece"
            elif unchoked == 0:
                reason = "requested_piece_peers_choking"
            else:
                reason = "awaiting_payload"
        return TorrentDiagnostics(
            discovered_peers=max(0, status.list_peers),
            connected_peers=max(0, status.num_peers),
            downloaded_payload_bytes=max(0, status.total_payload_download),
            verified_piece_bytes=sum(self._info.piece_size(piece) for piece in self._verified_pieces),
            first_audio_fragment_seconds=self._first_audio_fragment_seconds,
            tracker_replies=self._tracker_replies,
            tracker_errors=self._tracker_errors,
            last_tracker_error_code=self._last_tracker_error_code,
            dht_nodes=dht_nodes,
            dht_bootstrap_complete=self._dht_bootstrap_complete,
            connected_seeds=status.num_seeds if self.manager.network_mode == "public_discovery" else 0,
            handshakes_complete=handshakes,
            available_requested_pieces=len(available),
            unchoked_piece_peers=unchoked,
            no_transfer_reason=reason,
            listen_port=self._session.listen_port() if self.manager.network_mode == "public_discovery" else 0,
            listen_succeeded=self._listen_succeeded,
            listen_failed=self._listen_failed,
            interested_peers=interested,
            choking_piece_peers=choking,
            queued_block_requests=queued,
            finished_blocks=self._finished_blocks,
            timed_out_blocks=self._timed_out_blocks,
            dropped_requests=self._dropped_requests,
            sent_block_requests=self._sent_block_requests,
            handshake_pending_peers=pending_handshakes,
            discovered_seeds=max(0, getattr(status, "list_seeds", 0)),
            download_payload_rate_bytes_per_second=max(0, getattr(status, "download_payload_rate", 0)),
            verified_requested_pieces=len(self._verified_pieces),
        )

    async def read_range(self, start: int, end: int, *, timeout_seconds: float = 10) -> bytes:
        """Return bytes only after every intersecting piece passes libtorrent verification."""
        pieces = self.pieces_for_range(start, end)
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        async with self._lock:
            if self._closed:
                raise RuntimeError("Torrent session is closed")
            if self._first_range_started is None:
                self._first_range_started = time.monotonic()
            # Keep the reservation/prioritization window local to the current
            # read. Historical pieces remain hash-checked on disk, but must not
            # consume the in-flight reservation budget for the rest of a track.
            if len(set(pieces)) * self._piece_length > self._budget:
                raise CacheLimitError("Requested pieces exceed disk cache limit")
            self._requested_pieces = pieces
            upcoming_pieces: set[int] = set()
            try:
                file_last = ((self._storage.file_offset(self.file_index) + self.file.size - 1)
                             // self._piece_length)
                if self.manager.network_mode == "public_discovery":
                    window_capacity = max(1, self._budget // self._piece_length)
                    prefetch_capacity = max(0, window_capacity - len(pieces))
                    upcoming_pieces = set(range(
                        pieces[-1] + 1,
                        min(pieces[-1] + 1 + min(2, prefetch_capacity), file_last + 1),
                    ))
                priority_window = set(pieces) | upcoming_pieces
                for obsolete in self._reserved - priority_window:
                    self._handle.piece_priority(obsolete, 0)
                self._reserved = priority_window
                for piece in pieces:
                    self._handle.piece_priority(piece, 7)
                # Request the next two pieces while the first verified piece
                # is still arriving. The browser otherwise waits for a fresh
                # peer round trip at every HTTP chunk boundary.
                if self.manager.network_mode == "public_discovery":
                    for upcoming in sorted(upcoming_pieces):
                        self._handle.piece_priority(upcoming, 6)
                if self.manager.network_mode == "loopback":
                    for host, port in self.peers:
                        self._handle.connect_peer((host, port))
                last_progress = time.monotonic()
                next_peer_retry = last_progress + 1.0
                adaptive = self.manager.network_mode == "public_discovery"
                last_payload = self._handle.status().total_payload_download if adaptive else 0
                while not all(self._handle.have_piece(piece) for piece in pieces):
                    self._sample_first_milestones(pieces)
                    if self._closed:
                        raise RuntimeError("Torrent session was closed")
                    if self._disk_bytes() > self._budget:
                        raise CacheLimitError("Disk cache limit exceeded")
                    now = time.monotonic()
                    if self.peers and now >= next_peer_retry:
                        # A previously selected explicit peer may be restarting.
                        # Retry within the live Range wait instead of requiring a
                        # new torrent session after the peer returns.
                        for host, port in self.peers:
                            self._handle.connect_peer((host, port))
                        next_peer_retry = now + 1.0
                    payload = self._handle.status().total_payload_download if adaptive else 0
                    if payload > last_payload:
                        last_payload = payload
                        last_progress = now
                    elif now - last_progress >= timeout_seconds:
                        raise TimeoutError("Torrent payload stalled")
                    await asyncio.sleep(0.02)
                self._sample_first_milestones(pieces, force=True)
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
                self._verified_pieces.update(pieces)
                if self._first_audio_fragment_seconds is None:
                    self._first_audio_fragment_seconds = time.monotonic() - self._first_range_started
                return data
            finally:
                if self._handle is not None:
                    for piece in pieces:
                        if piece not in upcoming_pieces:
                            self._handle.piece_priority(piece, 0)
