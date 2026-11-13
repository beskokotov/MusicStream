"""Restricted public entry point. Run only on 127.0.0.1:8001."""

from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from musicstream.providers.base import Track
from musicstream.public_rate_limit import ClientRateLimiter, PublicRateLimitMiddleware
from musicstream.search_service import provider_lifespan, search_tracks
from musicstream.audio_providers.routes import router as audio_router

SEARCH_TIMEOUT_SECONDS = 12.0
PAGES_ORIGIN = "https://beskokotov.github.io"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.rate_limiter = ClientRateLimiter()
    async with provider_lifespan(app, include_musicbrainz=False):
        yield


app = FastAPI(
    title="MusicStream Public API", lifespan=lifespan,
    docs_url=None, redoc_url=None, openapi_url=None,
    redirect_slashes=False, debug=False,
)
app.add_middleware(PublicRateLimitMiddleware)
app.include_router(audio_router)
# CORS wraps rate-limit responses as well, so Pages can read HTTP 429.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[PAGES_ORIGIN],
    allow_methods=["GET"],
    allow_headers=["Accept"],
    allow_credentials=False,
    expose_headers=["Retry-After"],
)


@app.exception_handler(RequestValidationError)
async def invalid_search_parameters(request: Request, exc: RequestValidationError):
    # Do not echo input or framework validation diagnostics to public clients.
    return JSONResponse(status_code=422, content={
        "detail": "Invalid API parameters: check query lengths, limits and selected provider",
    })


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/search", response_model=list[Track])
async def search(
    request: Request,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    limit: Annotated[int, Query(ge=1, le=25)] = 10,
    provider: Annotated[Literal["deezer"], Query()] = "deezer",
):
    return await search_tracks(request, q, limit, provider, timeout_seconds=SEARCH_TIMEOUT_SECONDS)
