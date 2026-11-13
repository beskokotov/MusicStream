from typing import Annotated, Literal

from fastapi import FastAPI, Query, Request
from fastapi.staticfiles import StaticFiles

from musicstream.providers.base import Track
from musicstream.search_service import provider_lifespan as lifespan, search_tracks
from musicstream.web import STATIC_DIR, router as web_router
from musicstream.audio_providers.routes import router as audio_router

app = FastAPI(
    lifespan=lifespan,
    title="MusicStream API",
    description="Сервер нашего музыкального приложения",
    version="0.1.0",
)
app.state.local_audio_diagnostics = True

app.include_router(web_router)
app.include_router(audio_router)
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
    return await search_tracks(request, q, limit, provider)
