"""Opt-in, read-only Prowlarr music discovery. Never fetches a release payload."""

from __future__ import annotations

import os
import hashlib
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .timing import current_timing


class ProwlarrError(RuntimeError):
    """A search failed; this must not be interpreted as zero results."""


class ProwlarrAuthError(ProwlarrError):
    pass


class ProwlarrUnavailableError(ProwlarrError):
    pass


class ProwlarrIndexerError(ProwlarrUnavailableError):
    pass


class ProwlarrResponseError(ProwlarrError):
    pass


@dataclass(frozen=True)
class MusicRelease:
    source: str
    source_id: str
    title: str
    category: tuple[str, ...]
    size_bytes: int
    seeders: int | None
    indexer_id: int
    indexer: str
    info_url: str | None = None
    magnet_uri: str | None = None
    info_hash: str | None = None
    metadata_url: str | None = None
    peers: int | None = None


def release_fingerprint(release: MusicRelease) -> str:
    """Stable local approval token without exposing an indexer URL or GUID."""
    return "sha256:" + hashlib.sha256(release.source_id.encode("utf-8")).hexdigest()


def _local_url(value: object, base_url: str) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    parsed, base = urlsplit(value), urlsplit(base_url)
    if (parsed.scheme, parsed.hostname, parsed.port) != (base.scheme, base.hostname, base.port):
        return None
    query = urlencode([(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                       if k.lower() not in {"apikey", "api_key"}])
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def _safe_identifier(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https"):
        return value
    query = urlencode([(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                       if k.lower() not in {"apikey", "api_key"}])
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_releases(data: object, *, base_url: str) -> tuple[MusicRelease, ...]:
    if not isinstance(data, list):
        raise ProwlarrResponseError("Prowlarr search response is not a list")
    releases = []
    for row in data:
        if not isinstance(row, dict):
            raise ProwlarrResponseError("Invalid Prowlarr release")
        if row.get("protocol") != "torrent":
            continue
        title, guid = row.get("title"), row.get("guid")
        indexer_id, size = row.get("indexerId"), row.get("size")
        if (not isinstance(title, str) or not title.strip() or not isinstance(guid, str)
                or not guid or not isinstance(indexer_id, int) or isinstance(indexer_id, bool)
                or not isinstance(size, int) or isinstance(size, bool) or size < 0):
            raise ProwlarrResponseError("Prowlarr release lacks required fields")
        seeds = row.get("seeders")
        if not isinstance(seeds, int) or isinstance(seeds, bool) or seeds < 0:
            seeds = None
        peers = row.get("peers")
        if not isinstance(peers, int) or isinstance(peers, bool) or peers < 0:
            peers = None
        categories = row.get("categories")
        if not isinstance(categories, list):
            raise ProwlarrResponseError("Invalid Prowlarr categories")
        names = tuple(c["name"] for c in categories if isinstance(c, dict)
                      and isinstance(c.get("name"), str))
        magnet = row.get("magnetUrl")
        if not isinstance(magnet, str) or not magnet.startswith("magnet:?"):
            magnet = None
        info_hash = row.get("infoHash")
        if not isinstance(info_hash, str) or not info_hash:
            info_hash = None
        info_url = row.get("infoUrl")
        if not isinstance(info_url, str) or urlsplit(info_url).scheme not in ("http", "https"):
            info_url = None
        releases.append(MusicRelease(
            source="prowlarr", source_id=f"{indexer_id}:{_safe_identifier(guid)}", title=title.strip(),
            category=names, size_bytes=size, seeders=seeds, indexer_id=indexer_id,
            indexer=str(row.get("indexer") or ""),
            info_url=_safe_identifier(info_url) if info_url else None,
            magnet_uri=magnet, info_hash=info_hash,
            metadata_url=_local_url(row.get("downloadUrl"), base_url),
            peers=peers,
        ))
    return tuple(releases)


class ProwlarrDiscoveryProvider:
    """Search a local Prowlarr instance; API key is sent only in a header."""

    def __init__(self, client: httpx.AsyncClient, *, api_key: str | None = None,
                 base_url: str = "http://127.0.0.1:18746", indexer_ids: tuple[int, ...] = ()):
        parsed = urlsplit(base_url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.path not in ("", "/") or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ValueError("Prowlarr discovery requires a local HTTP endpoint")
        self.api_key = api_key or os.environ.get("MUSICSTREAM_PROWLARR_API_KEY")
        if not self.api_key:
            raise ValueError("Set MUSICSTREAM_PROWLARR_API_KEY")
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.indexer_ids = indexer_ids

    async def resolve_indexer_priority(self, names: tuple[str, ...]) -> tuple[tuple[int, str], ...]:
        """Resolve configured torrent indexer names from this Prowlarr instance."""
        if not names or len({name.strip().casefold() for name in names}) != len(names):
            raise ValueError("Indexer priority requires distinct names")
        try:
            response = await self.client.get(
                f"{self.base_url}/api/v1/indexer",
                headers={"X-Api-Key": self.api_key}, timeout=10,
            )
        except httpx.RequestError as exc:
            raise ProwlarrUnavailableError("Could not load configured indexers") from exc
        if response.status_code in (401, 403):
            raise ProwlarrAuthError(f"Prowlarr returned HTTP {response.status_code}")
        if not response.is_success:
            raise ProwlarrUnavailableError(f"Prowlarr returned HTTP {response.status_code}")
        try:
            entries = response.json()
        except ValueError as exc:
            raise ProwlarrResponseError("Invalid indexer list") from exc
        if not isinstance(entries, list):
            raise ProwlarrResponseError("Invalid indexer list")
        result = []
        for requested in names:
            matching = [entry for entry in entries if isinstance(entry, dict)
                        and isinstance(entry.get("name"), str)
                        and entry["name"].strip().casefold() == requested.strip().casefold()
                        and entry.get("enable") is True and entry.get("protocol") == "torrent"
                        and isinstance(entry.get("id"), int) and not isinstance(entry["id"], bool)
                        and entry["id"] > 0]
            if len(matching) != 1:
                raise ValueError(f"Configured torrent indexer unavailable: {requested}")
            result.append((matching[0]["id"], matching[0]["name"]))
        return tuple(result)

    async def search(self, artist: str, album: str, *, limit: int = 20,
                     indexer_ids: tuple[int, ...] | None = None) -> tuple[MusicRelease, ...]:
        if not artist.strip() or not album.strip() or not 1 <= limit <= 100:
            raise ValueError("Artist, album and limit are required")
        selected_indexers = self.indexer_ids if indexer_ids is None else indexer_ids
        if not selected_indexers and indexer_ids is not None:
            raise ValueError("Explicit indexer search requires an indexer")
        params: list[tuple[str, str | int]] = [
            ("query", f"{artist.strip()} {album.strip()}"), ("type", "search"),
            ("categories", 3000), ("limit", limit), ("offset", 0),
        ]
        params.extend(("indexerIds", i) for i in selected_indexers)
        search_started = datetime.now(timezone.utc)
        request_started = time.monotonic()
        try:
            response = await self.client.get(
                f"{self.base_url}/api/v1/search", params=params,
                headers={"X-Api-Key": self.api_key}, timeout=30,
            )
        except httpx.RequestError as exc:
            raise ProwlarrUnavailableError("Could not reach Prowlarr or its indexer") from exc
        finally:
            timing = current_timing.get()
            if timing:
                timing.mark("prowlarr_request", operation_started=request_started,
                            indexer_id=selected_indexers[0] if len(selected_indexers) == 1 else None)
        if response.status_code in (401, 403):
            raise ProwlarrAuthError(f"Prowlarr returned HTTP {response.status_code}")
        if response.status_code >= 500:
            raise ProwlarrUnavailableError(f"Prowlarr returned HTTP {response.status_code}")
        if response.is_error:
            raise ProwlarrResponseError(f"Prowlarr returned HTTP {response.status_code}")
        try:
            releases = parse_releases(response.json(), base_url=self.base_url)
        except ValueError as exc:
            raise ProwlarrResponseError("Prowlarr returned invalid JSON") from exc
        if selected_indexers:
            releases = tuple(release for release in releases
                             if release.indexer_id in selected_indexers)
        if timing:
            timing.mark("prowlarr_results", operation_started=request_started,
                        indexer_id=selected_indexers[0] if len(selected_indexers) == 1 else None,
                        status=str(len(releases)))
        if not releases:
            try:
                status = await self.client.get(
                    f"{self.base_url}/api/v1/indexerstatus",
                    headers={"X-Api-Key": self.api_key}, timeout=8,
                )
            except httpx.RequestError as exc:
                raise ProwlarrUnavailableError("Could not verify indexer status") from exc
            if not status.is_success:
                raise ProwlarrUnavailableError("Could not verify indexer status")
            try:
                entries = status.json()
            except ValueError as exc:
                raise ProwlarrResponseError("Invalid indexer status response") from exc
            if not isinstance(entries, list):
                raise ProwlarrResponseError("Invalid indexer status response")
            now = datetime.now(timezone.utc)
            for entry in entries:
                if not isinstance(entry, dict) or (selected_indexers and entry.get("indexerId") not in selected_indexers):
                    continue
                disabled = entry.get("disabledTill")
                if isinstance(disabled, str):
                    try:
                        if _timestamp(disabled) > now:
                            raise ProwlarrIndexerError("Indexer is temporarily disabled")
                    except ValueError:
                        raise ProwlarrResponseError("Invalid indexer status timestamp") from None
                failed = entry.get("mostRecentFailure")
                if isinstance(failed, str):
                    try:
                        if _timestamp(failed) >= search_started:
                            raise ProwlarrIndexerError("Indexer failed during search")
                    except ValueError:
                        raise ProwlarrResponseError("Invalid indexer failure timestamp") from None
        return releases
