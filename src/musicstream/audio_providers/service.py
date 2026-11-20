"""Discover, rank and remember metadata independently of HTTP routes and players."""

import asyncio
import logging
import re
import time
from collections import OrderedDict
from urllib.parse import urlencode

from musicstream.audio_providers.base import DiscoveryBatch, ProviderSummary, RecordingReference, SourceResult
from musicstream.audio_providers.hitmos import valid_stream_url
from musicstream.audio_providers.selection import exact_metadata, hitmos_missing_duration_match, hitmos_unknown_explicit_match, rank_batch
from musicstream.audio_providers.matching import normalize, versions

logger = logging.getLogger(__name__)


def reference_key(recording: RecordingReference) -> tuple:
    # Preserve version labels and explicit unknown/false/true as distinct keys.
    return (recording.title, recording.artist, recording.duration_seconds, recording.explicit_lyrics)


def search_url(source: str, recording: RecordingReference) -> str:
    query = f"{recording.artist} {recording.title}"
    return ("https://ru.hitmox.org/search?" + urlencode({"q": query}) if source == "hitmos" else
            "https://www.youtube.com/results?" + urlencode({"search_query": query}))


def _equivalent_hitmos_group(recording: RecordingReference, candidates):
    """Return confirmed duplicate sources for one version, otherwise an empty group."""
    if len(candidates) < 2 or recording.duration_seconds is None:
        return []
    wanted_title = normalize(recording.title)
    wanted_artist = normalize(recording.artist)
    wanted_versions = versions(f"{recording.title} {recording.artist}")
    durations = []
    for candidate in candidates:
        if (candidate.source != "hitmos" or candidate.match.status != "likely"
                or not exact_metadata(recording, candidate)
                or normalize(candidate.title) != wanted_title
                or normalize(candidate.artist or "") != wanted_artist
                or versions(f"{candidate.title} {candidate.artist or ''}") != wanted_versions
                or candidate.duration_seconds is None):
            return []
        durations.append(candidate.duration_seconds)
    # A one-second catalog rounding difference is common; larger spreads may
    # indicate a different edit, so they remain a user choice.
    if max(durations) - min(durations) > 1:
        return []
    # rank_batch already preserves the source's order when quality is tied.
    return candidates


