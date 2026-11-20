"""Metadata-only VK Audio search primitives for a future provider adapter.

This module never handles playback URLs, cookies, page storage, or audio bytes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from musicstream.audio_providers.base import MatchAssessment, RecordingReference
from musicstream.audio_providers.matching import assess


@dataclass(frozen=True)
class VKAudioRecording:
    """A recording exposed by VK's visible search results."""

    id: str
    title: str
    artist: str | None
    duration_seconds: int | None

    @property
    def external_url(self) -> str:
        return f"https://vk.com/audio{self.id}"


@dataclass(frozen=True)
class VKAudioSearchResult:
    query: str
    candidate_count: int
    recordings: tuple[VKAudioRecording, ...]


@dataclass(frozen=True)
class VKAudioMatch:
    recording: VKAudioRecording
    assessment: MatchAssessment


def recording_id(*values: Any) -> str | None:
    """Extract only VK's stable owner_audio identity, never access keys.

    VK's row markup has appeared as ``owner_audio`` (data-full-id) or as a
    JSON-like ``data-audio`` tuple whose first two members are owner and audio
    IDs. Other formats are rejected rather than guessed.
    """
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            owner, audio = value[0], value[1]
            if _signed_integer(owner) and _signed_integer(audio):
                return f"{int(owner)}_{int(audio)}"
            continue
        if isinstance(value, dict):
            owner = value.get("owner_id", value.get("ownerId"))
            audio = value.get("id", value.get("audio_id", value.get("audioId")))
            if _signed_integer(owner) and _signed_integer(audio):
                return f"{int(owner)}_{int(audio)}"
            continue
        text = str(value).strip()
        match = re.fullmatch(r"(-?\d+)_(-?\d+)", text)
        if match:
            return f"{int(match.group(1))}_{int(match.group(2))}"
        try:
            decoded = json.loads(text)
        except (ValueError, TypeError):
            continue
        if not isinstance(decoded, (dict, list, tuple)):
            continue
        found = recording_id(decoded)
        if found:
            return found
    return None


def recording_id_from_url(value: str) -> str | None:
    """Accept only a direct VK audio page URL with one explicit owner_audio ID."""
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value.strip())
        if (parsed.scheme != "https" or (parsed.hostname or "").casefold() not in
                {"vk.ru", "www.vk.ru", "vk.com", "www.vk.com"}
                or parsed.port not in (None, 443) or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            return None
    except ValueError:
        return None
    match = re.fullmatch(r"/audio(-?\d+)_(-?\d+)/?", parsed.path)
    if not match or int(match.group(1)) == 0 or int(match.group(2)) <= 0:
        return None
    return recording_id((match.group(1), match.group(2)))


def _signed_integer(value: Any) -> bool:
    return isinstance(value, (int, str)) and re.fullmatch(r"-?\d+", str(value)) is not None


def parse_duration(value: str | None) -> int | None:
    """Parse a displayed duration; a player position is never inferred here."""
    if not value:
        return None
    match = re.fullmatch(r"\s*(?:(\d{1,2}):)?([0-5]?\d):([0-5]\d)\s*", value)
    if not match:
        return None
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def clean_recordings(rows: list[dict[str, Any]]) -> tuple[VKAudioRecording, ...]:
    """Keep identifiable, visible-looking song rows and reject UI noise."""
    result: list[VKAudioRecording] = []
    seen: set[str] = set()
    for row in rows:
        identity = recording_id(row.get("full_id"), row.get("audio_data"), row.get("audio_id"), row.get("href_id"))
        if not identity:
            identity = recording_id((row.get("owner_id"), row.get("local_audio_id")))
        title = _clean_text(row.get("title"))
        artist = _clean_text(row.get("artist")) or None
        labels = " ".join(str(row.get(key) or "") for key in ("text", "label", "class_name")).casefold()
        if not identity or not title or identity in seen:
            continue
        if any(word in labels for word in ("advert", "sponsored", "реклама", "промо")):
            continue
        if len(title) > 200 or title.casefold() in {
            "play", "pause", "more", "подробнее", "результаты поиска", "введите запрос",
            "search results", "enter a query", "\u043f\u043e\u0438\u0441\u043a \u0430\u0443\u0434\u0438\u043e\u0437\u0430\u043f\u0438\u0441\u0435\u0439",
            "\u043f\u0440\u0435\u0434\u044b\u0434\u0443\u0449\u0430\u044f \u0430\u0443\u0434\u0438\u043e\u0437\u0430\u043f\u0438\u0441\u044c",
            "\u0441\u043b\u0435\u0434\u0443\u044e\u0449\u0430\u044f \u0430\u0443\u0434\u0438\u043e\u0437\u0430\u043f\u0438\u0441\u044c",
            "\u0441\u043e\u043e\u0431\u0449\u0435\u0441\u0442\u0432\u0430", "\u043f\u043e\u043a\u0430\u0437\u0430\u0442\u044c \u0432\u0441\u0435",
        }:
            continue
        seen.add(identity)
        duration_value = row.get("duration")
        duration = parse_duration(duration_value if isinstance(duration_value, str) else None)
        if duration is None and isinstance(duration_value, (int, float)) and not isinstance(duration_value, bool) and 0 <= duration_value <= 86400:
            duration = int(duration_value)
        if duration is None and isinstance(duration_value, str) and duration_value.isdigit() and int(duration_value) <= 86400:
            duration = int(duration_value)
        result.append(VKAudioRecording(identity, title, artist, duration))
    return tuple(result)


def match_recordings(reference: RecordingReference, result: VKAudioSearchResult) -> tuple[VKAudioMatch, ...]:
    """Rank VK search candidates through MusicStream's existing Matching Engine."""
    matches = [VKAudioMatch(item, assess(reference, item.title, item.artist, item.duration_seconds))
               for item in result.recordings]
    rank = {"likely": 0, "uncertain": 1, "mismatch": 2}
    return tuple(sorted(matches, key=lambda item: (rank[item.assessment.status], len(item.assessment.reasons))))


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())
