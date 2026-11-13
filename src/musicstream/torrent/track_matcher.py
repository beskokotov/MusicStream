"""Match Deezer-style track metadata against a torrent's file list, offline."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import PurePosixPath

from musicstream.providers.base import Track

from .metadata import AUDIO_EXTENSIONS, TorrentFile, TorrentMetadata


_CYRILLIC = str.maketrans({
    **dict(zip("абвгдезийклмнопрстуфхц", "abvgdezijklmnoprstufhc")),
    "ё": "e", "ж": "zh", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
})
_VERSION = {"live", "remix", "acoustic", "instrumental", "karaoke", "demo", "remaster",
            "remastered", "edit", "mix", "radio", "концерт", "ремикс", "акустика", "демо"}


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def _latin(value: str) -> str:
    return normalize(value.translate(_CYRILLIC))


def similarity(left: str, right: str) -> float:
    a, b = normalize(left), normalize(right)
    if not a or not b:
        return 0.0
    if a == b or _latin(a) == _latin(b):
        return 1.0
    return max(SequenceMatcher(None, a, b).ratio(),
               SequenceMatcher(None, _latin(a), _latin(b)).ratio())


@dataclass(frozen=True)
class TrackReference:
    artist: str
    title: str
    album: str | None = None
    duration_seconds: float | None = None
    artist_aliases: tuple[str, ...] = ()

    @classmethod
    def from_deezer(cls, track: Track, *, artist_aliases: tuple[str, ...] = ()) -> TrackReference:
        if (track.source != "deezer" or not track.artist or not track.title
                or not track.artist.strip() or not track.title.strip()):
            raise ValueError("A Deezer track with artist and title is required")
        return cls(track.artist, track.title, track.album, track.duration_seconds, artist_aliases)


@dataclass(frozen=True)
class TrackMatch:
    file_index: int | None
    confidence: float
    reason: str
    status: str


def _track_name(file: TorrentFile, artists: tuple[str, ...]) -> str:
    stem = PurePosixPath(file.name).stem
    stem = re.sub(r"^\s*(?:cd|disc|диск)?\s*\d{1,2}(?:[._\- ]+\d{1,2})?[._\- ]+", "", stem, flags=re.I)
    stem = re.sub(r"^\s*\d{1,3}\s*[-._ ]+", "", stem)
    for artist in artists:
        # Strip a prefix only at a separator, so similar track words survive.
        pattern = r"^\s*" + re.escape(artist) + r"\s*[-–—]\s*"
        stem = re.sub(pattern, "", stem, flags=re.I)
    parts = re.split(r"\s+[-–—]\s+", stem, maxsplit=1)
    if len(parts) == 2 and any(similarity(parts[0], artist) >= 0.90 for artist in artists):
        stem = parts[1]
    return stem


def _version_terms(value: str) -> frozenset[str]:
    return frozenset(normalize(value).split()) & _VERSION


def match_track(reference: TrackReference, metadata: TorrentMetadata,
                *, threshold: float = 0.78, min_gap: float = 0.12) -> TrackMatch:
    if not reference.artist.strip() or not reference.title.strip():
        raise ValueError("Artist and title are required")
    audio = [f for f in metadata.files if f.extension.lower() in AUDIO_EXTENSIONS and f.size > 0]
    if not audio:
        return TrackMatch(None, 0, "no_audio_files", "missing")
    cue = any(f.extension.lower() == ".cue" for f in metadata.files)
    if len(audio) == 1 and cue:
        return TrackMatch(None, 0, "single_album_image_with_cue", "unsupported")
    artists = (reference.artist, *reference.artist_aliases)
    rated = []
    for file in audio:
        stem = _track_name(file, artists)
        title_score = similarity(reference.title, stem)
        # A track title must stand on its own; a matching album/artist cannot rescue it.
        if title_score < 0.68:
            continue
        file_path = normalize(file.path)
        artist_score = max(similarity(a, file_path) if normalize(a) == file_path else
                           float(normalize(a) in file_path or _latin(a) in _latin(file.path))
                           for a in artists)
        album_score = float(bool(reference.album) and
                            (normalize(reference.album) in file_path or
                             _latin(reference.album) in _latin(file.path)))
        version_penalty = 0.24 if _version_terms(reference.title) != _version_terms(stem) else 0
        score = min(1.0, 0.80 * title_score + 0.09 * artist_score +
                    0.06 * album_score + (0.05 if file.extension.lower() == ".mp3" else 0.02)
                    - version_penalty)
        rated.append((score, title_score, file))
    if not rated:
        return TrackMatch(None, 0, "track_title_not_found", "missing")
    rated.sort(key=lambda item: (-item[0], item[2].index))
    top, title_score, file = rated[0]
    if top < threshold or title_score < 0.78:
        return TrackMatch(None, round(top, 3), "weak_title_match", "uncertain")
    if len(rated) > 1 and top - rated[1][0] < min_gap:
        return TrackMatch(None, round(top, 3), "multiple_similar_audio_files", "ambiguous")
    return TrackMatch(file.index, round(top, 3),
                      f"title_match={title_score:.2f}; format={file.extension.lower()}; file={file.name}",
                      "matched")
