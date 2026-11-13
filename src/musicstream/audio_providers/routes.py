import asyncio
import os
from contextlib import asynccontextmanager
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

from musicstream.audio_providers.base import RecordingReference, SourceResult
from musicstream.audio_providers.hitmos import HitmosProvider
from musicstream.audio_providers.hitmos_browser import HitmosBrowserReader
from musicstream.audio_providers.transport import BoundedSourceClient
from musicstream.audio_providers.youtube import YouTubeProvider
from musicstream.audio_providers.service import SourceSelectionService
from musicstream.public_rate_limit import ClientRateLimiter

router = APIRouter()


@asynccontextmanager
async def audio_lifespan(app):
    local_diagnostics = bool(getattr(app.state, "local_audio_diagnostics", False)) and os.getenv("MUSICSTREAM_DIAGNOSTICS") == "true"
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=False, headers={"User-Agent": "MusicStream/0.1.0"}) as client:
        app.state.audio_providers = {
            "youtube": YouTubeProvider(BoundedSourceClient(client), os.getenv("YOUTUBE_API_KEY", "").strip() or None),
            "hitmos": HitmosProvider(HitmosBrowserReader() if os.getenv("HITMOS_BROWSER_ENABLED") == "true" else None,
                                     diagnostics_enabled=local_diagnostics),
        }
        app.state.audio_limiter = ClientRateLimiter(limit=10)
        app.state.source_selection = SourceSelectionService(app.state.audio_providers)
        yield


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
):
    peer = request.client.host if request.client else "unknown"
    retry_after = request.app.state.audio_limiter.check(peer)
    if retry_after:
        raise HTTPException(429, "Source lookup limit reached", headers={"Retry-After": str(retry_after)})
    if (set(request.query_params) - {"title", "artist", "duration_seconds", "explicit_lyrics", "provider", "hitmos_id", "candidate_id", "allow_youtube"}
            or not title.strip() or not artist.strip() or (hitmos_id and provider != "hitmos")
            or (hitmos_id and candidate_id) or (candidate_id and provider == "auto")
            or (candidate_id and provider == "youtube" and len(candidate_id) != 11)
            or (candidate_id and provider == "hitmos" and not candidate_id.isascii())
            or (candidate_id and provider == "hitmos" and not candidate_id.isdigit())):
        raise HTTPException(422, "Invalid source lookup parameters")
    recording = RecordingReference(title=title.strip(), artist=artist.strip(), duration_seconds=duration_seconds, explicit_lyrics=explicit_lyrics)
    try:
        async with asyncio.timeout(10):
            if hitmos_id or candidate_id:
                return await request.app.state.source_selection.prepare(recording, provider, hitmos_id or candidate_id)
            if provider == "auto":
                return await request.app.state.source_selection.discover(recording, allow_youtube=allow_youtube)
            return await request.app.state.audio_providers[provider].find(recording)
    except TimeoutError as exc:
        raise HTTPException(504, "Source lookup timed out") from exc
