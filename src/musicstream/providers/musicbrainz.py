import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from musicstream.providers.base import ProviderError, SearchProvider, Track


class RateLimiter:
    """Serialize requests in this process, spacing starts by at least one second."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._next_request = 0.0

    @asynccontextmanager
    async def slot(self):
        async with self._lock:
            delay = self._next_request - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_request = time.monotonic() + 1.0
            yield

    def defer(self, seconds: float) -> None:
        self._next_request = max(self._next_request, time.monotonic() + seconds)


# All provider instances share a limiter unless an application supplies its own.
_shared_limiter = RateLimiter()


class _Artist(BaseModel):
    name: str | None = None


class _Credit(BaseModel):
    name: str | None = None
    artist: _Artist | None = None
    joinphrase: str = ""


class _Release(BaseModel):
    title: str | None = None


class _Recording(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: str | None = None
    title: str | None = None
    length: int | None = Field(default=None, ge=0)
    artist_credit: list[_Credit] = Field(default_factory=list, alias="artist-credit")
    releases: list[_Release] = Field(default_factory=list)


class _SearchResponse(BaseModel):
    recordings: list[_Recording]


def _retry_delay(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(int(value)) if int(value) >= 0 else None
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


class MusicBrainzProvider(SearchProvider):
    def __init__(self, client: httpx.AsyncClient, limiter: RateLimiter | None = None):
        self.client = client
        self.limiter = limiter if limiter is not None else _shared_limiter

    async def search(self, query: str, limit: int = 10) -> list[Track]:
        try:
            async with self.limiter.slot():
                response = await self.client.get(
                    "https://musicbrainz.org/ws/2/recording/",
                    params={"query": query, "limit": limit, "fmt": "json"},
                )
                if response.status_code in (429, 503):
                    delay = _retry_delay(response.headers.get("Retry-After"))
                    if delay is not None:
                        self.limiter.defer(delay)
            if response.status_code == 429:
                delay = _retry_delay(response.headers.get("Retry-After"))
                raise ProviderError(
                    "MusicBrainz request limit reached; try again later", 503,
                    str(max(1, int(delay + 0.999))) if delay is not None else "1",
                )
            if response.status_code >= 500:
                raise ProviderError("MusicBrainz is temporarily unavailable", 503)
            response.raise_for_status()
            data = _SearchResponse.model_validate(response.json())
        except httpx.TimeoutException as exc:
            raise ProviderError("MusicBrainz request timed out", 504) from exc
        except httpx.RequestError as exc:
            raise ProviderError("Could not connect to MusicBrainz", 502) from exc
        except httpx.HTTPStatusError as exc:
            raise ProviderError("MusicBrainz rejected the request", 502) from exc
        except (ValidationError, ValueError) as exc:
            raise ProviderError("MusicBrainz returned an invalid response", 502) from exc

        tracks = []
        for recording in data.recordings[:limit]:
            artist = "".join(
                (credit.name or (credit.artist.name if credit.artist else "") or "")
                + credit.joinphrase
                for credit in recording.artist_credit
            ).strip() or None
            tracks.append(Track(
                id=recording.id,
                title=recording.title,
                artist=artist,
                duration_seconds=recording.length / 1000 if recording.length is not None else None,
                album=next((release.title for release in recording.releases if release.title), None),
                musicbrainz_id=recording.id,
                source="musicbrainz",
            ))
        return tracks
