"""Local-only automatic Play preparation; no source identifiers reach the browser."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from musicstream.providers.base import Track

from .http import TorrentPlaybackRegistry
from .metadata import inspect_torrent, select_audio_file
from .metadata_resolver import MetadataResolution
from .prowlarr_discovery import MusicRelease, ProwlarrDiscoveryProvider
from .release_selection import rank_releases
from .track_matcher import TrackReference


router = APIRouter(prefix="/api/experimental/playback", include_in_schema=False)


class SearchSource(Protocol):
    async def search(self, artist: str, album: str, *, limit: int = 20) -> tuple[MusicRelease, ...]: ...


class MetadataSource(Protocol):
    async def resolve(self, release: MusicRelease) -> MetadataResolution: ...


class OwnedTorrentSource:
    """Opt-in original test album, with names checked by TrackMatcher."""

    def __init__(self, torrent_path: Path, *, max_bytes: int = 4 * 1024 * 1024):
        self.path = torrent_path.resolve(strict=True)
        if self.path.suffix.lower() != ".torrent" or self.path.stat().st_size > max_bytes:
            raise ValueError("Expected a small owned .torrent file")
        self.metadata = inspect_torrent(self.path)
        self.max_bytes = max_bytes

    async def search(self, artist: str, album: str, *, limit: int = 20) -> tuple[MusicRelease, ...]:
        if not artist.strip() or not album.strip():
            return ()
        # The local test album may be a compilation; file matching is decisive.
        audio_format = "FLAC" if any(f.extension == ".flac" for f in self.metadata.files) else "MP3"
        return (MusicRelease("owned_local", "owned:local", f"{artist} - {album} {audio_format} tracks",
                             (f"Audio/{audio_format}",), sum(f.size for f in self.metadata.files), 1,
                             0, "Owned local test"),)

    async def resolve(self, release: MusicRelease) -> MetadataResolution:
        if release.source != "owned_local" or release.source_id != "owned:local":
            return MetadataResolution("unsupported_source")
        if self.path.stat().st_size > self.max_bytes:
            return MetadataResolution("too_large")
        payload = self.path.read_bytes()
        if len(payload) > self.max_bytes:
            return MetadataResolution("too_large")
        return MetadataResolution("ok", self.metadata, payload)


class ApprovedProwlarrSource:
    """Search Prowlarr but resolve only explicitly approved source IDs."""

    def __init__(self, provider: ProwlarrDiscoveryProvider, approved_ids: frozenset[str]):
        if not approved_ids:
            raise ValueError("An approved release list is required")
        self.provider = provider
        self.approved_ids = approved_ids

    async def search(self, artist: str, album: str, *, limit: int = 20) -> tuple[MusicRelease, ...]:
        results = await self.provider.search(artist, album, limit=limit)
        return tuple(item for item in results if item.source_id in self.approved_ids)


@dataclass
class _Job:
    id: str
    status: str = "preparing"
    playback_id: str | None = None
    torrent_path: Path | None = None
    task: asyncio.Task | None = None


class AutoTorrentPlayback:
    """One preparation at a time; each candidate is matched and prebuffered."""

    def __init__(self, search: SearchSource, resolver: MetadataSource,
                 registry: TorrentPlaybackRegistry, *, peers: tuple[tuple[str, int], ...],
                 max_candidates: int = 5, first_bytes: int = 4096,
                 preparation_timeout_seconds: float = 60):
        if not peers and registry.manager.network_mode not in ("public_discovery", "safe_webseed"):
            raise ValueError("An explicit peer is required")
        registry.manager.validate_peers(peers)
        if (not 1 <= max_candidates <= 10 or not 1 <= first_bytes <= 65536
                or not 5 <= preparation_timeout_seconds <= 180):
            raise ValueError("Invalid preparation limits")
        self.search = search
        self.resolver = resolver
        self.registry = registry
        self.peers = peers
        self.max_candidates = max_candidates
        self.first_bytes = first_bytes
        self.preparation_timeout_seconds = preparation_timeout_seconds
        self._lock = asyncio.Lock()
        self._job: _Job | None = None
        self._cancelled_ids: set[str] = set()
        self._temporary = tempfile.TemporaryDirectory(prefix="musicstream-auto-play-")

    async def start(self, track: Track, request_id: str | None = None) -> dict[str, str]:
        reference = TrackReference.from_deezer(track)
        if not reference.album or not reference.album.strip():
            raise ValueError("Album metadata is required")
        async with self._lock:
            if request_id is not None and not re.fullmatch(r"[0-9a-fA-F-]{36}", request_id):
                raise ValueError("Invalid request ID")
            if request_id in self._cancelled_ids:
                self._cancelled_ids.discard(request_id)
                return {"job_id": request_id, "status": "unavailable"}
            if self._job is not None:
                await self._cancel(self._job)
            job = _Job(request_id or secrets.token_urlsafe(18))
            self._job = job
            job.task = asyncio.create_task(self._prepare(job, reference))
            return {"job_id": job.id, "status": job.status}

    async def _prepare(self, job: _Job, reference: TrackReference) -> None:
        had_error = False
        deadline = time.monotonic() + self.preparation_timeout_seconds
        try:
            releases = await asyncio.wait_for(
                self.search.search(reference.artist, reference.album, limit=20),
                timeout=max(0.01, deadline - time.monotonic()),
            )
            ordered = rank_releases(reference, releases, {}).ranked[:self.max_candidates]
            resolved_by_id: dict[str, MetadataResolution] = {}
            for candidate in ordered:
                if job is not self._job:
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    resolved = await asyncio.wait_for(
                        self.resolver.resolve(candidate.release), timeout=min(20, remaining))
                except TimeoutError:
                    had_error = True
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception:
                    had_error = True
                    continue
                if resolved.status == "ok" and resolved.metadata and resolved.torrent_bytes:
                    resolved_by_id[candidate.release.source_id] = resolved
                else:
                    had_error |= resolved.status not in ("unsupported_source",)

            selection = rank_releases(
                reference, tuple(item.release for item in ordered),
                {key: value.metadata for key, value in resolved_by_id.items()},
            )
            if selection.reason == "ambiguous_releases":
                job.status = "unavailable"
                return
            for candidate in selection.ranked:
                if job is not self._job or time.monotonic() >= deadline:
                    break
                resolved = resolved_by_id.get(candidate.release.source_id)
                if resolved is None or resolved.metadata is None or resolved.torrent_bytes is None:
                    continue
                verified = rank_releases(reference, (candidate.release,),
                                         {candidate.release.source_id: resolved.metadata})
                if verified.release is None or verified.file_index is None:
                    continue
                chosen = select_audio_file(resolved.metadata, verified.file_index)
                if chosen.extension not in (".mp3", ".flac") or chosen.size == 0:
                    continue
                try:
                    token = secrets.token_urlsafe(18)
                    path = Path(self._temporary.name) / f"{token}.torrent"
                    path.write_bytes(resolved.torrent_bytes)
                    job.torrent_path = path
                    if self.registry.manager.network_mode == "safe_webseed":
                        from .safe_webseed import SafeWebSeedFetcher, UnsafeWebSeedError

                        try:
                            if inspect_torrent(path) != resolved.metadata:
                                await self._discard_playback(job)
                                continue
                            approved = SafeWebSeedFetcher(
                                path,
                                approved_info_hashes=self.registry.manager.approved_info_hashes,
                                archive_item=self.registry.manager.archive_item,
                            )
                        except (ValueError, UnsafeWebSeedError):
                            await self._discard_playback(job)
                            continue
                        approved.close()
                    job.playback_id = token
                    self.registry.register(token, path, verified.file_index, peers=self.peers)
                    job.status = "buffering"
                    lease = await self.registry.acquire(token)
                    try:
                        await asyncio.wait_for(
                            self.registry.read(lease, 0, min(self.first_bytes, chosen.size)),
                            timeout=max(0.01, deadline - time.monotonic()),
                        )
                    finally:
                        await self.registry.release(lease)
                    job.status = "ready"
                    return
                except TimeoutError:
                    await self._discard_playback(job)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    had_error = True
                    await self._discard_playback(job)
            job.status = "error" if had_error else "unavailable"
        except asyncio.CancelledError:
            await self._discard_playback(job)
            raise
        except Exception:
            job.status = "error"
        finally:
            if job.status != "ready":
                await self._discard_playback(job)

    async def _discard_playback(self, job: _Job) -> None:
        if job.playback_id:
            await self.registry.unregister(job.playback_id)
            job.playback_id = None
        if job.torrent_path:
            job.torrent_path.unlink(missing_ok=True)
            job.torrent_path = None

    def status(self, job_id: str) -> dict[str, str]:
        job = self._job
        if job is None or job.id != job_id:
            raise KeyError(job_id)
        result = {"status": job.status}
        if job.status == "ready" and job.playback_id:
            result["stream_url"] = f"/api/experimental/torrent/{job.playback_id}"
        return result

    async def _cancel(self, job: _Job) -> None:
        if job.task is not None and not job.task.done():
            job.task.cancel()
            try:
                await job.task
            except asyncio.CancelledError:
                pass
        await self._discard_playback(job)

    async def cancel(self, job_id: str) -> None:
        async with self._lock:
            if self._job is not None and self._job.id == job_id:
                job, self._job = self._job, None
                await self._cancel(job)
            else:
                if len(self._cancelled_ids) >= 32:
                    self._cancelled_ids.pop()
                self._cancelled_ids.add(job_id)

    async def aclose(self) -> None:
        async with self._lock:
            if self._job is not None:
                await self._cancel(self._job)
                self._job = None
        self._temporary.cleanup()


class PrepareRequest(BaseModel):
    request_id: str | None = Field(default=None, max_length=36)
    id: str | None = Field(default=None, max_length=40)
    title: str = Field(min_length=1, max_length=200)
    artist: str = Field(min_length=1, max_length=200)
    album: str = Field(min_length=1, max_length=200)
    duration_seconds: float | None = Field(default=None, ge=0, le=86400)


def _service(request: Request) -> AutoTorrentPlayback:
    try:
        local = ipaddress.ip_address(request.client.host).is_loopback if request.client else False
    except ValueError:
        local = False
    origin = request.headers.get("origin")
    expected = str(request.base_url).rstrip("/")
    if (not local or request.headers.get("sec-fetch-site") == "cross-site"
            or (origin is not None and origin != expected)
            or not hasattr(request.app.state, "torrent_auto")):
        raise HTTPException(404, "Not found")
    return request.app.state.torrent_auto


@router.get("/capabilities")
async def capabilities(request: Request):
    _service(request)
    return {"available": True}


@router.post("/prepare")
async def prepare(request: Request, payload: PrepareRequest):
    service = _service(request)
    try:
        track = Track(source="deezer", id=payload.id, title=payload.title,
                      artist=payload.artist, album=payload.album,
                      duration_seconds=payload.duration_seconds)
        return await service.start(track, payload.request_id)
    except ValueError as exc:
        raise HTTPException(422, "Incomplete track metadata") from exc


@router.get("/prepare/{job_id}")
async def preparation_status(request: Request, job_id: str):
    try:
        return _service(request).status(job_id)
    except KeyError as exc:
        raise HTTPException(404, "Unknown preparation") from exc


@router.delete("/prepare/{job_id}", status_code=204)
async def cancel_preparation(request: Request, job_id: str):
    await _service(request).cancel(job_id)
