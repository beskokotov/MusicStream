"""Shared conservative ranking. Scores order metadata; they are not probabilities."""

from difflib import SequenceMatcher

from musicstream.audio_providers.base import AudioCandidate, DiscoveryBatch, RecordingReference, SourceResult
from musicstream.audio_providers.matching import assess, normalize


def exact_metadata(recording: RecordingReference, candidate: AudioCandidate) -> bool:
    return (candidate.match.status == "likely" and recording.duration_seconds is not None
            and recording.duration_seconds > 0
            and normalize(candidate.title) == normalize(recording.title)
            and normalize(candidate.artist or "") == normalize(recording.artist))


def rank_batch(recording: RecordingReference, batch: DiscoveryBatch) -> SourceResult:
    candidates = []
    for item in batch.recordings:
        match = assess(recording, item.title, item.artist, item.duration_seconds)
        title = SequenceMatcher(None, normalize(recording.title), normalize(item.title)).ratio()
        artist = SequenceMatcher(None, normalize(recording.artist), normalize(item.artist or "")).ratio()
        duration = 0.0
        if item.duration_seconds is not None and recording.duration_seconds is not None:
            tolerance = max(5, recording.duration_seconds * .03)
            duration = max(0.0, 1 - abs(item.duration_seconds - recording.duration_seconds) / tolerance)
        candidates.append(AudioCandidate(**item.model_dump(), source=batch.source, match=match,
                                         match_score=round(.6 * title + .3 * artist + .1 * duration, 4)))
    candidates.sort(key=lambda c: ({"likely": 0, "uncertain": 1, "mismatch": 2}[c.match.status], -(c.match_score or 0)))
    eligible = [c for c in candidates if c.match.status != "mismatch"]
    selected = eligible[0] if (len(eligible) == 1 and batch.complete and exact_metadata(recording, eligible[0])
                              and (batch.source == "hitmos" or eligible[0].method == "youtube_iframe")) else None
    for candidate in candidates:
        candidate.selection_reason = ("recording_mismatch" if candidate.match.status == "mismatch" else
                                      "unique_high_confidence" if candidate is selected else "manual_confirmation_required")
    available = any(c.method == "youtube_iframe" and c.match.status != "mismatch" for c in candidates)
    result = SourceResult(source=batch.source, status="available" if available else "error" if batch.status == "error" else
                        "external_player" if candidates or batch.status == "blocked" else "unavailable",
        reason=("unique_high_confidence" if selected else "ambiguous_candidates" if candidates else batch.reason),
        message=("Найдено единственное точное совпадение метаданных. Нажмите Play; идентичность мастера и доступность воспроизведения ещё не подтверждены." if selected else
                 "Выберите запись: однозначное совпадение не подтверждено. Отклонённые версии не запускаются встроенным плеером." if candidates else batch.message),
        stage="matching" if candidates else "source_search", search_url=batch.search_url,
        candidates=candidates, diagnostics=batch.diagnostics,
        selection="automatic" if selected else "choose" if candidates else "none",
        selected_id=selected.id if selected else None, selected_source=batch.source if selected else None)
    if not batch.complete and candidates:
        result.message += " Часть выдачи не проверена; автоматический выбор отключён."
    return result
