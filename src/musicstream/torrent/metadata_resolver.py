"""Bounded torrent-file retrieval from a trusted local Prowlarr download route."""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx

from .metadata import TorrentMetadata, _libtorrent, inspect_torrent
from .prowlarr_discovery import MusicRelease
from .track_matcher import TrackMatch, TrackReference, match_track


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
                 allowed_indexer_ids: tuple[int, ...], max_bytes: int = 4 * 1024 * 1024):
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
        url = self._validated_url(release)
        if url is None:
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

    async def resolve_track(self, reference: TrackReference,
                            release: MusicRelease) -> TorrentTrackResolution:
        result = await self.resolve(release)
        if result.metadata is None:
            return TorrentTrackResolution(result.status, reason=result.status)
        match: TrackMatch = match_track(reference, result.metadata)
        return TorrentTrackResolution(match.status, match.file_index,
                                      match.confidence, match.reason, result.metadata)
