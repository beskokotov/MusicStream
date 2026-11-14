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

            network_mode = os.getenv("MUSICSTREAM_TORRENT_NETWORK_MODE", "loopback")
            fast_priority = bool(network_mode == "public_discovery" and
                                 os.getenv("MUSICSTREAM_TORRENT_INDEXER_PRIORITY") and TORRENT_AUTO_ENABLED)
            if network_mode == "remote_tcp":
                raw_peers = json.loads(os.environ["MUSICSTREAM_TORRENT_REMOTE_PEERS"])
                if not isinstance(raw_peers, list) or not raw_peers:
                    raise ValueError("Remote mode requires explicit peers")
                peers = tuple(tuple(item) for item in raw_peers)
            elif network_mode in ("public_discovery", "safe_webseed"):
                peers = ()
            elif network_mode == "loopback":
                peer_port = os.getenv("MUSICSTREAM_TORRENT_HTTP_PEER_PORT")
                peers = (("127.0.0.1", int(peer_port)),) if peer_port else ()
            else:
                raise ValueError("Invalid BitTorrent network mode")
            connect_timeout = int(os.getenv(
                "MUSICSTREAM_TORRENT_PEER_CONNECT_TIMEOUT",
                "12" if network_mode == "public_discovery" else "8",
            ))
            public_hashes = frozenset()
            allow_unlisted_public = (network_mode == "public_discovery" and
                                     os.getenv("MUSICSTREAM_TORRENT_ALLOW_UNLISTED_PUBLIC") == "true")
            if network_mode in ("public_discovery", "safe_webseed"):
                raw_hashes = json.loads(os.getenv("MUSICSTREAM_TORRENT_PUBLIC_APPROVED_INFO_HASHES", "[]"))
                if not isinstance(raw_hashes, list) or any(not isinstance(value, str) for value in raw_hashes):
                    raise ValueError("Approved public info hashes must be a JSON array of strings")
                public_hashes = frozenset(value.lower() for value in raw_hashes)
            if network_mode == "safe_webseed":
                from musicstream.torrent.safe_webseed_stream import SafeWebSeedManager

                manager = SafeWebSeedManager(approved_info_hashes=public_hashes)
            else:
                manager = TorrentSessionManager(
                    max_sessions=2 if fast_priority else 1,
                    max_connections=(12 if network_mode == "public_discovery"
                                                     else 4 if network_mode == "remote_tcp" else 8),
                    network_mode=network_mode, peer_connect_timeout_seconds=connect_timeout,
                    approved_public_info_hashes=public_hashes,
                    allow_unlisted_public=allow_unlisted_public,
                    public_bind_ip=os.getenv("MUSICSTREAM_TORRENT_PUBLIC_BIND_IP")
                    if network_mode == "public_discovery" else None,
                )
            if TORRENT_AUTO_ENABLED and not peers and network_mode not in ("public_discovery", "safe_webseed"):
                raise ValueError("Automatic Play requires an explicit peer")
            if peers:
                manager.validate_peers(peers)
            default_read_timeout = "45" if network_mode == "safe_webseed" else \
                "30" if network_mode == "public_discovery" else \
                "20" if network_mode == "remote_tcp" else "5"
            read_timeout = float(os.getenv("MUSICSTREAM_TORRENT_AUTO_READ_TIMEOUT", default_read_timeout))
            if not 1 <= read_timeout <= 60:
                raise ValueError("Range read timeout must be 1-60 seconds")
            registry = TorrentPlaybackRegistry(
                manager, max_streams=2 if fast_priority else 1 if TORRENT_AUTO_ENABLED else 2,
                read_timeout_seconds=read_timeout,
            )
            auto = None
            prowlarr_client = None
            source = os.getenv("MUSICSTREAM_TORRENT_HTTP_SOURCE")
            indexes = os.getenv("MUSICSTREAM_TORRENT_HTTP_FILE_INDEXES", "")
            if source and indexes and (peers or network_mode in ("public_discovery", "safe_webseed")):
                for index in (int(value.strip()) for value in indexes.split(",")):
                    registry.register(f"track-{index}", source, index, peers=peers)
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

                    owned_source = os.getenv("MUSICSTREAM_TORRENT_AUTO_TEST_SOURCE")
                    if owned_source:
                        search = resolver = OwnedTorrentSource(Path(owned_source))
                    else:
                        approved = frozenset(json.loads(os.getenv("MUSICSTREAM_TORRENT_APPROVED_SOURCE_IDS", "[]")))
                        prowlarr_client = httpx.AsyncClient(trust_env=False)
                        provider = ProwlarrDiscoveryProvider(prowlarr_client)
                        priority_raw = os.getenv("MUSICSTREAM_TORRENT_INDEXER_PRIORITY")
                        if priority_raw:
                            priority_names = json.loads(priority_raw)
                            if (not isinstance(priority_names, list) or not priority_names
                                    or any(not isinstance(name, str) or not name.strip()
                                           for name in priority_names)):
                                raise ValueError("Indexer priority must be a JSON array of names")
                            indexer_priority = await provider.resolve_indexer_priority(tuple(priority_names))
                            indexers = tuple(indexer_id for indexer_id, _ in indexer_priority)
                        else:
                            indexer_priority = ()
                            indexers = tuple(int(value) for value in
                                             os.environ["MUSICSTREAM_TORRENT_ALLOWED_INDEXER_IDS"].split(","))
                        provider.indexer_ids = indexers
                        search = ApprovedProwlarrSource(
                            provider, approved, allow_unlisted=allow_unlisted_public,
                            indexer_priority=indexer_priority)
                        resolver = TorrentMetadataResolver(
                            prowlarr_client, allowed_indexer_ids=indexers,
                            approved_info_hashes=public_hashes,
                            allow_unlisted_public=allow_unlisted_public,
                            public_bind_ip=os.getenv("MUSICSTREAM_TORRENT_PUBLIC_BIND_IP")
                            if network_mode == "public_discovery" else None)
                    auto = AutoTorrentPlayback(
                        search, resolver, registry, peers=peers,
                        preparation_timeout_seconds=120 if getattr(search, "indexer_priority", ()) else 60)
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
