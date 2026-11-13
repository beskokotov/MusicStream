"""Shared provider lifecycle and search execution for local and public apps."""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request

from musicstream.config import Settings
from musicstream.providers.base import ProviderError, SearchProvider, Track
from musicstream.providers.deezer import DeezerProvider
from musicstream.providers.musicbrainz import MusicBrainzProvider
from musicstream.audio_providers.routes import audio_lifespan


@asynccontextmanager
async def provider_lifespan(app: FastAPI, *, include_musicbrainz: bool = True):
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(audio_lifespan(app))
        deezer_client = await stack.enter_async_context(httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            headers={"Accept": "application/json", "User-Agent": "MusicStream/0.1.0"},
        ))
        app.state.search_providers = {"deezer": DeezerProvider(deezer_client)}
        if include_musicbrainz:
            try:
                settings = Settings.from_env()
            except ValueError:
                settings = Settings()
            app.state.search_providers["musicbrainz"] = None
            if settings.user_agent:
                client = await stack.enter_async_context(httpx.AsyncClient(
                    timeout=httpx.Timeout(10.0),
                    headers={"Accept": "application/json", "User-Agent": settings.user_agent},
                ))
                app.state.search_providers["musicbrainz"] = MusicBrainzProvider(client)
        yield


def get_search_provider(request: Request, name: str) -> SearchProvider:
    provider = request.app.state.search_providers[name]
    if provider is None:
        raise HTTPException(503, "Set a valid MUSICBRAINZ_CONTACT to enable MusicBrainz search")
    return provider


async def search_tracks(
    request: Request, query: str, limit: int, provider: str = "deezer",
    *, timeout_seconds: float | None = None,
) -> list[Track]:
    query = query.strip()
    if not query:
        raise HTTPException(422, "q must not be blank")
    search_provider = get_search_provider(request, provider)
    try:
        async with asyncio.timeout(timeout_seconds):
            return await search_provider.search(query, limit)
    except TimeoutError as exc:
        raise HTTPException(504, "Music search timed out; try again later") from exc
    except ProviderError as exc:
        headers = {"Retry-After": exc.retry_after} if exc.retry_after else None
        raise HTTPException(exc.status_code, str(exc), headers=headers) from exc
