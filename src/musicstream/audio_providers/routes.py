import asyncio
import ipaddress
import logging
import os
import re
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.responses import Response

from musicstream.audio_providers.base import RecordingReference, SourceResult
from musicstream.audio_providers.hitmos import HitmosProvider
from musicstream.audio_providers.hitmos_browser import HitmosBrowserReader
from musicstream.audio_providers.transport import BoundedSourceClient
from musicstream.audio_providers.youtube import YouTubeProvider
from musicstream.audio_providers.service import SourceSelectionService
from musicstream.public_rate_limit import ClientRateLimiter
from musicstream.audio_providers.hitmos_stream import HitmosStreamProxy, diagnostic_host_value
from musicstream.audio_providers.transport import SourceFailure
from musicstream.audio_providers.vk_browser import (VKAudioBrowserReader, VKAudioProvider, VKLoginRequired,
    VKReaderOperationFailure)
from musicstream.audio_providers.vk import recording_id_from_url
from musicstream.audio_providers.vk_playback import VKPlaybackAdapter
from musicstream.audio_providers.playback_adapter import FIXTURE as PLAYBACK_TEST_FIXTURE, PlaybackAdapter

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
    vk_enabled = os.getenv("MUSICSTREAM_VK_ENABLED", "").strip().lower() == "true"
    vk_playback_experimental = os.getenv("MUSICSTREAM_VK_PLAYBACK_EXPERIMENTAL", "").strip().lower() == "true"
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=False, headers={"User-Agent": "MusicStream/0.1.0"}) as client:
        hitmos_reader = HitmosBrowserReader(diagnostics_enabled=local_diagnostics, lab_mode=lab_mode,
                                             request_limit=lab_request_limit if lab_mode else 5)
        vk_playback_adapter = VKPlaybackAdapter() if vk_enabled and vk_playback_experimental else None
        vk_provider = VKAudioProvider(VKAudioBrowserReader(), playback_adapter=vk_playback_adapter,
            playback_experimental=bool(vk_playback_adapter)) if vk_enabled else None
        app.state.audio_providers = {
            "youtube": YouTubeProvider(BoundedSourceClient(client), os.getenv("YOUTUBE_API_KEY", "").strip() or None),
            "hitmos": HitmosProvider(hitmos_reader,
                                     diagnostics_enabled=local_diagnostics, lab_mode=lab_mode,
                                     stream_proxy=hitmos_proxy),
        }
        if vk_provider:
            app.state.audio_providers["vk"] = vk_provider
        app.state.vk_enabled = vk_enabled
        app.state.vk_playback_experimental = bool(vk_playback_adapter)
        app.state.vk_playback_adapter = vk_playback_adapter
        playback_test_enabled = os.getenv("MUSICSTREAM_AUDIO_TRANSPORT_TEST", "").strip().lower() == "true"
        app.state.playback_test_adapter = PlaybackAdapter() if playback_test_enabled else None
        app.state.playback_test_enabled = playback_test_enabled
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
            if vk_provider:
                await vk_provider.close()


@router.get("/api/audio/config")
async def audio_config(request: Request):
    return {"vk_enabled": bool(getattr(request.app.state, "vk_enabled", False)),
            "vk_playback_experimental": bool(getattr(request.app.state, "vk_playback_experimental", False)),
            "playback_test_enabled": bool(getattr(request.app.state, "playback_test_enabled", False))}


def _local_vk_playback(request: Request):
    _local_vk_reader(request)
    adapter = getattr(request.app.state, "vk_playback_adapter", None)
    if adapter is None:
        raise HTTPException(404, "Not found")
    return adapter


@router.api_route("/api/audio/vk/playback/{token}/manifest", methods=["GET", "HEAD"], include_in_schema=False)
async def vk_playback_manifest(request: Request, token: str):
    adapter = _local_vk_playback(request)
    try:
        body, ttl = adapter.manifest(token)
    except ValueError as exc:
        reason = str(exc) if str(exc).startswith("vk_") else "vk_playback_id_expired"
        raise HTTPException(410, detail={"reason": reason}) from None
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if request.method == "HEAD":
        return Response(status_code=200, media_type="application/dash+xml", headers=headers)
    return Response(content=body, media_type="application/dash+xml", headers=headers)


