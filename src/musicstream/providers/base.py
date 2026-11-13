from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel


class Track(BaseModel):
    id: str | None = None
    title: str | None = None
    artist: str | None = None
    duration_seconds: float | None = None
    album: str | None = None
    musicbrainz_id: str | None = None
    cover_url: str | None = None
    isrc: str | None = None
    explicit_lyrics: bool | None = None
    rank: int | None = None
    source: Literal["deezer", "musicbrainz"]


class ProviderError(Exception):
    def __init__(self, message: str, status_code: int = 502, retry_after: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class SearchProvider(ABC):
    @abstractmethod
    async def search(self, query: str, limit: int = 10) -> list[Track]:
        """Find recordings matching the query."""
