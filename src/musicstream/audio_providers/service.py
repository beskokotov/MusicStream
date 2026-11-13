"""Discover, rank and remember metadata independently of HTTP routes and players."""

import asyncio
import re
import time
from collections import OrderedDict
from urllib.parse import urlencode, urlsplit

from musicstream.audio_providers.base import DiscoveryBatch, ProviderSummary, RecordingReference, SourceResult
from musicstream.audio_providers.hitmos import CDN_HOSTS, valid_stream_url
from musicstream.audio_providers.selection import exact_metadata, rank_batch


def reference_key(recording: RecordingReference) -> tuple:
    # Preserve version labels and explicit unknown/false/true as distinct keys.
    return (recording.title, recording.artist, recording.duration_seconds, recording.explicit_lyrics)


def search_url(source: str, recording: RecordingReference) -> str:
    query = f"{recording.artist} {recording.title}"
    return ("https://ru.hitmox.org/search?" + urlencode({"q": query}) if source == "hitmos" else
            "https://www.youtube.com/results?" + urlencode({"search_query": query}))


class SourceSelectionService:
    MAX_ENTRIES = 64
    MATCH_TTL = 600
    PROVIDER_TIMEOUT = 8

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
        key = (source, id(provider), reference_key(recording))
        url = search_url(source, recording)
        try:
            # Waiting for an occupied provider is included in its deadline.
            async with asyncio.timeout(self.PROVIDER_TIMEOUT):
                async with self._gates[source]:
                    cached = self._get(self._batches, key)
                    if cached is not None:
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

    async def discover(self, recording: RecordingReference, *, allow_youtube=False) -> SourceResult:
        remembered = self._get(self._matches, reference_key(recording))
        if (remembered and remembered.selected_source in self.providers
                and (remembered.selected_source != "youtube" or allow_youtube)):
            remembered.reused_match = True
            remembered.message = "Повторно выбрано ранее проверенное соответствие метаданных. Нажмите Play: источник будет проверен заново."
            return remembered
        names = [name for name in ("hitmos", "youtube") if name in self.providers and (name != "youtube" or allow_youtube)]
        batches = await asyncio.gather(*(self._discover(name, recording) for name in names))
        if not allow_youtube:
            batches.append(DiscoveryBatch(source="youtube", status="blocked", reason="consent_required",
                message="Для поиска и плеера YouTube требуется ваше согласие.", search_url=search_url("youtube", recording)))
        results = [rank_batch(recording, batch) for batch in batches]
        candidates = [candidate for result in results for candidate in result.candidates]
        candidates.sort(key=lambda c: ({"likely": 0, "uncertain": 1, "mismatch": 2}[c.match.status], -(c.match_score or 0)))
        eligible = [c for c in candidates if c.match.status != "mismatch"]
        complete = all(batch.complete for batch in batches if batch.status == "ok") and not any(batch.status == "error" for batch in batches)
        chosen = eligible[0] if (len(eligible) == 1 and complete and exact_metadata(recording, eligible[0])
                                and (eligible[0].source == "hitmos" or eligible[0].method == "youtube_iframe")) else None
        for candidate in candidates:
            candidate.selection_reason = ("recording_mismatch" if candidate.match.status == "mismatch" else
                                          "unique_high_confidence" if candidate is chosen else "manual_confirmation_required")
        trouble = any(batch.status != "ok" for batch in batches)
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
            diagnostics=next((batch.diagnostics for batch in batches if batch.source == "hitmos"), None),
            sources=[ProviderSummary(**batch.model_dump(include={"source", "status", "reason", "message", "search_url"})) for batch in batches])
        return result

    def remember(self, recording: RecordingReference, result: SourceResult):
        if result.status != "available" or result.source not in self.providers:
            return False
        candidate = next((c for c in result.candidates if exact_metadata(recording, c)
                          and (c.method == "youtube_iframe" or (c.method == "html_audio" and c.stream_url))), None)
        if candidate is None or (result.source == "hitmos" and (not valid_stream_url(candidate.stream_url, candidate.id)
                                   or urlsplit(candidate.stream_url).hostname not in CDN_HOSTS)):
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
        except Exception:
            result = SourceResult(source=source, status="error", reason="provider_error", stage="stream_resolution",
                message="Не удалось подготовить источник. Можно открыть оригинальный сервис.", search_url=search_url(source, recording))
        if result.status != "available" or not self.remember(recording, result):
            self.forget(recording, source, candidate_id)
        return result
