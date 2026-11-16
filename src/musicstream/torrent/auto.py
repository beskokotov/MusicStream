"""Local-only automatic Play preparation; no source identifiers reach the browser."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
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
from .cache import TTLCache
from .metadata import inspect_torrent, select_audio_file
from .metadata_resolver import MetadataResolution
from .prowlarr_discovery import MusicRelease, ProwlarrDiscoveryProvider, release_fingerprint
from .release_selection import rank_releases
from .track_matcher import TrackReference
from .timing import PlaybackTiming, current_timing


router = APIRouter(prefix="/api/experimental/playback", include_in_schema=False)
logger = logging.getLogger("uvicorn.error")


def _log_label(value: str) -> str:
    """Keep untrusted release names on one bounded server log line."""
    value = re.sub(r"(?:https?://|magnet:\?)\S+", "[link]", value, flags=re.I)
    return "".join(char if char.isprintable() else " " for char in value)[:160]


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
    """Search one indexer, optionally allowing all its public music releases."""

    def __init__(self, provider: ProwlarrDiscoveryProvider, approved_ids: frozenset[str],
                 *, allow_unlisted: bool = False,
                 indexer_priority: tuple[tuple[int, str], ...] = ()):
        if not approved_ids and not allow_unlisted:
            raise ValueError("An approved release list is required")
        self.provider = provider
        self.approved_ids = approved_ids
        self.allow_unlisted = allow_unlisted
        self.indexer_priority = indexer_priority
        self.last_search_total = 0
        self._search_slots = asyncio.Semaphore(2)
        self._search_cache: TTLCache[tuple[MusicRelease, ...]] = TTLCache(
            ttl_seconds=90, max_entries=64, max_bytes=2 * 1024 * 1024)

    async def _search_term(self, artist: str, term: str, limit: int,
                           indexer_id: int | None) -> tuple[MusicRelease, ...]:
        key = (artist.casefold(), term.casefold(), limit, indexer_id)
        cached = self._search_cache.get(key)
        if cached is not None:
            timing = current_timing.get()
            if timing:
                timing.mark("search_cache_hit", indexer_id=indexer_id)
            return cached
        extra = {"indexer_ids": (indexer_id,)} if indexer_id is not None else {}
        async with self._search_slots:
            results = await self.provider.search(artist, term, limit=limit, **extra)
        # The cache is memory-only. Releases containing credential-like URLs
        # are not retained after this request.
        if not any(re.search(r"(?i)passkey|api[_-]?key|auth(?:orization)?|token",
                          release.source_id + " " + (release.metadata_url or ""))
                   for release in results):
            size = sum(len(release.title) + len(release.source_id) +
                       len(release.metadata_url or "") for release in results)
            self._search_cache.put(key, results, size=max(1, size),
                                   ttl_seconds=5 if not results else None)
        return results

    async def search(self, artist: str, album: str, *, limit: int = 20) -> tuple[MusicRelease, ...]:
        results = await self._search_term(artist, album, limit, None)
        self.last_search_total = len(results)
        return self._filter(results)

    def _filter(self, results: tuple[MusicRelease, ...]) -> tuple[MusicRelease, ...]:
        return tuple(item for item in results if self.allow_unlisted or
                     item.source_id in self.approved_ids or
                     release_fingerprint(item) in self.approved_ids)

    async def search_track(self, reference: TrackReference, *, limit: int = 50,
                           indexer_id: int | None = None) -> tuple[MusicRelease, ...]:
        """Search albums and singles, keeping one release per indexer identifier."""
        queries = [reference.album, reference.title]
        terms = tuple(dict.fromkeys(term.strip() for term in queries if term and term.strip()))
        batches = await asyncio.gather(*(self._search_term(reference.artist, term, limit, indexer_id)
                                         for term in terms), return_exceptions=True)
        if all(isinstance(batch, BaseException) for batch in batches):
            raise batches[0]
        unique = tuple({release.source_id: release for batch in batches
                        if isinstance(batch, tuple)
                        for release in batch}.values())
        self.last_search_total = len(unique)
        return self._filter(unique)


@dataclass
class _Job:
    id: str
    timing: PlaybackTiming | None = None
    status: str = "preparing"
    playback_id: str | None = None
    torrent_path: Path | None = None
    selected_release: MusicRelease | None = None
    task: asyncio.Task | None = None


@dataclass(frozen=True)
class _ReadyCandidate:
    playback_id: str
    torrent_path: Path
    release: MusicRelease


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
        received_at = time.monotonic()
        reference = TrackReference.from_deezer(track)
        async with self._lock:
            if request_id is not None and not re.fullmatch(r"[0-9a-fA-F-]{36}", request_id):
                raise ValueError("Invalid request ID")
            if request_id in self._cancelled_ids:
                self._cancelled_ids.discard(request_id)
                return {"job_id": request_id, "status": "unavailable"}
            if self._job is not None:
                await self._cancel(self._job)
            job = _Job(request_id or secrets.token_urlsafe(18))
            job.timing = PlaybackTiming(job.id, received_at)
            self._job = job
            context_token = current_timing.set(job.timing)
            try:
                job.task = asyncio.create_task(self._prepare(job, reference))
            finally:
                current_timing.reset(context_token)
            return {"job_id": job.id, "status": job.status}

    async def _prepare(self, job: _Job, reference: TrackReference) -> None:
        job.timing.mark("prepare_started")
        priority = (self.search.indexer_priority
                    if isinstance(self.search, ApprovedProwlarrSource) else ())
        if (len(priority) >= 2 and self.registry.max_streams >= 2 and
                self.registry.manager.max_sessions >= 2):
            await self._prepare_fast(job, reference, priority)
            return
        if not priority:
            await self._prepare_one(job, reference,
                                    time.monotonic() + self.preparation_timeout_seconds)
            return
        overall_deadline = time.monotonic() + self.preparation_timeout_seconds
        any_error = False
        for indexer in priority:
            if job is not self._job or time.monotonic() >= overall_deadline:
                break
            group_deadline = min(overall_deadline, time.monotonic() + 60)
            job.timing.mark("indexer_search_started", indexer_id=indexer[0])
            logger.info("Auto Play job=%s indexer=%s indexer_id=%d phase=search", job.id,
                        _log_label(indexer[1]), indexer[0])
            await self._prepare_one(job, reference, group_deadline, indexer=indexer)
            if job.status == "ready":
                return
            any_error |= job.status == "error"
            logger.info("Auto Play job=%s indexer=%s indexer_id=%d phase=fallback status=%s",
                        job.id, _log_label(indexer[1]), indexer[0], job.status)
            job.status = "preparing"
        job.status = "error" if any_error else "unavailable"

    async def _prepare_fast(self, job: _Job, reference: TrackReference,
                            priority: tuple[tuple[int, str], ...]) -> None:
        """Race at most two indexers and retain the first verified stream."""
        deadline = time.monotonic() + self.preparation_timeout_seconds
        selected = priority[:2]
        tasks = {indexer[0]: asyncio.create_task(
            self._prepare_fast_indexer(job, reference, indexer, deadline,
                                       2 if position == 0 else self.max_candidates))
                 for position, indexer in enumerate(selected)}
        pending = set(tasks.values())
        ready: dict[int, _ReadyCandidate] = {}
        winner: _ReadyCandidate | None = None
        errors = 0
        try:
            while pending and time.monotonic() < deadline:
                done, pending = await asyncio.wait(
                    pending, timeout=max(0.01, deadline - time.monotonic()),
                    return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    break
                for task in done:
                    indexer_id = next(key for key, value in tasks.items() if value is task)
                    try:
                        result = task.result()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        errors += 1
                        logger.info("Auto Play job=%s indexer_id=%d phase=fast_prepare_error exception=%s",
                                    job.id, indexer_id, type(exc).__name__)
                        continue
                    if result is not None:
                        ready[indexer_id] = result
                primary_id = selected[0][0]
                if primary_id in ready:
                    winner = ready[primary_id]
                    break
                if ready:
                    if tasks[primary_id] in pending:
                        extra, pending = await asyncio.wait(
                            pending, timeout=min(0.75, max(0.01, deadline - time.monotonic())),
                            return_when=asyncio.FIRST_COMPLETED)
                        for task in extra:
                            indexer_id = next(key for key, value in tasks.items() if value is task)
                            try:
                                result = task.result()
                            except Exception:
                                result = None
                                errors += 1
                            if result is not None:
                                ready[indexer_id] = result
                    winner = ready.get(primary_id) or next(iter(ready.values()))
                    break
            if winner is not None:
                job.playback_id = winner.playback_id
                job.torrent_path = winner.torrent_path
                job.selected_release = winner.release
                job.timing.mark("first_fragment_ready", indexer_id=winner.release.indexer_id)
                logger.info("Auto Play job=%s selected_indexer=%s indexer_id=%d selected_release=%s",
                            job.id, _log_label(winner.release.indexer), winner.release.indexer_id,
                            _log_label(winner.release.title))
            else:
                job.status = "error" if errors == len(selected) else "unavailable"
        finally:
            for task in pending:
                task.cancel()
            completed = await asyncio.gather(*tasks.values(), return_exceptions=True)
            for result in completed:
                if isinstance(result, _ReadyCandidate) and result is not winner:
                    await self.registry.unregister(result.playback_id)
                    result.torrent_path.unlink(missing_ok=True)
            if winner is not None:
                job.status = "ready"

    async def _prepare_fast_indexer(self, job: _Job, reference: TrackReference,
                                     indexer: tuple[int, str], deadline: float,
                                     candidate_limit: int) -> _ReadyCandidate | None:
        logger.info("Auto Play job=%s indexer=%s indexer_id=%d phase=search", job.id,
                    _log_label(indexer[1]), indexer[0])
        job.timing.mark("indexer_search_started", indexer_id=indexer[0])
        search_started = time.monotonic()
        releases = await self.search.search_track(reference, indexer_id=indexer[0])
        job.timing.mark("indexer_search_finished", operation_started=search_started,
                        indexer_id=indexer[0], status=str(len(releases)))
        logger.info("Auto Play job=%s indexer_id=%d search_results=%d", job.id,
                    indexer[0], len(releases))
        ordered = rank_releases(reference, releases, {}).ranked[:candidate_limit]
        for candidate in ordered:
            if time.monotonic() >= deadline or job is not self._job:
                break
            try:
                resolution = await asyncio.wait_for(
                    self.resolver.resolve(candidate.release),
                    timeout=min(20, max(0.01, deadline - time.monotonic())))
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.info("Auto Play job=%s indexer_id=%d phase=metadata_failed exception=%s",
                            job.id, indexer[0], type(exc).__name__)
                continue
            if resolution.status != "ok" or not resolution.metadata or not resolution.torrent_bytes:
                logger.info("Auto Play job=%s indexer_id=%d metadata_status=%s release=%s",
                            job.id, indexer[0], resolution.status,
                            _log_label(candidate.release.title))
                continue
            selection_started = time.monotonic()
            verified = rank_releases(
                reference, (candidate.release,),
                {candidate.release.source_id: resolution.metadata})
            job.timing.mark("file_selection_finished", operation_started=selection_started,
                            indexer_id=indexer[0], status=verified.reason)
            if verified.release is None or verified.file_index is None:
                continue
            chosen = select_audio_file(resolution.metadata, verified.file_index)
            if chosen.extension not in (".mp3", ".flac") or chosen.size == 0:
                continue
            job.timing.mark("audio_file_chosen", indexer_id=indexer[0], status=chosen.extension)
            logger.info("Auto Play job=%s indexer=%s indexer_id=%d candidate=%s file=%s index=%d seeders=%s score=%.3f confidence=%.3f",
                        job.id, _log_label(candidate.release.indexer), indexer[0],
                        _log_label(candidate.release.title), _log_label(chosen.name), chosen.index,
                        candidate.release.seeders, candidate.score,
                        verified.ranked[0].track_match.confidence)
            result = await self._try_fast_candidate(job, candidate.release, resolution,
                                                    verified.file_index, chosen.size, deadline)
            if result is not None:
                return result
        return None

    async def _try_fast_candidate(self, job: _Job, release: MusicRelease,
                                   resolved: MetadataResolution, file_index: int,
                                   file_size: int, deadline: float) -> _ReadyCandidate | None:
        token = secrets.token_urlsafe(18)
        path = Path(self._temporary.name) / f"{token}.torrent"
        registered = False
        success = False
        try:
            path.write_bytes(resolved.torrent_bytes)
            self.registry.register(token, path, file_index, peers=self.peers)
            registered = True
            job.status = "buffering"
            lease = await self.registry.acquire(
                token, wait_seconds=min(8, max(0.01, deadline - time.monotonic())))
            try:
                await self._adaptive_first_read(token, lease, min(self.first_bytes, file_size), deadline)
            finally:
                await self.registry.release(lease)
            success = True
            return _ReadyCandidate(token, path, release)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log_transfer(job, token, "candidate_failed")
            logger.info("Auto Play job=%s indexer_id=%d stage=fast_candidate exception=%s",
                        job.id, release.indexer_id, type(exc).__name__)
            return None
        finally:
            if not success:
                if registered:
                    await self.registry.unregister(token)
                path.unlink(missing_ok=True)

    async def _adaptive_first_read(self, token: str, lease, size: int,
                                    deadline: float) -> None:
        started = time.monotonic()
        last_progress = started
        payload = 0
        read_task = asyncio.create_task(self.registry.read(
            lease, 0, size, timeout_seconds=max(1, deadline - started)))
        try:
            while not read_task.done():
                await asyncio.wait({read_task}, timeout=0.5)
                if read_task.done():
                    break
                now = time.monotonic()
                details = self.registry.diagnostics(token)
                if details is None:
                    continue
                if details.downloaded_payload_bytes > payload:
                    payload = details.downloaded_payload_bytes
                    last_progress = now
                elapsed = now - started
                if now >= deadline:
                    raise TimeoutError("Preparation deadline reached")
                if details.discovered_peers == 0 and details.connected_peers == 0 and elapsed >= 18:
                    raise TimeoutError("No peers discovered")
                if details.handshakes_complete == 0 and elapsed >= 25:
                    raise TimeoutError("No completed handshake")
                if payload == 0 and elapsed >= 35:
                    raise TimeoutError("No audio payload")
                if payload > 0 and now - last_progress >= (
                        30 if details.verified_piece_bytes else 20):
                    raise TimeoutError("Audio payload stalled")
            await read_task
        finally:
            if not read_task.done():
                read_task.cancel()
            await asyncio.gather(read_task, return_exceptions=True)

    async def _prepare_one(self, job: _Job, reference: TrackReference, deadline: float,
                           *, indexer: tuple[int, str] | None = None) -> None:
        had_error = False
        try:
            search_started = time.monotonic()
            search = (self.search.search_track(reference, indexer_id=indexer[0] if indexer else None)
                      if isinstance(self.search, ApprovedProwlarrSource)
                      else self.search.search(reference.artist, reference.album or reference.title, limit=20))
            releases = await asyncio.wait_for(
                search,
                timeout=max(0.01, deadline - time.monotonic()),
            )
            job.timing.mark("indexer_search_finished", operation_started=search_started,
                            indexer_id=indexer[0] if indexer else None,
                            status=str(len(releases)))
            logger.info("Auto Play job=%s search_results=%d approved_results=%d", job.id,
                        getattr(self.search, "last_search_total", len(releases)), len(releases))
            candidate_limit = (min(self.max_candidates, 2) if indexer and
                               isinstance(self.search, ApprovedProwlarrSource) and
                               self.search.indexer_priority and
                               indexer == self.search.indexer_priority[0] and
                               len(self.search.indexer_priority) > 1 else self.max_candidates)
            ordered = rank_releases(reference, releases, {}).ranked[:candidate_limit]
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
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception:
                    had_error = True
                    continue
                if resolved.status == "ok" and resolved.metadata and resolved.torrent_bytes:
                    resolved_by_id[candidate.release.source_id] = resolved
                else:
                    had_error |= resolved.status not in ("unsupported_source", "unavailable")
                    logger.info("Auto Play job=%s indexer=%s indexer_id=%d metadata_status=%s release=%s",
                                job.id, _log_label(candidate.release.indexer),
                                candidate.release.indexer_id, resolved.status,
                                _log_label(candidate.release.title))

            selection_started = time.monotonic()
            selection = rank_releases(
                reference, tuple(item.release for item in ordered),
                {key: value.metadata for key, value in resolved_by_id.items()},
            )
            job.timing.mark("file_selection_finished", operation_started=selection_started,
                            indexer_id=indexer[0] if indexer else None,
                            status=selection.reason)
            logger.info("Auto Play job=%s selection=%s candidates=%d resolved=%d", job.id,
                        selection.reason, len(selection.ranked), len(resolved_by_id))
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
                job.timing.mark("audio_file_chosen", indexer_id=candidate.release.indexer_id,
                                status=chosen.extension)
                logger.info("Auto Play job=%s indexer=%s indexer_id=%d candidate=%s file=%s index=%d reported_seeders=%s score=%.3f confidence=%.3f",
                            job.id, _log_label(candidate.release.indexer), candidate.release.indexer_id,
                            _log_label(candidate.release.title), _log_label(chosen.name),
                            chosen.index, candidate.release.seeders, candidate.score,
                            verified.ranked[0].track_match.confidence)
                stage = "write_metadata"
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
                    stage = "register"
                    self.registry.register(token, path, verified.file_index, peers=self.peers)
                    job.status = "buffering"
                    stage = "acquire"
                    lease = await self.registry.acquire(
                        token, wait_seconds=min(8, max(0.01, deadline - time.monotonic())))
                    try:
                        stage = "prebuffer"
                        await asyncio.wait_for(
                            self.registry.read(lease, 0, min(self.first_bytes, chosen.size)),
                            timeout=max(0.01, deadline - time.monotonic()),
                        )
                        self._log_transfer(job, token, "first_fragment_ready")
                        job.timing.mark("first_fragment_ready",
                                        indexer_id=candidate.release.indexer_id)
                    finally:
                        await self.registry.release(lease)
                    job.status = "ready"
                    job.selected_release = candidate.release
                    logger.info("Auto Play job=%s selected_indexer=%s indexer_id=%d selected_release=%s",
                                job.id, _log_label(candidate.release.indexer), candidate.release.indexer_id,
                                _log_label(candidate.release.title))
                    return
                except TimeoutError:
                    self._log_transfer(job, token, "timeout")
                    await self._discard_playback(job)
                except asyncio.CancelledError:
                    raise
                except ValueError as exc:
                    if not ("not approved" in str(exc) or "Private torrents" in str(exc)):
                        had_error = True
                    logger.info("Auto Play job=%s stage=%s exception=%s", job.id, stage,
                                type(exc).__name__)
                    self._log_transfer(job, token, "rejected")
                    await self._discard_playback(job)
                except HTTPException as exc:
                    logger.info("Auto Play job=%s stage=%s http_status=%d", job.id, stage,
                                exc.status_code)
                    had_error |= exc.status_code != 429
                    self._log_transfer(job, token, "unavailable")
                    await self._discard_playback(job)
                except Exception as exc:
                    had_error = True
                    logger.info("Auto Play job=%s stage=%s exception=%s", job.id, stage,
                                type(exc).__name__)
                    self._log_transfer(job, token, "failed")
                    await self._discard_playback(job)
            job.status = "error" if had_error else "unavailable"
        except asyncio.CancelledError:
            await self._discard_playback(job)
            raise
        except Exception as exc:
            logger.info("Auto Play job=%s indexer=%s phase=prepare_exception exception=%s", job.id,
                        _log_label(indexer[1]) if indexer else "default", type(exc).__name__)
            job.status = "error"
        finally:
            if job.status != "ready":
                await self._discard_playback(job)

    def _log_transfer(self, job: _Job, playback_id: str, phase: str) -> None:
        try:
            details = self.registry.diagnostics(playback_id)
        except (KeyError, RuntimeError):
            details = None
        if details is None:
            logger.info("Auto Play job=%s phase=%s transfer=not_started", job.id, phase)
            return
        if self.registry.manager.network_mode == "safe_webseed":
            logger.info("Auto Play job=%s phase=%s webseed_connections=%d received_bytes=%d "
                        "verified_pieces=%d first_fragment_seconds=%s http_errors=%d",
                        job.id, phase, details.connections, details.received_bytes,
                        details.verified_pieces, details.first_piece_seconds, details.http_errors)
            return
        logger.info(
            "Auto Play job=%s phase=%s discovered=%d connected=%d seeds=%d "
            "handshakes=%d interested=%d unchoked=%d sent_blocks=%d received_blocks=%d "
            "payload_bytes=%d verified_bytes=%d payload_rate=%d first_fragment_seconds=%s reason=%s",
            job.id, phase, details.discovered_peers, details.connected_peers,
            details.connected_seeds, details.handshakes_complete, details.interested_peers,
            details.unchoked_piece_peers, details.sent_block_requests, details.finished_blocks,
            details.downloaded_payload_bytes, details.verified_piece_bytes,
            details.download_payload_rate_bytes_per_second, details.first_audio_fragment_seconds,
            details.no_transfer_reason,
        )

    async def _discard_playback(self, job: _Job) -> None:
        job.selected_release = None
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
    album: str | None = Field(default=None, max_length=200)
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
