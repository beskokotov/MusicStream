"""Shared conservative ranking. Scores order metadata; they are not probabilities."""

from difflib import SequenceMatcher

from musicstream.audio_providers.base import AudioCandidate, DiscoveryBatch, RecordingReference, SourceResult
from musicstream.audio_providers.matching import assess, assess_hitmos, normalize, same_hitmos_artists


def exact_metadata(recording: RecordingReference, candidate: AudioCandidate) -> bool:
    return (candidate.match.status == "likely" and recording.duration_seconds is not None
            and recording.duration_seconds > 0
            and normalize(candidate.title) == normalize(recording.title)
            and (same_hitmos_artists(recording.artist, candidate.artist) if candidate.source == "hitmos"
                 else normalize(candidate.artist or "") == normalize(recording.artist)))


def hitmos_missing_duration_match(recording: RecordingReference, candidate: AudioCandidate) -> bool:
    return (candidate.source == "hitmos"
            and recording.duration_seconds is not None and recording.duration_seconds > 0
            and candidate.duration_seconds is None
            and candidate.match.reasons == ["duration_unknown"]
            and normalize(candidate.title) == normalize(recording.title)
            and same_hitmos_artists(recording.artist, candidate.artist))


def hitmos_unknown_explicit_match(recording: RecordingReference, candidate: AudioCandidate) -> bool:
    return (recording.allow_unknown_hitmos_explicit
            and candidate.source == "hitmos"
            and candidate.match.status == "uncertain"
            and candidate.match.explicit_status == "unknown"
            and candidate.match.reasons == ["explicit_unconfirmed"]
            and recording.duration_seconds is not None and recording.duration_seconds > 0
            and candidate.duration_seconds is not None and candidate.duration_seconds > 0
            and normalize(candidate.title) == normalize(recording.title)
            and same_hitmos_artists(recording.artist, candidate.artist))


def rank_batch(recording: RecordingReference, batch: DiscoveryBatch) -> SourceResult:
    if batch.source == "hitmos" and batch.hitmos_artist:
        recording = recording.model_copy(update={"artist": batch.hitmos_artist})
    assess_candidate = assess_hitmos if batch.source == "hitmos" else assess
    candidates = []
    for item in batch.recordings:
        match = assess_candidate(recording, item.title, item.artist, item.duration_seconds)
        title = SequenceMatcher(None, normalize(recording.title), normalize(item.title)).ratio()
        artist = (1.0 if batch.source == "hitmos" and same_hitmos_artists(recording.artist, item.artist)
                  else SequenceMatcher(None, normalize(recording.artist), normalize(item.artist or "")).ratio())
        duration = 0.0
        if item.duration_seconds is not None and recording.duration_seconds is not None:
            tolerance = max(5, recording.duration_seconds * .03)
            duration = max(0.0, 1 - abs(item.duration_seconds - recording.duration_seconds) / tolerance)
        candidates.append(AudioCandidate(**item.model_dump(), source=batch.source, match=match,
                                         match_score=round(.6 * title + .3 * artist + .1 * duration, 4)))
    candidates.sort(key=lambda c: ({"likely": 0, "uncertain": 1, "mismatch": 2}[c.match.status], -(c.match_score or 0)))
    eligible = [c for c in candidates if c.match.status != "mismatch"]
    selected = eligible[0] if (len(eligible) == 1 and batch.complete and not batch.hitmos_manual_selection
                              and (batch.source == "hitmos" or eligible[0].method == "youtube_iframe")
                              and (exact_metadata(recording, eligible[0])
                                   or hitmos_missing_duration_match(recording, eligible[0])
                                   or hitmos_unknown_explicit_match(recording, eligible[0]))) else None
    for candidate in candidates:
        candidate.selection_reason = ("recording_mismatch" if candidate.match.status == "mismatch" else
                                      "unique_high_confidence" if candidate is selected else "manual_confirmation_required")
    available = any(c.method == "youtube_iframe" and c.match.status != "mismatch" for c in candidates)
    result = SourceResult(source=batch.source, status="available" if available else "error" if batch.status == "error" else
                        "external_player" if candidates or batch.status == "blocked" else "unavailable",
        reason=("unique_high_confidence" if selected else batch.reason if batch.retry_after_seconds is not None
                else "ambiguous_candidates" if candidates else batch.reason),
        message=("Найдено единственное точное совпадение метаданных. Нажмите Play; идентичность мастера и доступность воспроизведения ещё не подтверждены." if selected else
                 "Выберите запись: однозначное совпадение не подтверждено. Отклонённые версии не запускаются встроенным плеером."
                 if candidates and batch.retry_after_seconds is None else batch.message),
        stage="matching" if candidates else "source_search", search_url=batch.search_url,
        candidates=candidates, diagnostics=batch.diagnostics, retry_after_seconds=batch.retry_after_seconds,
        selection="automatic" if selected else "choose" if candidates else "none",
        selected_id=selected.id if selected else None, selected_source=batch.source if selected else None)
    if not batch.complete and candidates:
        result.message += " Часть выдачи не проверена; автоматический выбор отключён."
    return result
