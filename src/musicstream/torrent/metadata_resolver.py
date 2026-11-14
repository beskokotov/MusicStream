"""Bounded torrent-file retrieval from a trusted local Prowlarr download route."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx

from .metadata import TorrentMetadata, _from_info, _libtorrent, inspect_torrent
from .prowlarr_discovery import MusicRelease
from .track_matcher import TrackMatch, TrackReference, match_track
from .timing import current_timing
from .cache import TTLCache


@dataclass(frozen=True)
class MetadataResolution:
    status: str
    metadata: TorrentMetadata | None = None
    torrent_bytes: bytes | None = None


@dataclass(frozen=True)
class TorrentTrackResolution:
    status: str
    file_index: int | None = None
    confidence: float = 0.0
    reason: str = ""
    metadata: TorrentMetadata | None = None


class TorrentMetadataResolver:
    """Accept only a local Prowlarr route; never follow or fetch external URLs."""

    def __init__(self, client: httpx.AsyncClient, *, api_key: str | None = None,
                 base_url: str = "http://127.0.0.1:18746",
                 allowed_indexer_ids: tuple[int, ...], max_bytes: int = 4 * 1024 * 1024,
                 approved_info_hashes: frozenset[str] = frozenset(),
                 allow_unlisted_public: bool = False, public_bind_ip: str | None = None):
        base = urlsplit(base_url)
        if (base.scheme != "http" or base.hostname not in {"127.0.0.1", "::1"}
                or base.path not in ("", "/") or base.username or base.password
                or base.query or base.fragment):
            raise ValueError("Resolver requires a local Prowlarr endpoint")
        if not allowed_indexer_ids or any(i <= 0 for i in allowed_indexer_ids):
            raise ValueError("Explicit indexer allowlist is required")
        if not 1024 <= max_bytes <= 16 * 1024 * 1024:
            raise ValueError("max_bytes must be between 1 and 16 MiB")
        self.client = client
        self.api_key = api_key or os.environ.get("MUSICSTREAM_PROWLARR_API_KEY")
        if not self.api_key:
            raise ValueError("Set MUSICSTREAM_PROWLARR_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.allowed_indexer_ids = allowed_indexer_ids
        self.max_bytes = max_bytes
        self.approved_info_hashes = approved_info_hashes
        self.allow_unlisted_public = allow_unlisted_public
        self.public_bind_ip = public_bind_ip
        self._metadata_cache: TTLCache[MetadataResolution] = TTLCache(
            ttl_seconds=300, max_entries=24, max_bytes=32 * 1024 * 1024)

    def _validated_url(self, release: MusicRelease) -> str | None:
        if release.source != "prowlarr" or release.indexer_id not in self.allowed_indexer_ids:
            return None
        value = release.metadata_url
        if not value:
            return None
        try:
            url, base = urlsplit(value), urlsplit(self.base_url)
            origin = (url.scheme, url.hostname, url.port)
            expected = (base.scheme, base.hostname, base.port)
        except ValueError:
            return None
        if origin != expected:
            return None
        if url.username or url.password or url.fragment or url.path != f"/{release.indexer_id}/download":
            return None
        fields = parse_qsl(url.query, keep_blank_values=True)
        if (not fields or any(k not in {"link", "file"} for k, _ in fields)
                or sum(k == "link" for k, _ in fields) != 1
                or not next(v for k, v in fields if k == "link")):
            return None
        if any(len(v) > 4096 for _, v in fields) or len(value) > 8192:
            return None
        return value

    async def resolve(self, release: MusicRelease) -> MetadataResolution:
        started = time.monotonic()
        cache_key = hashlib.sha256(
            (release.source_id + "\0" + (release.info_hash or "") + "\0" +
             (release.metadata_url or "")).encode("utf-8")).digest()
        can_read_cache = (release.source == "prowlarr" and
                          release.indexer_id in self.allowed_indexer_ids and
                          (not release.info_hash or self.allow_unlisted_public or
                           release.info_hash.lower() in self.approved_info_hashes))
        cached = self._metadata_cache.get(cache_key) if can_read_cache else None
        if cached is not None:
            timing = current_timing.get()
            if timing:
                timing.mark("metadata_cache_hit", indexer_id=release.indexer_id)
            return cached
        result = await self._resolve_uncached(release)
        timing = current_timing.get()
        if timing:
            timing.mark("torrent_metadata", operation_started=started,
                        indexer_id=release.indexer_id, status=result.status)
        if (result.status == "ok" and result.torrent_bytes and
                not _libtorrent().load_torrent_buffer(result.torrent_bytes).ti.priv() and
                not re.search(rb"(?i)passkey|api[_-]?key|auth(?:orization)?|token",
                              result.torrent_bytes) and
                not re.search(r"(?i)passkey|api[_-]?key|auth(?:orization)?|token",
                              release.source_id + " " + (release.metadata_url or ""))):
            self._metadata_cache.put(cache_key, result, size=len(result.torrent_bytes))
        return result

    async def _resolve_uncached(self, release: MusicRelease) -> MetadataResolution:
        url = self._validated_url(release)
        if url is None:
            if (release.source == "prowlarr" and release.indexer_id in self.allowed_indexer_ids
                    and release.info_hash and self.public_bind_ip):
                return await self._resolve_info_hash(release.info_hash)
            return MetadataResolution("unsupported_source")
        try:
            async with self.client.stream(
                "GET", url, headers={"X-Api-Key": self.api_key},
                follow_redirects=False, timeout=20,
            ) as response:
                if 300 <= response.status_code < 400:
                    return MetadataResolution("redirect_rejected")
                if response.status_code in (401, 403):
                    return MetadataResolution("auth_error")
                if response.status_code >= 500:
                    return MetadataResolution("unavailable")
                if response.status_code != 200:
                    return MetadataResolution("http_error")
                declared = response.headers.get("content-length")
                if declared and declared.isdecimal() and int(declared) > self.max_bytes:
                    return MetadataResolution("too_large")
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(payload) + len(chunk) > self.max_bytes:
                        return MetadataResolution("too_large")
                    payload.extend(chunk)
        except httpx.RequestError:
            return MetadataResolution("unavailable")
        if not payload or not re.match(rb"^d", payload):
            return MetadataResolution("invalid_torrent")
        try:
            _libtorrent()
        except RuntimeError:
            return MetadataResolution("dependency_missing")
        try:
            with tempfile.TemporaryDirectory(prefix="musicstream-metadata-") as directory:
                path = Path(directory) / "release.torrent"
                path.write_bytes(payload)
                metadata = inspect_torrent(path)
        except (RuntimeError, ValueError, OSError):
            return MetadataResolution("invalid_torrent")
        return MetadataResolution("ok", metadata, bytes(payload))

    async def _resolve_info_hash(self, info_hash: str) -> MetadataResolution:
        normalized = info_hash.lower()
        if not re.fullmatch(r"[0-9a-f]{40}", normalized):
            return MetadataResolution("unsupported_source")
        if not (self.allow_unlisted_public or normalized in self.approved_info_hashes):
            return MetadataResolution("unsupported_source")
        stop = threading.Event()
        try:
            return await asyncio.to_thread(self._fetch_info_hash, normalized, stop)
        except asyncio.CancelledError:
            stop.set()
            raise

    def _fetch_info_hash(self, info_hash: str, stop: threading.Event) -> MetadataResolution:
        """Use DHT for metadata only; do not request audio pieces or accept redirects."""
        try:
            lt = _libtorrent()
            with tempfile.TemporaryDirectory(prefix="musicstream-magnet-") as directory:
                session = lt.session({
                    "listen_interfaces": f"{self.public_bind_ip}:0",
                    "outgoing_interfaces": self.public_bind_ip,
                    "enable_dht": True,
                    "enable_upnp": False,
                    "enable_natpmp": False,
                    "enable_lsd": False,
                    "enable_incoming_tcp": False,
                    "enable_incoming_utp": False,
                    "connections_limit": 12,
                    "max_metadata_size": self.max_bytes,
                })
                try:
                    params = lt.parse_magnet_uri(f"magnet:?xt=urn:btih:{info_hash}")
                    params.save_path = directory
                    params.flags |= lt.torrent_flags.upload_mode | lt.torrent_flags.default_dont_download
                    params.trackers = []
                    params.url_seeds = []
                    params.http_seeds = []
                    handle = session.add_torrent(params)
                    deadline = time.monotonic() + 15
                    while not stop.is_set() and time.monotonic() < deadline:
                        if handle.has_metadata():
                            info = handle.torrent_file()
                            if info.priv() or str(info.info_hashes().v1).lower() != info_hash:
                                return MetadataResolution("invalid_torrent")
                            payload = lt.bencode({b"info": lt.bdecode(info.metadata())})
                            if len(payload) > self.max_bytes:
                                return MetadataResolution("too_large")
                            path = Path(directory) / "resolved.torrent"
                            path.write_bytes(payload)
                            return MetadataResolution("ok", _from_info(info), payload)
                        stop.wait(0.2)
                    return MetadataResolution("unavailable")
                finally:
                    session.pause()
                    if "handle" in locals():
                        session.remove_torrent(handle)
                    del session
        except (RuntimeError, ValueError, OSError):
            return MetadataResolution("unavailable")

    async def resolve_track(self, reference: TrackReference,
                            release: MusicRelease) -> TorrentTrackResolution:
        result = await self.resolve(release)
        if result.metadata is None:
            return TorrentTrackResolution(result.status, reason=result.status)
        match: TrackMatch = match_track(reference, result.metadata)
        return TorrentTrackResolution(match.status, match.file_index,
                                      match.confidence, match.reason, result.metadata)
