"""Conservative metadata heuristics. A likely match is never a recording guarantee."""

import re
import unicodedata
from difflib import SequenceMatcher

from musicstream.audio_providers.base import MatchAssessment, RecordingComparison, RecordingReference


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).casefold()
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w]+", " ", value, flags=re.UNICODE).split())


VERSIONS = ("live", "remix", "cover", "instrumental", "karaoke", "acoustic",
            "sped up", "slowed", "remaster", "remastered", "radio edit", "extended",
            "clean", "explicit", "ремикс", "лайв", "кавер")


def versions(title: str) -> set[str]:
    normalized = f" {normalize(title)} "
    return {"remaster" if tag == "remastered" else tag for tag in VERSIONS if f" {tag} " in normalized}


def assess(recording: RecordingReference, title: str, artist: str | None, duration: float | None) -> MatchAssessment:
    reasons = []
    wanted_title = normalize(recording.title)
    wanted_artist = normalize(recording.artist)
    actual_title = normalize(title)
    actual_artist = normalize(artist or "")
    wanted_versions, actual_versions = versions(recording.title), versions(title)
    # A positive catalog flag can confirm an explicit label on the source.
    explicit_label_confirmed = recording.explicit_lyrics is True and "explicit" in actual_versions
    if explicit_label_confirmed:
        wanted_versions.add("explicit")
    artist_match = bool(wanted_artist) and (
        f" {wanted_artist} " in f" {actual_title} " or
        f" {wanted_artist} " in f" {actual_artist} "
    )
    stripped = actual_title.replace(wanted_artist, " ") if wanted_artist else actual_title
    if explicit_label_confirmed and "explicit" not in versions(recording.title):
        stripped = " ".join(word for word in stripped.split() if word != "explicit")
    noise = {"official", "music", "video", "audio", "lyrics", "lyric", "hd", "4k", "visualizer"}
    stripped = " ".join(word for word in stripped.split() if word not in noise)
    title_match = SequenceMatcher(None, wanted_title, stripped).ratio() >= .86
    if not title_match:
        reasons.append("title_differs")
    if not artist_match:
        reasons.append("artist_unconfirmed")
    if wanted_versions != actual_versions:
        reasons.append("version_differs")
    # An explicit catalog flag conflicts with a positively labelled clean source.
    # False/unknown is never interpreted as evidence of a clean version.
    if recording.explicit_lyrics is True and "clean" in versions(title) and "version_differs" not in reasons:
        reasons.append("version_differs")
    if recording.explicit_lyrics is True and "explicit" not in versions(title) and "clean" not in versions(title):
        reasons.append("explicit_unconfirmed")
    if recording.duration_seconds is None or duration is None:
        reasons.append("duration_unknown")
    elif abs(recording.duration_seconds - duration) > max(5, recording.duration_seconds * .03):
        reasons.append("duration_differs")
    if any(reason in reasons for reason in ("version_differs", "duration_differs", "title_differs")):
        status = "mismatch"
    elif reasons:
        status = "uncertain"
    else:
        status = "likely"
    return MatchAssessment(status=status, reasons=reasons, comparison=RecordingComparison(
        reference_title=recording.title, reference_artist=recording.artist,
        reference_duration_seconds=recording.duration_seconds,
        candidate_title=title, candidate_artist=artist, candidate_duration_seconds=duration,
        duration_difference_seconds=(duration - recording.duration_seconds)
        if duration is not None and recording.duration_seconds is not None else None,
        duration_tolerance_seconds=max(5, recording.duration_seconds * .03)
        if recording.duration_seconds is not None else None,
    ))
