"""Read-only TorAPI discovery prototype; never starts a torrent transfer."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx


@dataclass(frozen=True)
class TorApiSearchHit:
    id: str
    name: str
    category: str
    size: str
    seeds: int | None
    page_url: str


@dataclass(frozen=True)
class TorApiFileHint:
    name: str
    size: str


@dataclass(frozen=True)
class TorApiDetails:
    id: str
    name: str
    info_hash: str
    magnet: str
    files: tuple[TorApiFileHint, ...]


class TorApiDiscoveryError(RuntimeError):
    """TorAPI could not provide a trustworthy discovery response."""


class TorApiAccessDeniedError(TorApiDiscoveryError):
    """The API endpoint refused access."""


class TorApiUnavailableError(TorApiDiscoveryError):
    """The API or its upstream source is unavailable."""


class TorApiResponseError(TorApiDiscoveryError):
    """The API returned an unexpected response."""


def _check_result(data: object) -> bool:
    """Return true only for TorAPI's explicit empty-result message."""
    if not isinstance(data, dict) or not isinstance(data.get("Result"), str):
        return False
    result = data["Result"]
    if result == "No matches were found for your title":
        return True
    if "not available" in result.lower() or result == "No data":
        raise TorApiUnavailableError(f"TorAPI source unavailable: {result}")
    raise TorApiResponseError(f"Unexpected TorAPI result: {result}")


def parse_search(data: object) -> tuple[TorApiSearchHit, ...]:
    """TorAPI returns either a JSON array or a {'Result': ...} error object."""
    if _check_result(data):
        return ()
    if not isinstance(data, list):
        raise TorApiResponseError("Unexpected TorAPI search response")
    hits = []
    for row in data[:50]:
        if not isinstance(row, dict) or not str(row.get("Id", "")).isdigit():
            continue
        try:
            seeds = int(str(row.get("Seeds", "")).replace(" ", ""))
        except ValueError:
            seeds = None
        hits.append(TorApiSearchHit(
            id=str(row["Id"]), name=str(row.get("Name", "")),
            category=str(row.get("Category", row.get("Type", ""))),
            size=str(row.get("Size", "")), seeds=seeds,
            page_url=str(row.get("Url", "")),
        ))
    if data and not hits:
        raise TorApiResponseError("TorAPI search response has no valid results")
    return tuple(hits)


def parse_details(torrent_id: str, data: object) -> TorApiDetails | None:
    if _check_result(data):
        return None
    if not isinstance(data, list):
        raise TorApiResponseError("Unexpected TorAPI details response")
    if not data:
        return None
    if not isinstance(data[0], dict):
        raise TorApiResponseError("Invalid TorAPI details response")
    row = data[0]
    info_hash = str(row.get("Hash", ""))
    magnet = str(row.get("Magnet", ""))
    if not re.fullmatch(r"[0-9a-fA-F]{40}", info_hash):
        return None
    if urlsplit(magnet).scheme != "magnet" or f"urn:btih:{info_hash.lower()}" not in magnet.lower():
        return None
    raw_files = row.get("Files")
    files = tuple(
        TorApiFileHint(str(item.get("Name", "")), str(item.get("Size", "")))
        for item in raw_files if isinstance(item, dict) and item.get("Name")
    ) if isinstance(raw_files, list) else ()
    return TorApiDetails(str(torrent_id), str(row.get("Name", "")), info_hash.lower(), magnet, files)


class TorApiDiscoveryProvider:
    """Prototype transport with an injected client and no Source Manager wiring."""

    def __init__(self, client: httpx.AsyncClient, *, base_url: str = "https://torapi.vercel.app"):
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.netloc or parts.path not in ("", "/"):
            raise ValueError("Invalid TorAPI base URL")
        self.client = client
        self.base_url = base_url.rstrip("/")

    async def _get_json(self, path: str, params: dict[str, str | int]) -> object:
        try:
            response = await self.client.get(
                f"{self.base_url}{path}", params=params, timeout=8,
            )
        except httpx.RequestError as exc:
            raise TorApiUnavailableError("Could not reach TorAPI") from exc
        if response.status_code in (401, 403):
            raise TorApiAccessDeniedError(f"TorAPI returned HTTP {response.status_code}")
        if response.status_code >= 500:
            raise TorApiUnavailableError(f"TorAPI returned HTTP {response.status_code}")
        if response.is_error:
            raise TorApiResponseError(f"TorAPI returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise TorApiResponseError("TorAPI returned invalid JSON") from exc

    async def search_title(self, query: str, *, category: int = 0) -> tuple[TorApiSearchHit, ...]:
        if not query.strip() or len(query) > 200 or category < 0:
            raise ValueError("Invalid search query or category")
        data = await self._get_json(
            "/api/search/title/rutracker",
            {"query": query.strip(), "category": category, "page": 0},
        )
        return parse_search(data)

    async def details(self, torrent_id: str) -> TorApiDetails | None:
        if not re.fullmatch(r"[0-9]{1,12}", torrent_id):
            raise ValueError("Invalid RuTracker ID")
        data = await self._get_json(
            "/api/search/id/rutracker", {"query": torrent_id},
        )
        return parse_details(torrent_id, data)
