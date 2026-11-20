"""Deezer metadata search and paginated public catalog reads."""

import asyncio
import time
from collections import OrderedDict

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from musicstream.providers.base import ProviderError, SearchProvider, Track


class _Artist(BaseModel):
    name: str | None = None


class _Album(BaseModel):
    title: str | None = None
    cover: str | None = None
    cover_small: str | None = None
    cover_medium: str | None = None
    cover_big: str | None = None
    cover_xl: str | None = None


class _Track(BaseModel):
    model_config = ConfigDict(strict=True)
    id: int | None = None
    title: str | None = None
    artist: _Artist | None = None
    album: _Album | None = None
    duration: int | None = Field(default=None, ge=0)
    isrc: str | None = None
    explicit_lyrics: bool | None = None
    rank: int | None = Field(default=None, ge=0)
    track_position: int | None = Field(default=None, ge=0)


class _SearchResponse(BaseModel):
    data: list[_Track]


class _TrackContributor(BaseModel):
    name: str
    role: str


class _TrackDetail(_Track):
    contributors: list[_TrackContributor] = Field(default_factory=list)


class _Artist(BaseModel):
    id: int
    name: str
    picture: str | None = None
    picture_small: str | None = None
    picture_medium: str | None = None
    picture_big: str | None = None
    picture_xl: str | None = None
    nb_album: int | None = None
    nb_fan: int | None = None


class _ArtistSearchResponse(BaseModel):
    data: list[_Artist]
    total: int | None = None
    next: str | None = None


class _Release(BaseModel):
    id: int
    title: str
    link: str | None = None
    cover: str | None = None
    cover_small: str | None = None
    cover_medium: str | None = None
    cover_big: str | None = None
    cover_xl: str | None = None
    release_date: str | None = None
    record_type: str | None = None
    artist: _Artist | None = None
    nb_tracks: int | None = None
    explicit_lyrics: bool | None = None
    type: str | None = None


class _Page(BaseModel):
    data: list[dict]
    total: int | None = None
    next: str | None = None


