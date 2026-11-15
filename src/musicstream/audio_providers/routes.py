import asyncio
import logging
import os
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from musicstream.audio_providers.base import RecordingReference, SourceResult
from musicstream.audio_providers.hitmos import HitmosProvider
from musicstream.audio_providers.hitmos_browser import HitmosBrowserReader
from musicstream.audio_providers.transport import BoundedSourceClient
from musicstream.audio_providers.youtube import YouTubeProvider
from musicstream.audio_providers.service import SourceSelectionService
from musicstream.public_rate_limit import ClientRateLimiter
from musicstream.audio_providers.hitmos_stream import HitmosStreamProxy, diagnostic_host_value
from musicstream.audio_providers.transport import SourceFailure

router = APIRouter()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def audio_lifespan(app):
    explicitly_enabled = os.getenv("HITMOS_LAB_MODE", "").strip().lower() == "true"
    local_diagnostics = bool(getattr(app.state, "local_audio_diagnostics", False)) and (
        os.getenv("MUSICSTREAM_DIAGNOSTICS") == "true" or explicitly_enabled)
    lab_mode = explicitly_enabled and bool(getattr(app.state, "local_audio_diagnostics", False))
    try:
        lab_request_limit = int(os.getenv("HITMOS_LAB_RATE_LIMIT", "15"))
    except ValueError:
        lab_request_limit = 15
    lab_request_limit = max(5, min(30, lab_request_limit))
    hitmos_proxy = HitmosStreamProxy() if lab_mode else None
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=False, headers={"User-Agent": "MusicStream/0.1.0"}) as client:
        hitmos_reader = HitmosBrowserReader(diagnostics_enabled=local_diagnostics, lab_mode=lab_mode,
                                             request_limit=lab_request_limit if lab_mode else 5)
        app.state.audio_providers = {
            "youtube": YouTubeProvider(BoundedSourceClient(client), os.getenv("YOUTUBE_API_KEY", "").strip() or None),
            "hitmos": HitmosProvider(hitmos_reader,
                                     diagnostics_enabled=local_diagnostics, lab_mode=lab_mode,
                                     stream_proxy=hitmos_proxy),
        }
        app.state.hitmos_lab_mode = lab_mode
        app.state.hitmos_stream_proxy = hitmos_proxy
        app.state.audio_limiter = ClientRateLimiter(limit=lab_request_limit if lab_mode else 10)
        # Process-local only: never persist CDN URLs or source metadata to disk.
        app.state.audio_response_cache = OrderedDict()
        app.state.source_selection = SourceSelectionService(app.state.audio_providers)
        try:
            yield
        finally:
            await hitmos_reader.close()


@router.get("/api/audio/hitmos-stream/{token}")
async def hitmos_lab_stream(request: Request, token: str, range_header: str | None = Header(default=None, alias="Range")):
    if not getattr(request.app.state, "hitmos_lab_mode", False):
        raise HTTPException(404, "Not found")
    if not token.isascii() or not (32 <= len(token) <= 64) or not all(c.isalnum() or c in "-_" for c in token):
        raise HTTPException(404, "Not found")
    proxy = request.app.state.hitmos_stream_proxy
    try:
        opened = await proxy.open(token, range_header)
        return StreamingResponse(opened.body(), status_code=opened.status_code, headers=opened.headers)
    except SourceFailure as exc:
        status = (exc.http_status if exc.owner == "external_server" and exc.http_status in {403, 404, 410, 416, 429}
                  else 416 if exc.reason == "invalid_range" else 404 if exc.reason == "audio_token_expired" else 502)
        headers = {"Cache-Control": "no-store"}
        if exc.retry_after_seconds is not None:
            headers["Retry-After"] = str(exc.retry_after_seconds)
        logger.warning("Hitmos Lab stream rejected: stage=%s code=%s owner=%s status=%s retry_after=%s host=%s path_type=%s",
            exc.stage or "http_range", exc.reason, exc.owner or "unknown", exc.http_status,
            exc.retry_after_seconds, diagnostic_host_value(exc.host), exc.path_type or "unknown")
        raise HTTPException(status, detail={"reason": exc.reason, "stage": exc.stage or "http_range",
                            "rejected_by": exc.owner, "http_status": exc.http_status,
                            "retry_after_seconds": exc.retry_after_seconds,
                            "host": diagnostic_host_value(exc.host), "path_type": exc.path_type},
                            headers=headers) from exc


