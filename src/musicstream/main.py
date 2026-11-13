import os
import json
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import FastAPI, Query, Request
from fastapi.staticfiles import StaticFiles

from musicstream.providers.base import Track
from musicstream.search_service import provider_lifespan, search_tracks
from musicstream.web import STATIC_DIR, router as web_router
from musicstream.audio_providers.routes import router as audio_router


TORRENT_HTTP_ENABLED = os.getenv("MUSICSTREAM_TORRENT_HTTP_ENABLED") == "true"
TORRENT_AUTO_ENABLED = TORRENT_HTTP_ENABLED and os.getenv("MUSICSTREAM_TORRENT_AUTO_ENABLED") == "true"


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with provider_lifespan(app):
        if TORRENT_HTTP_ENABLED:
            from musicstream.torrent.http import TorrentPlaybackRegistry
            from musicstream.torrent.session import TorrentSessionManager

            registry = TorrentPlaybackRegistry(
                TorrentSessionManager(max_sessions=1), max_streams=1,
                read_timeout_seconds=float(os.getenv("MUSICSTREAM_TORRENT_AUTO_READ_TIMEOUT", "5")),
            ) if TORRENT_AUTO_ENABLED else TorrentPlaybackRegistry()
            auto = None
            prowlarr_client = None
            source = os.getenv("MUSICSTREAM_TORRENT_HTTP_SOURCE")
            port = os.getenv("MUSICSTREAM_TORRENT_HTTP_PEER_PORT")
            indexes = os.getenv("MUSICSTREAM_TORRENT_HTTP_FILE_INDEXES", "")
            if source and port and indexes:
                for index in (int(value.strip()) for value in indexes.split(",")):
                    registry.register(f"track-{index}", source, index, peers=(("127.0.0.1", int(port)),))
            registry.start()
            app.state.torrent_registry = registry
            try:
                if TORRENT_AUTO_ENABLED:
                    import httpx

                    from musicstream.torrent.auto import (
                        ApprovedProwlarrSource, AutoTorrentPlayback, OwnedTorrentSource,
                    )
                    from musicstream.torrent.metadata_resolver import TorrentMetadataResolver
                    from musicstream.torrent.prowlarr_discovery import ProwlarrDiscoveryProvider

                    peer_port = int(os.environ["MUSICSTREAM_TORRENT_HTTP_PEER_PORT"])
                    peers = (("127.0.0.1", peer_port),)
                    owned_source = os.getenv("MUSICSTREAM_TORRENT_AUTO_TEST_SOURCE")
                    if owned_source:
                        search = resolver = OwnedTorrentSource(Path(owned_source))
                    else:
                        approved = frozenset(json.loads(os.environ["MUSICSTREAM_TORRENT_APPROVED_SOURCE_IDS"]))
                        indexers = tuple(int(value) for value in
                                         os.environ["MUSICSTREAM_TORRENT_ALLOWED_INDEXER_IDS"].split(","))
                        prowlarr_client = httpx.AsyncClient(trust_env=False)
                        search = ApprovedProwlarrSource(
                            ProwlarrDiscoveryProvider(prowlarr_client, indexer_ids=indexers), approved)
                        resolver = TorrentMetadataResolver(
                            prowlarr_client, allowed_indexer_ids=indexers)
                    auto = AutoTorrentPlayback(search, resolver, registry, peers=peers)
                    app.state.torrent_auto = auto
                yield
            finally:
                if auto is not None:
                    await auto.aclose()
                    del app.state.torrent_auto
                if prowlarr_client is not None:
                    await prowlarr_client.aclose()
                await registry.aclose()
                del app.state.torrent_registry
        else:
            yield

app = FastAPI(
    lifespan=lifespan,
    title="MusicStream API",
    description="Сервер нашего музыкального приложения",
    version="0.1.0",
)
app.state.local_audio_diagnostics = True

app.include_router(web_router)
app.include_router(audio_router)
if TORRENT_HTTP_ENABLED:
    from musicstream.torrent.http import router as torrent_router
    app.include_router(torrent_router)
if TORRENT_AUTO_ENABLED:
    from musicstream.torrent.auto import router as torrent_auto_router
    app.include_router(torrent_auto_router)
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