async def _vk_playback_resource_response(request: Request, token: str, slot: str, suffix: str = ""):
    adapter = _local_vk_playback(request)
    try:
        client, upstream = await adapter.open_resource(token, slot, suffix,
            dict(request.query_params), request.method, request.headers.get("range"))
    except ValueError as exc:
        reason = str(exc) if str(exc).startswith("vk_") else "vk_resource_rejected"
        upstream_status = re.fullmatch(r"vk_segment_http_(\d{3})", reason)
        status = (410 if reason in {"vk_playback_id_expired", "vk_source_expired"}
                  else int(upstream_status.group(1)) if upstream_status and 400 <= int(upstream_status.group(1)) <= 499
                  else 403 if "origin" in reason or "cors" in reason else 502)
        raise HTTPException(status, detail={"reason": reason}) from None
    except (httpx.HTTPError, OSError):
        raise HTTPException(502, detail={"reason": "vk_segment_upstream_failed"}) from None
    response_headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    for name in ("content-type", "content-length", "content-range", "accept-ranges"):
        value = upstream.headers.get(name)
        if value and (name != "content-type" or re.fullmatch(r"(?:audio|video)/[a-z0-9.+-]+|application/octet-stream", value.split(";", 1)[0].strip().lower())):
            response_headers[name.title()] = value if name == "content-type" else value
    if request.method == "HEAD":
        status = upstream.status_code
        await upstream.aclose()
        await client.aclose()
        return Response(status_code=status, headers=response_headers)

    async def body():
        try:
            async for chunk in upstream.aiter_raw(chunk_size=65536):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(body(), status_code=upstream.status_code, headers=response_headers)


@router.api_route("/api/audio/vk/playback/{token}/resource/{slot}", methods=["GET", "HEAD"], include_in_schema=False)
async def vk_playback_resource(request: Request, token: str, slot: str):
    return await _vk_playback_resource_response(request, token, slot)


