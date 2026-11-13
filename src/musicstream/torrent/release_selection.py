"""Rank search hits, then select only a verified, unambiguous torrent file."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Awaitable, Callable

from .metadata import TorrentMetadata
from .prowlarr_discovery import MusicRelease, ProwlarrDiscoveryProvider
from .track_matcher import TrackMatch, TrackReference, match_track, normalize, similarity


@dataclass(frozen=True)
class RankedRelease:
    release: MusicRelease
    score: float
    track_match: TrackMatch | None
    reason: str


@dataclass(frozen=True)
class ReleaseSelection:
    ranked: tuple[RankedRelease, ...]
    release: MusicRelease | None
    file_index: int | None
    reason: str


def _name_score(name: str, release_title: str) -> float:
    words = normalize(name).split()
    title = normalize(release_title)
    if not words:
        return 0.0
    if all(word in title.split() for word in words):
        return 1.0
    return min(0.8, similarity(name, release_title))


def rank_releases(reference: TrackReference, releases: tuple[MusicRelease, ...],
                  metadata_by_id: dict[str, TorrentMetadata]) -> ReleaseSelection:
    ranked = []
    eligible: set[str] = set()
    for release in releases:
        artist = max(_name_score(a, release.title) for a in
                     (reference.artist, *reference.artist_aliases))
        album = _name_score(reference.album or "", release.title)
        song_hint = _name_score(reference.title, release.title)
        format_hint = 1.0 if "mp3" in normalize(release.title) else 0.3
        seeds = min(1.0, math.log1p(max(0, release.seeders or 0)) / math.log1p(30))
        match = (match_track(reference, metadata_by_id[release.source_id])
                 if release.source_id in metadata_by_id else None)
        track_quality = match.confidence if match and match.status == "matched" else 0.0
        score = (0.22 * artist + 0.20 * album + 0.06 * song_hint +
                 0.08 * format_hint + 0.04 * seeds + 0.40 * track_quality)
        if match and match.status in {"ambiguous", "unsupported"}:
            score *= 0.5
        if artist >= 0.85 and (not reference.album or album >= 0.75):
            eligible.add(release.source_id)
        ranked.append(RankedRelease(release, round(score, 3), match,
                                    f"artist={artist:.2f}; album={album:.2f}; "
                                    f"track={match.status if match else 'metadata_pending'}; "
                                    f"mp3_hint={format_hint:.1f}; seeders={release.seeders}"))
    ranked.sort(key=lambda x: (-x.score, x.release.source_id))
    choices = [r for r in ranked if r.release.source_id in eligible
               and r.track_match and r.track_match.status == "matched"
               and r.score >= 0.72]
    if not choices:
        return ReleaseSelection(tuple(ranked), None, None, "no_verified_track")
    best = choices[0]
    if len(choices) > 1 and best.score - choices[1].score < 0.05:
        return ReleaseSelection(tuple(ranked), None, None, "ambiguous_releases")
    return ReleaseSelection(tuple(ranked), best.release, best.track_match.file_index,
                            "unique_verified_track")


async def discover_and_match(
    reference: TrackReference, provider: ProwlarrDiscoveryProvider,
    metadata_resolver: Callable[[MusicRelease], Awaitable[TorrentMetadata | None]],
    *, max_candidates: int = 5,
) -> ReleaseSelection:
    """Choose automatically after resolving only the best candidate file lists.

    The resolver is injected so the caller controls metadata access. This
    function does not download torrents or start a libtorrent session.
    """
    if not 1 <= max_candidates <= 20:
        raise ValueError("max_candidates must be between 1 and 20")
    if not reference.album:
        return ReleaseSelection((), None, None, "album_required_for_search")
    releases = await provider.search(reference.artist, reference.album)
    preliminary = rank_releases(reference, releases, {})
    metadata = {}
    for candidate in preliminary.ranked[:max_candidates]:
        found = await metadata_resolver(candidate.release)
        if found is not None:
            metadata[candidate.release.source_id] = found
    return rank_releases(reference, releases, metadata)