class DeezerProvider(SearchProvider):
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self._catalog_cache: OrderedDict[tuple[str, tuple[tuple[str, str], ...]], tuple[float, object]] = OrderedDict()
        self._catalog_lock = asyncio.Lock()

    async def _get_json(self, path: str, params: dict[str, object]) -> object:
        key = (path, tuple(sorted((name, str(value)) for name, value in params.items())))
        async with self._catalog_lock:
            cached = self._catalog_cache.get(key)
            if cached and cached[0] > time.monotonic():
                self._catalog_cache.move_to_end(key)
                return cached[1]
        try:
            response = await self.client.get(f"https://api.deezer.com/{path}", params=params)
            if response.status_code == 429:
                raise ProviderError("Deezer request limit reached; try again later", 503,
                                    response.headers.get("Retry-After"))
            if response.status_code >= 500:
                raise ProviderError("Deezer is temporarily unavailable", 503)
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and payload.get("error") is not None:
                raise ProviderError("Deezer could not complete the catalog request", 502)
        except httpx.TimeoutException as exc:
            raise ProviderError("Deezer request timed out", 504) from exc
        except httpx.RequestError as exc:
            raise ProviderError("Could not connect to Deezer", 502) from exc
        except httpx.HTTPStatusError as exc:
            raise ProviderError("Deezer rejected the request", 502) from exc
        except ValueError as exc:
            raise ProviderError("Deezer returned an invalid response", 502) from exc
        async with self._catalog_lock:
            self._catalog_cache[key] = (time.monotonic() + 600, payload)
            self._catalog_cache.move_to_end(key)
            while len(self._catalog_cache) > 256:
                self._catalog_cache.popitem(last=False)
        return payload

    @staticmethod
    def _page(payload: object) -> tuple[list[dict], int | None, bool]:
        try:
            parsed = _Page.model_validate(payload)
        except ValidationError as exc:
            raise ProviderError("Deezer returned invalid catalog metadata", 502) from exc
        return parsed.data, parsed.total, bool(parsed.next)

    async def track_credits(self, track_id: str) -> tuple[str | None, str | None, int | None, list[str]]:
        payload = await self._get_json(f"track/{int(track_id)}", {})
        try:
            detail = _TrackDetail.model_validate(payload)
        except ValidationError as exc:
            raise ProviderError("Deezer returned invalid track credits", 502) from exc
        if detail.id != int(track_id):
            raise ProviderError("Deezer returned a different track", 502)
        return (detail.title, detail.artist.name if detail.artist else None, detail.duration,
                [credit.name for credit in detail.contributors if credit.role == "Main"])

    async def search_artists(self, query: str, limit: int = 10) -> dict:
        payload = await self._get_json("search/artist", {"q": query, "limit": limit})
        try:
            parsed = _ArtistSearchResponse.model_validate(payload)
        except ValidationError as exc:
            raise ProviderError("Deezer returned invalid artist search results", 502) from exc
        return {"data": [self._artist_dict(item) for item in parsed.data],
                "total": parsed.total, "has_more": bool(parsed.next)}

    @staticmethod
    def _artist_dict(item: _Artist) -> dict:
        return {"id": str(item.id), "name": item.name,
                "picture_url": item.picture_xl or item.picture_big or item.picture_medium or item.picture_small or item.picture,
                "album_count": item.nb_album, "fan_count": item.nb_fan}

    async def artist(self, artist_id: int) -> dict:
        payload = await self._get_json(f"artist/{artist_id}", {})
        try:
            item = _Artist.model_validate(payload)
        except ValidationError as exc:
            raise ProviderError("Deezer returned invalid artist metadata", 502) from exc
        return self._artist_dict(item)

    async def artist_top(self, artist_id: int, limit: int = 10) -> list[dict]:
        payload = await self._get_json(f"artist/{artist_id}/top", {"limit": limit})
        data, _, _ = self._page(payload)
        return [self._track_dict(item) for item in data]

    async def artist_albums(self, artist_id: int, index: int, limit: int) -> dict:
        payload = await self._get_json(f"artist/{artist_id}/albums", {"index": index, "limit": limit})
        data, total, has_more = self._page(payload)
        releases = []
        for raw in data:
            try:
                item = _Release.model_validate(raw)
            except ValidationError as exc:
                raise ProviderError("Deezer returned invalid release metadata", 502) from exc
            releases.append({"id": str(item.id), "title": item.title,
                             "cover_url": item.cover_xl or item.cover_big or item.cover_medium or item.cover_small or item.cover,
                             "release_date": item.release_date, "type": item.record_type or item.type,
                             "artist": item.artist.name if item.artist else None,
                             "track_count": item.nb_tracks, "explicit_lyrics": item.explicit_lyrics})
        return {"data": releases, "total": total, "has_more": has_more}

    async def album(self, album_id: int) -> dict:
        payload = await self._get_json(f"album/{album_id}", {})
        try:
            item = _Release.model_validate(payload)
        except ValidationError as exc:
            raise ProviderError("Deezer returned invalid album metadata", 502) from exc
        return {"id": str(item.id), "title": item.title,
                "cover_url": item.cover_xl or item.cover_big or item.cover_medium or item.cover_small or item.cover,
                "release_date": item.release_date, "type": item.record_type or item.type,
                "artist": item.artist.name if item.artist else None,
                "artist_id": str(item.artist.id) if item.artist else None,
                "track_count": item.nb_tracks, "explicit_lyrics": item.explicit_lyrics}

    async def album_tracks(self, album_id: int, index: int, limit: int) -> dict:
        payload = await self._get_json(f"album/{album_id}/tracks", {"index": index, "limit": limit})
        data, total, has_more = self._page(payload)
        return {"data": [self._track_dict(item, index + offset + 1) for offset, item in enumerate(data)],
                "total": total, "has_more": has_more}

    @staticmethod
    def _track_dict(raw: dict, number: int | None = None) -> dict:
        try:
            item = _Track.model_validate(raw)
        except ValidationError as exc:
            raise ProviderError("Deezer returned invalid track metadata", 502) from exc
        return {"id": str(item.id) if item.id is not None else None, "title": item.title,
                "artist": item.artist.name if item.artist else None,
                "album": item.album.title if item.album else None,
                "duration_seconds": item.duration, "cover_url": (
                    item.album.cover_xl or item.album.cover_big or item.album.cover_medium
                    or item.album.cover_small or item.album.cover if item.album else None),
                "isrc": item.isrc, "track_number": item.track_position or number,
                "rank": item.rank, "source": "deezer"}

    async def search(self, query: str, limit: int = 10) -> list[Track]:
        try:
            response = await self.client.get(
                "https://api.deezer.com/search",
                params={"q": query, "limit": limit},
            )
            if response.status_code == 429:
                raise ProviderError("Deezer request limit reached; try again later", 503)
            if response.status_code >= 500:
                raise ProviderError("Deezer is temporarily unavailable", 503)
            response.raise_for_status()
            payload = response.json()
            # Deezer can return API errors inside an HTTP 200 response.
            if isinstance(payload, dict) and payload.get("error") is not None:
                raise ProviderError("Deezer could not complete the search", 502)
            data = _SearchResponse.model_validate(payload)
        except httpx.TimeoutException as exc:
            raise ProviderError("Deezer request timed out", 504) from exc
        except httpx.RequestError as exc:
            raise ProviderError("Could not connect to Deezer", 502) from exc
        except httpx.HTTPStatusError as exc:
            raise ProviderError("Deezer rejected the request", 502) from exc
        except (ValidationError, ValueError) as exc:
            raise ProviderError("Deezer returned an invalid response", 502) from exc

        return [Track(
            id=str(item.id) if item.id is not None else None,
            title=item.title or None,
            artist=item.artist.name or None if item.artist else None,
            album=item.album.title or None if item.album else None,
            duration_seconds=item.duration,
            cover_url=(item.album.cover_xl or item.album.cover_big or item.album.cover_medium
                       or item.album.cover_small or item.album.cover or None) if item.album else None,
            isrc=item.isrc or None,
            explicit_lyrics=item.explicit_lyrics,
            rank=item.rank,
            source="deezer",
        ) for item in data.data[:limit]]
