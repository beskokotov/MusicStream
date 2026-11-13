"""Deezer metadata search; no playback or track-detail requests."""

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


class _SearchResponse(BaseModel):
    data: list[_Track]


class DeezerProvider(SearchProvider):
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

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