@router.api_route("/api/audio/vk/playback/{token}/resource/{slot}/{suffix:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def vk_playback_resource_suffix(request: Request, token: str, slot: str, suffix: str):
    return await _vk_playback_resource_response(request, token, slot, suffix)


def _local_vk_reader(request: Request):
    if not getattr(request.app.state, "vk_enabled", False):
        raise HTTPException(404, "Not found")
    peer = request.client.host if request.client else ""
    try:
        if not ipaddress.ip_address(peer.split("%", 1)[0]).is_loopback:
            raise HTTPException(404, "Not found")
    except ValueError:
        raise HTTPException(404, "Not found") from None
    host_header = request.headers.get("host", "")
    host_name = urlsplit(f"//{host_header}").hostname or ""
    if host_name.casefold() != "localhost":
        try:
            if not ipaddress.ip_address(host_name).is_loopback:
                raise HTTPException(404, "Not found")
        except ValueError:
            raise HTTPException(404, "Not found") from None
    origin = request.headers.get("origin")
    if origin:
        origin_url = urlsplit(origin)
        origin_host = origin_url.hostname or ""
        if origin_url.scheme != "http" or origin_host.casefold() != "localhost":
            try:
                if not ipaddress.ip_address(origin_host).is_loopback:
                    raise HTTPException(404, "Not found")
            except ValueError:
                raise HTTPException(404, "Not found") from None
    provider = getattr(request.app.state, "audio_providers", {}).get("vk")
    reader = getattr(provider, "reader", None)
    if reader is None:
        raise HTTPException(503, detail={"reason": "vk_reader_unavailable"})
    return reader


@router.post("/api/audio/vk/diagnostics/start", include_in_schema=False)
async def start_vk_playback_diagnostics(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    reader = _local_vk_reader(request)
    try:
        result = await reader.start_playback_diagnostics()
    except VKLoginRequired:
        raise HTTPException(401, detail={"reason": "vk_auth_required"}) from None
    except VKReaderOperationFailure as exc:
        logger.warning("VK Reader start failed: stage=%s code=%s error_type=%s", exc.stage, exc.code, exc.error_type)
        raise HTTPException(503, detail={"reason": exc.code, "stage": exc.stage,
                                        "error_type": exc.error_type}) from None
    except RuntimeError as exc:
        reason = str(exc) if str(exc) in {"playwright_unavailable", "vk_reader_page_unavailable"} else "vk_browser_unavailable"
        raise HTTPException(503, detail={"reason": reason}) from None
    except Exception as exc:
        logger.warning("VK Reader start failed: stage=diagnostics_start error_type=%s", type(exc).__name__)
        raise HTTPException(503, detail={"reason": "vk_browser_unavailable"}) from None
    return result


@router.get("/api/audio/vk/diagnostics", include_in_schema=False)
async def get_vk_playback_diagnostics(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return await _local_vk_reader(request).playback_diagnostics()


@router.post("/api/audio/vk/diagnostics/stop", include_in_schema=False)
async def stop_vk_playback_diagnostics(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return await _local_vk_reader(request).stop_playback_diagnostics()


@router.post("/api/audio/vk/resource-capture/start", include_in_schema=False)
async def start_vk_resource_capture(request: Request, response: Response):
    """Arm exact-ID Play/resource correlation on the existing VK Reader page."""
    response.headers["Cache-Control"] = "no-store"
    reader = _local_vk_reader(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, detail={"reason": "invalid_json"}) from None
    audio_id = body.get("vk_audio_id") if isinstance(body, dict) else None
    if not isinstance(audio_id, str) or not re.fullmatch(r"-?\d+_-?\d+", audio_id):
        raise HTTPException(422, detail={"reason": "invalid_vk_audio_id"})
    try:
        return await reader.arm_resource_capture(audio_id, str(request.base_url).rstrip("/"))
    except VKLoginRequired:
        raise HTTPException(401, detail={"reason": "vk_auth_required"}) from None
    except VKReaderOperationFailure as exc:
        logger.warning("VK resource capture failed: stage=%s code=%s error_type=%s", exc.stage, exc.code, exc.error_type)
        raise HTTPException(503, detail={"reason": exc.code, "stage": exc.stage,
                                        "error_type": exc.error_type}) from None
    except ValueError as exc:
        raise HTTPException(422, detail={"reason": str(exc)}) from None
    except RuntimeError as exc:
        reason = str(exc) if str(exc) in {"playwright_unavailable", "vk_reader_page_unavailable"} else "vk_browser_unavailable"
        raise HTTPException(503, detail={"reason": reason}) from None
    except Exception as exc:
        logger.warning("VK resource capture failed: stage=resource_capture_start error_type=%s", type(exc).__name__)
        raise HTTPException(503, detail={"reason": "vk_browser_unavailable"}) from None


@router.get("/api/audio/vk/resource-capture", include_in_schema=False)
async def get_vk_resource_capture(request: Request, response: Response):
    """Expose only descriptor metadata; URLs and request headers remain server-side."""
    response.headers["Cache-Control"] = "no-store"
    return await _local_vk_reader(request).resource_capture_status()


@router.get("/api/audio/playback-test/prepare")
async def prepare_playback_test(request: Request):
    adapter = getattr(request.app.state, "playback_test_adapter", None)
    if adapter is None:
        raise HTTPException(404, "Not found")
    grant = await adapter.prepare()
    return {"source": "musicstream_test_mp3", "playback_url": f"/api/audio/playback-test/{grant.token}",
            "expires_in_seconds": max(1, int(grant.expires_at - time.monotonic()))}


@router.api_route("/api/audio/playback-test/{token}", methods=["GET", "HEAD"], include_in_schema=False)
async def playback_test_stream(request: Request, token: str,
                               range_header: str | None = Header(default=None, alias="Range")):
    adapter = getattr(request.app.state, "playback_test_adapter", None)
    if adapter is None:
        raise HTTPException(404, "Not found")
    if not token.isascii() or not (20 <= len(token) <= 64) or not all(c.isalnum() or c in "-_" for c in token):
        raise HTTPException(404, "Not found")
    status, headers, start, length = await adapter.open(token, range_header)
    if request.method == "HEAD":
        return Response(status_code=status, headers=headers)

    async def body():
        with PLAYBACK_TEST_FIXTURE.open("rb") as source:
            source.seek(start)
            remaining = length
            while remaining:
                chunk = await asyncio.to_thread(source.read, min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(body(), status_code=status, headers=headers)


@router.get("/api/audio/hitmos-stream/{token}", include_in_schema=False)
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
    provider: Annotated[Literal["youtube", "hitmos", "vk", "auto"], Query()] = "youtube",
    hitmos_id: Annotated[str | None, Query(pattern=r"^[0-9]{1,12}$")] = None,
    vk_id: Annotated[str | None, Query(pattern=r"^-?[0-9]{1,12}_-?[0-9]{1,12}$")] = None,
    vk_url: Annotated[str | None, Query(max_length=2048)] = None,
    candidate_id: Annotated[str | None, Query(pattern=r"^[a-zA-Z0-9_-]{1,12}$")] = None,
    vk_refresh: bool = False,
    allow_youtube: bool = False,
    deezer_id: Annotated[str | None, Query(pattern=r"^[0-9]{1,20}$")] = None,
):
    peer = request.client.host if request.client else "unknown"
    cache_key = (title.strip(), artist.strip(), duration_seconds, explicit_lyrics,
                 provider, hitmos_id or vk_id or candidate_id or (recording_id_from_url(vk_url) if vk_url else None),
                 vk_refresh, allow_youtube, deezer_id)
    cache = request.app.state.audio_response_cache
    if (set(request.query_params) - {"title", "artist", "duration_seconds", "explicit_lyrics", "provider", "hitmos_id", "vk_id", "vk_url", "candidate_id", "vk_refresh", "allow_youtube", "deezer_id"}
            or not title.strip() or not artist.strip() or (hitmos_id and provider != "hitmos")
            or (vk_id and provider != "vk") or (hitmos_id and (candidate_id or vk_id)) or (vk_id and candidate_id)
            or (vk_url and (provider != "vk" or hitmos_id or vk_id or candidate_id or vk_refresh))
            or (vk_refresh and (provider != "vk" or vk_id or candidate_id or hitmos_id))
            or (candidate_id and provider in {"auto", "vk"})
            or (candidate_id and provider == "youtube" and len(candidate_id) != 11)
            or (candidate_id and provider == "hitmos" and not candidate_id.isascii())
            or (candidate_id and provider == "hitmos" and not candidate_id.isdigit())):
        raise HTTPException(422, "Invalid source lookup parameters")
    if provider == "vk" and not getattr(request.app.state, "vk_enabled", False):
        return SourceResult(source="vk", status="unavailable", reason="vk_disabled",
            message="VK Audio отключён. Для локального эксперимента установите MUSICSTREAM_VK_ENABLED=true и перезапустите MusicStream.",
            search_url="https://vk.com/audio")
    if (provider == "vk" or provider == "auto") and getattr(request.app.state, "vk_playback_experimental", False):
        _local_vk_reader(request)
        vk_provider = request.app.state.audio_providers.get("vk")
        if vk_provider is not None:
            vk_provider.musicstream_origin = str(request.base_url).rstrip("/")
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
                allow_youtube=allow_youtube,
                include_vk=bool(getattr(request.app.state, "vk_playback_experimental", False)))
        return cached[1].model_copy(deep=True, update={"diagnostics": None, "reused_cache": True})
    retry_after = request.app.state.audio_limiter.check(peer)
    if retry_after:
        raise HTTPException(429, "Source lookup limit reached", headers={"Retry-After": str(retry_after)})
    recording = RecordingReference(title=title.strip(), artist=artist.strip(), duration_seconds=duration_seconds, explicit_lyrics=explicit_lyrics)
    try:
        operation_timeout = (50 if provider == "vk" else 30 if getattr(request.app.state, "hitmos_lab_mode", False) and (hitmos_id or candidate_id)
                             else 15 if provider == "auto" or (provider == "hitmos" and not (hitmos_id or candidate_id))
                             else 10)
        async with asyncio.timeout(operation_timeout):
            if vk_refresh:
                result = await request.app.state.audio_providers["vk"].refresh_visible(recording)
            elif vk_url:
                if not recording_id_from_url(vk_url):
                    raise HTTPException(422, detail={"reason": "invalid_vk_audio_url"})
                result = await request.app.state.audio_providers["vk"].resolve_direct(recording, vk_url)
            elif hitmos_id or vk_id or candidate_id:
                result = await request.app.state.source_selection.prepare(recording, provider, hitmos_id or vk_id or candidate_id)
            elif provider == "auto":
                result = await request.app.state.source_selection.discover(recording, allow_youtube=allow_youtube,
                    include_vk=bool(getattr(request.app.state, "vk_playback_experimental", False)))
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