class SourceSelectionService:
    MAX_ENTRIES = 64
    MATCH_TTL = 600
    PROVIDER_TIMEOUT = 8
    HITMOS_PROVIDER_TIMEOUT = 14

    def __init__(self, providers, *, clock=time.monotonic):
        self.providers = providers
        self.clock = clock
        self._batches = OrderedDict()
        self._matches = OrderedDict()
        self._gates = {name: asyncio.Semaphore(1) for name in ("hitmos", "youtube")}
        self._outbound = asyncio.Semaphore(2)

    def _get(self, cache, key):
        for expired in [k for k, (until, _) in cache.items() if until <= self.clock()]:
            del cache[expired]
        value = cache.get(key)
        if value is None:
            return None
        cache.move_to_end(key)
        return value[1].model_copy(deep=True)

    def _put(self, cache, key, value, ttl):
        cache[key] = (self.clock() + ttl, value.model_copy(deep=True))
        cache.move_to_end(key)
        while len(cache) > self.MAX_ENTRIES:
            cache.popitem(last=False)

    async def _discover(self, source, recording):
        provider = self.providers[source]
        key = (source, id(provider), reference_key(recording), recording.deezer_id if source == "hitmos" else None)
        url = search_url(source, recording)
        try:
            # Waiting for an occupied provider is included in its deadline.
            provider_timeout = (self.HITMOS_PROVIDER_TIMEOUT if source == "hitmos"
                               else self.PROVIDER_TIMEOUT)
            async with asyncio.timeout(provider_timeout):
                async with self._gates[source]:
                    cached = self._get(self._batches, key)
                    if cached is not None:
                        cached.reused_cache = True
                        cached.diagnostics = None
                        return cached
                    async with self._outbound:
                        batch = await provider.discover(recording)
                    if not isinstance(batch, DiscoveryBatch) or batch.source != source:
                        raise ValueError("Invalid discovery contract")
                    self._put(self._batches, key, batch, 15 if batch.status == "error" else 30 if batch.status == "blocked" else 60)
                    return batch.model_copy(deep=True)
        except TimeoutError:
            reason, message = "provider_timeout", "Источник не ответил вовремя. Это не означает, что композиция отсутствует."
        except Exception:
            # No exception, request header or upstream response is exposed.
            reason, message = "provider_error", "Источник недоступен. Можно открыть оригинальный сервис."
        failure = DiscoveryBatch(source=source, status="error", reason=reason, message=message, search_url=url, complete=False)
        self._put(self._batches, key, failure, 15)
        return failure

    async def discover(self, recording: RecordingReference, *, allow_youtube=False, exclude_hitmos_id=None) -> SourceResult:
        remembered = self._get(self._matches, reference_key(recording))
        if (remembered and remembered.selected_source in self.providers
                and (remembered.selected_source != "hitmos" or remembered.selected_id != exclude_hitmos_id)
                and (remembered.selected_source != "youtube" or allow_youtube)):
            remembered.reused_match = True
            remembered.reused_cache = True
            remembered.diagnostics = None
            remembered.message = "Повторно выбрано ранее проверенное соответствие метаданных. Нажмите Play: источник будет проверен заново."
            return remembered
        names = [name for name in ("hitmos", "youtube") if name in self.providers and (name != "youtube" or allow_youtube)]
        batches = await asyncio.gather(*(self._discover(name, recording) for name in names))
        if exclude_hitmos_id is not None:
            batches = [batch.model_copy(update={
                "recordings": [item for item in batch.recordings if item.id != exclude_hitmos_id],
                "auto_candidate_id": None if batch.auto_candidate_id == exclude_hitmos_id else batch.auto_candidate_id,
            }) if batch.source == "hitmos" else batch for batch in batches]
        if not allow_youtube:
            batches.append(DiscoveryBatch(source="youtube", status="blocked", reason="consent_required",
                message="Для поиска и плеера YouTube требуется ваше согласие.", search_url=search_url("youtube", recording)))
        matching_started = time.monotonic()
        hitmos_batch = next((batch for batch in batches if batch.source == "hitmos"), None)
        hitmos_reference = (recording.model_copy(update={"artist": hitmos_batch.hitmos_artist})
                            if hitmos_batch and hitmos_batch.hitmos_artist else recording)
        results = [rank_batch(recording, batch) for batch in batches]
        candidates = [candidate for result in results for candidate in result.candidates]
        candidates.sort(key=lambda c: ({"likely": 0, "uncertain": 1, "mismatch": 2}[c.match.status],
                                       -(c.match_score or 0), c.source))
        eligible = [c for c in candidates if c.match.status != "mismatch"
                    and (c.source != "hitmos" or (
                        normalize(c.title) == normalize(recording.title)
                        and (not c.artist or normalize(c.artist) == normalize(hitmos_reference.artist))))]
        complete = all(batch.complete for batch in batches if batch.status == "ok") and not any(batch.status == "error" for batch in batches)
        confirmed = [candidate for candidate in eligible if candidate.match.status == "likely"]
        equivalent_group = _equivalent_hitmos_group(hitmos_reference, confirmed)
        fast_candidate = next((candidate for candidate in confirmed
                               if hitmos_batch and candidate.id == hitmos_batch.auto_candidate_id
                               and candidate.source == "hitmos" and (candidate.match_score or 0) >= .90
                               and exact_metadata(hitmos_reference, candidate)
                               and versions(f"{candidate.title} {candidate.artist or ''}")
                               == versions(f"{hitmos_reference.title} {hitmos_reference.artist}")
                               and not any(other.source != "hitmos" for other in confirmed)), None)
        first_confirmed_hitmos = next((candidate for candidate in confirmed
                                       if candidate.source == "hitmos" and (candidate.match_score or 0) >= .90
                                       and exact_metadata(hitmos_reference, candidate)
                                       and versions(f"{candidate.title} {candidate.artist or ''}")
                                       == versions(f"{hitmos_reference.title} {hitmos_reference.artist}")
                                       and not any(other.source != "hitmos" for other in confirmed)), None)
        require_unique_hitmos = bool(hitmos_batch and hitmos_batch.require_unique_hitmos_match)
        chosen = None if require_unique_hitmos else fast_candidate or first_confirmed_hitmos
        if chosen is None:
            reference = hitmos_reference if eligible and eligible[0].source == "hitmos" else recording
            chosen = (eligible[0] if (len(eligible) == 1 and complete
                                      and (eligible[0].source == "hitmos" or eligible[0].method == "youtube_iframe")
                                      and (exact_metadata(reference, eligible[0])
                                           or hitmos_missing_duration_match(reference, eligible[0])
                                           or hitmos_unknown_explicit_match(reference, eligible[0])))
                      else equivalent_group[0] if equivalent_group and not require_unique_hitmos
                      and not any(batch.status == "error" for batch in batches) else None)
        if chosen and chosen.source == "hitmos" and hitmos_batch.hitmos_manual_selection:
            chosen = None
        equivalent_ids = {candidate.id for candidate in equivalent_group}
        for candidate in candidates:
            candidate.selection_reason = ("recording_mismatch" if candidate.match.status == "mismatch" else
                                          "unique_high_confidence" if candidate is chosen else "manual_confirmation_required")
            if chosen and candidate.source == "hitmos" and candidate.id in equivalent_ids and candidate is not chosen:
                candidate.selection_reason = "equivalent_recording"
        trouble = any(batch.status != "ok" for batch in batches)
        if hitmos_batch and hitmos_batch.diagnostics:
            hitmos_batch.diagnostics.matching_elapsed_ms = round((time.monotonic() - matching_started) * 1000)
        result = SourceResult(source="auto", status="available" if chosen else "external_player" if candidates or trouble else "unavailable",
            reason="unique_high_confidence" if chosen else "ambiguous_candidates" if candidates else "sources_unavailable" if trouble else "no_results",
            message="Источник подобран по метаданным. Нажмите Play для проверки воспроизведения." if chosen else
                    "Нужен ваш выбор записи; однозначное совпадение не подтверждено." if candidates else
                    "Автоматический подбор недоступен. Причины по источникам указаны ниже; доступны оригинальные сервисы и раздел «Дополнительно»." if trouble else
                    "Доступные источники не вернули записей по этому запросу.",
            search_url=search_url("hitmos", recording), candidates=candidates,
            selection="automatic" if chosen else "choose" if candidates else "none",
            selected_id=chosen.id if chosen else None, selected_source=chosen.source if chosen else None,
            stage="matching" if candidates else "source_search",
            diagnostics=(hitmos_batch.diagnostics if hitmos_batch and not hitmos_batch.reused_cache else None),
            reused_cache=bool(hitmos_batch and hitmos_batch.reused_cache),
            sources=[ProviderSummary(**batch.model_dump(include={"source", "status", "reason", "message", "search_url"})) for batch in batches])
        return result

    def remember(self, recording: RecordingReference, result: SourceResult):
        if result.status != "available" or result.source not in self.providers:
            return False
        candidate = next((c for c in result.candidates if exact_metadata(recording, c)
                          and (c.method == "youtube_iframe" or (c.method == "html_audio" and c.stream_url))), None)
        local_hitmos_stream = bool(candidate and candidate.stream_url and re.fullmatch(
            r"/api/audio/hitmos-stream/[A-Za-z0-9_-]{32,64}", candidate.stream_url))
        if candidate is None or (result.source == "hitmos" and not local_hitmos_stream and
                                 not valid_stream_url(candidate.stream_url, candidate.id)):
            return False
        safe = candidate.model_copy(deep=True, update={"source": result.source, "stream_url": None,
                    "method": "external_link" if result.source == "hitmos" else "youtube_iframe",
                    "selection_reason": "previously_verified"})
        remembered = SourceResult(source="auto", status="available", reason="previously_verified",
            message="Ранее проверенное соответствие метаданных.", search_url=safe.external_url,
            candidates=[safe], selection="automatic", selected_id=safe.id, selected_source=result.source,
            stage="matching", reused_match=True)
        self._put(self._matches, reference_key(recording), remembered, self.MATCH_TTL)
        return True

    def forget(self, recording, source, candidate_id):
        key = reference_key(recording)
        remembered = self._get(self._matches, key)
        if remembered and remembered.selected_source == source and remembered.selected_id == candidate_id:
            self._matches.pop(key, None)
            for cached in [k for k in self._batches if k[0] == source and k[2] == key]:
                del self._batches[cached]

    async def prepare(self, recording, source, candidate_id):
        pattern = r"[0-9]{1,12}" if source == "hitmos" else r"[a-zA-Z0-9_-]{11}"
        if source not in self.providers or not re.fullmatch(pattern, candidate_id):
            raise ValueError("Invalid source candidate")
        try:
            result = await self.providers[source].prepare(recording, candidate_id)
            if (not isinstance(result, SourceResult) or result.source != source
                    or (result.status == "available" and not any(c.id == candidate_id for c in result.candidates))):
                raise ValueError("Invalid preparation contract")
        except Exception as exc:
            logger.warning("Audio source preparation failed (%s)", type(exc).__name__)
            result = SourceResult(source=source, status="error", reason="provider_error", stage="stream_resolution",
                message="Не удалось подготовить источник. Можно открыть оригинальный сервис.", search_url=search_url(source, recording))
        if result.status != "available" or not self.remember(recording, result):
            self.forget(recording, source, candidate_id)
        return result