@router.get("/api/audio/resolve", response_model=SourceResult)
async def resolve_audio(
    request: Request,
    title: Annotated[str, Query(min_length=1, max_length=200)],
    artist: Annotated[str, Query(min_length=1, max_length=200)],
    duration_seconds: Annotated[float | None, Query(ge=0, le=86400)] = None,
    explicit_lyrics: bool | None = None,
    provider: Annotated[Literal["youtube", "hitmos", "auto"], Query()] = "youtube",
    hitmos_id: Annotated[str | None, Query(pattern=r"^[0-9]{1,12}$")] = None,
    candidate_id: Annotated[str | None, Query(pattern=r"^[a-zA-Z0-9_-]{1,12}$")] = None,
    allow_youtube: bool = False,
    deezer_id: Annotated[str | None, Query(pattern=r"^[0-9]{1,20}$")] = None,
):
    peer = request.client.host if request.client else "unknown"
    cache_key = (title.strip(), artist.strip(), duration_seconds, explicit_lyrics,
                 provider, hitmos_id or candidate_id, allow_youtube, deezer_id)
    cache = request.app.state.audio_response_cache
    if (set(request.query_params) - {"title", "artist", "duration_seconds", "explicit_lyrics", "provider", "hitmos_id", "candidate_id", "allow_youtube", "deezer_id"}
            or not title.strip() or not artist.strip() or (hitmos_id and provider != "hitmos")
            or (hitmos_id and candidate_id) or (candidate_id and provider == "auto")
            or (candidate_id and provider == "youtube" and len(candidate_id) != 11)
            or (candidate_id and provider == "hitmos" and not candidate_id.isascii())
            or (candidate_id and provider == "hitmos" and not candidate_id.isdigit())):
        raise HTTPException(422, "Invalid source lookup parameters")
    now = time.monotonic()
    for key in [key for key, (expires, _) in cache.items() if expires <= now]:
        del cache[key]
    cached = cache.get(cache_key)
    if cached is not None:
        cache.move_to_end(cache_key)
        if provider == "auto":
            recording = RecordingReference(title=title.strip(), artist=artist.strip(),
                                           duration_seconds=duration_seconds,
                                           explicit_lyrics=explicit_lyrics)
            return await request.app.state.source_selection.discover(recording,
                                                                      allow_youtube=allow_youtube)
        return cached[1].model_copy(deep=True)
    retry_after = request.app.state.audio_limiter.check(peer)
    if retry_after:
        raise HTTPException(429, "Source lookup limit reached", headers={"Retry-After": str(retry_after)})
    recording = RecordingReference(title=title.strip(), artist=artist.strip(), duration_seconds=duration_seconds, explicit_lyrics=explicit_lyrics)
    try:
        operation_timeout = 30 if getattr(request.app.state, "hitmos_lab_mode", False) and (hitmos_id or candidate_id) else 10
        async with asyncio.timeout(operation_timeout):
            if hitmos_id or candidate_id:
                result = await request.app.state.source_selection.prepare(recording, provider, hitmos_id or candidate_id)
            elif provider == "auto":
                result = await request.app.state.source_selection.discover(recording, allow_youtube=allow_youtube)
            else:
                result = await request.app.state.audio_providers[provider].find(recording)
            if result.status in {"available", "external_player"}:
                ttl = 45 if hitmos_id or candidate_id else 60
                cache[cache_key] = (time.monotonic() + ttl, result.model_copy(deep=True))
                cache.move_to_end(cache_key)
                while len(cache) > 128:
                    cache.popitem(last=False)
            return result
    except TimeoutError as exc:
        raise HTTPException(504, "Source lookup timed out") from exc
