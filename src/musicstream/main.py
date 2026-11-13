
from contextlib import asynccontextmanager
from typing import Annotated, Literal

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles

from musicstream.config import Settings
from musicstream.providers.base import ProviderError, SearchProvider, Track
from musicstream.providers.deezer import DeezerProvider
from musicstream.providers.musicbrainz import MusicBrainzProvider
from musicstream.web import STATIC_DIR, router as web_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        settings = Settings.from_env()
    except ValueError:
        # A bad optional MusicBrainz contact must not disable Deezer.
        settings = Settings()
    headers = {"Accept": "application/json"}
    if settings.user_agent:
        headers["User-Agent"] = settings.user_agent
    async with (
        httpx.AsyncClient(timeout=httpx.Timeout(10.0), headers=headers) as musicbrainz_client,
        httpx.AsyncClient(timeout=httpx.Timeout(10.0), headers={
            "Accept": "application/json", "User-Agent": "MusicStream/0.1.0",
        }) as deezer_client,
    ):
        app.state.search_providers = {
            "deezer": DeezerProvider(deezer_client),
            "musicbrainz": MusicBrainzProvider(musicbrainz_client) if settings.user_agent else None,
        }
        yield


def get_search_provider(request: Request, name: str) -> SearchProvider:
    provider = request.app.state.search_providers[name]
    if provider is None:
        raise HTTPException(503, "Set a valid MUSICBRAINZ_CONTACT to enable MusicBrainz search")
    return provider


app = FastAPI(
    lifespan=lifespan,
    title="MusicStream API",
    description="Сервер нашего музыкального приложения",
    version="0.1.0",
)

app.include_router(web_router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def home():
    return {
        "project": "MusicStream",
        "status": "running",
        "message": "Музыкальный сервер работает!"
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/search", response_model=list[Track])
async def search(
    q: Annotated[str, Query(min_length=1)],
    request: Request,
    limit: Annotated[int, Query(ge=1, le=25)] = 10,
    provider: Annotated[Literal["deezer", "musicbrainz"], Query()] = "deezer",
):
    query = q.strip()
    if not query:
        raise HTTPException(422, "q must not be blank")
    search_provider = get_search_provider(request, provider)
    try:
        return await search_provider.search(query, limit)
    except ProviderError as exc:
        headers = {"Retry-After": exc.retry_after} if exc.retry_after else None
        raise HTTPException(exc.status_code, str(exc), headers=headers) from exc
