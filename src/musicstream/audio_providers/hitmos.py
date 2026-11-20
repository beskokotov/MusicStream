import json
import asyncio
import hashlib
import math
import re
import time
from collections import OrderedDict
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit

from musicstream.audio_providers.base import AudioCandidate, AudioProvider, DiscoveredRecording, DiscoveryBatch, RecordingReference, ResolutionDiagnostics, SourceDocument, SourceResult
from musicstream.audio_providers.matching import assess, assess_hitmos, normalize, versions
from musicstream.audio_providers.selection import exact_metadata, hitmos_missing_duration_match, rank_batch
from musicstream.audio_providers.transport import SourceFailure
from musicstream.providers.base import ProviderError

MAX_SEARCH_CANDIDATES = 5
MAX_METADATA_ENRICHMENTS = 3
STREAM_PREPARATION_ENRICHMENT_TIMEOUT_SECONDS = 6.0
HITMOS_BUDGET_MESSAGE = "Достигнут локальный лимит запросов Hitmos. Повторите попытку после паузы."


class _StructuredMetadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.parts = []
        self.blocks = []
        self.stream_entries = []
        self.song_ids = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        href = attributes.get("href", "")
        match = re.fullmatch(r"(?:https://ru\.hitmox\.org)?/song/([0-9]{1,12})/?", href)
        if match and match[1] not in self.song_ids:
            self.song_ids.append(match[1])
        if attributes.get("data-musid") and attributes.get("data-musmeta"):
            self.stream_entries.append((attributes["data-musid"], attributes["data-musmeta"]))
            match = re.fullmatch(r"track-id-([0-9]{1,12})", attributes["data-musid"])
            if match and match[1] not in self.song_ids:
                self.song_ids.append(match[1])
        if tag == "script" and dict(attrs).get("type") == "application/ld+json":
            self.active = True
            self.parts = []

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.active:
            self.blocks.append("".join(self.parts))
            self.active = False


def parse_song(html: str, song_id: str) -> tuple[str, str, float | None]:
    parser = _StructuredMetadata()
    parser.feed(html)
    for block in parser.blocks:
        try:
            item = json.loads(block)
        except ValueError:
            continue
        if (not isinstance(item, dict) or item.get("@type") != "MusicRecording"
                or item.get("@id") != f"https://ru.hitmox.org/song/{song_id}"):
            continue
        title = item.get("name")
        artists = item.get("byArtist")
        if not isinstance(title, str) or not title.strip() or len(title) > 500 or not isinstance(artists, list):
            break
        names = [artist.get("name") for artist in artists if isinstance(artist, dict)]
        if not names or any(not isinstance(name, str) or not name.strip() or len(name) > 200 for name in names):
            break
        duration = None
        value = item.get("duration", "")
        if isinstance(value, str) and len(value) < 60:
            match = re.fullmatch(r"P(?:(\d{1,3})D)?T(?:(\d{1,3})H)?(?:(\d{1,3})M)?(?:(\d{1,3})S)?", value)
            if match and any(match.groups()):
                d, h, m, s = (int(part or 0) for part in match.groups())
                duration = float(d * 86400 + h * 3600 + m * 60 + s)
        return title.strip(), ", ".join(name.strip() for name in names), duration
    raise SourceFailure("metadata_unavailable")


def parse_search_ids(html: str) -> list[str]:
    """Use the same document parser; metadata still comes from parse_song."""
    if len(html.encode("utf-8")) > 262144:
        raise SourceFailure("response_too_large", stage="source_search")
    parser = _StructuredMetadata()
    parser.feed(html)
    # One extra ID signals truncation: do not declare a unique winner then.
    return parser.song_ids[:MAX_SEARCH_CANDIDATES + 1]


def parse_search_metadata(html: str, song_ids: list[str]) -> dict[str, tuple[str, str | None, float | None]]:
    """Read recording metadata already embedded in the search page, when present."""
    parser = _StructuredMetadata()
    parser.feed(html)
    result = {}
    for song_id in song_ids:
        try:
            result[song_id] = parse_song(html, song_id)
            continue
        except SourceFailure:
            pass
        expected = f"track-id-{song_id}"
        for entry_id, raw in parser.stream_entries:
            if entry_id != expected:
                continue
            try:
                item = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(item, dict):
                continue
            title = item.get("name") or item.get("title")
            artist_value = item.get("byArtist") or item.get("artist") or item.get("artists")
            if isinstance(artist_value, dict):
                artists = [artist_value.get("name")]
            elif isinstance(artist_value, list):
                artists = [artist.get("name") if isinstance(artist, dict) else artist for artist in artist_value]
            else:
                artists = [artist_value]
            artist = ", ".join(value.strip() for value in artists
                               if isinstance(value, str) and value.strip())
            duration = item.get("duration_seconds")
            if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not 0 <= duration <= 86400:
                duration = None
                value = item.get("duration")
                if isinstance(value, str) and len(value) < 60:
                    match = re.fullmatch(r"PT(?:(\d{1,3})H)?(?:(\d{1,3})M)?(?:(\d{1,3})S)?", value)
                    if match and any(match.groups()):
                        h, m, s = (int(part or 0) for part in match.groups())
                        duration = float(h * 3600 + m * 60 + s)
            if isinstance(title, str) and title.strip() and len(title) <= 500 and artist and len(artist) <= 200:
                result[song_id] = (title.strip(), artist, duration)
                break
    return result


def stream_format(url: str) -> str:
    try:
        path = urlsplit(url).path
        if re.match(r"^/(?:s[1-9]/)?get/cuts/", path):
            return "cuts"
        if re.match(r"^/(?:s[1-9]/)?get/music/", path):
            return "music"
    except ValueError:
        pass
    return "unsupported"


def valid_stream_url(url: str, song_id: str) -> bool:
    """Check safe HTTPS URL syntax; provenance and public DNS are checked separately."""
    if not isinstance(url, str) or not re.fullmatch(r"[0-9]{1,12}", song_id):
        return False
    from musicstream.audio_providers.hitmos_stream import validated_audio_url
    return validated_audio_url(url)


def valid_hitmos_player_url(url: str, song_id: str) -> bool:
    """Accept only a safe player URL supplied by the requested Hitmos page."""
    if not valid_stream_url(url, song_id):
        return False
    try:
        parsed = urlsplit(url)
        return parsed.hostname == "ru.hitmox.org"
    except ValueError:
        return False


def inspect_player_url(html: str, song_id: str) -> tuple[str | None, ResolutionDiagnostics]:
    parser = _StructuredMetadata()
    parser.feed(html)
    expected = f"track-id-{song_id}"
    diagnostics = ResolutionDiagnostics()
    for entry_id, raw in parser.stream_entries:
        if entry_id != expected:
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("id") != expected or not isinstance(entry.get("url"), str):
            continue
        raw = entry["url"]
        diagnostics.player_format = stream_format(raw)
        diagnostics.player_relative = raw.startswith("/") and not raw.startswith("//")
        url = urljoin("https://ru.hitmox.org", raw) if diagnostics.player_relative else raw
        if valid_hitmos_player_url(url, song_id):
            diagnostics.player_valid = True
            return url, diagnostics
    return None, diagnostics


def parse_stream_url(html: str, song_id: str) -> str | None:
    return inspect_player_url(html, song_id)[0]


class HitmosProvider(AudioProvider):
    def __init__(self, reader=None, *, diagnostics_enabled=False, lab_mode=False, stream_proxy=None, track_credits=None):
        self.reader = reader
        self.diagnostics_enabled = diagnostics_enabled
        self.lab_mode = bool(lab_mode)
        self.stream_proxy = stream_proxy
        self.track_credits = track_credits
        self._search_gate = asyncio.Semaphore(1)
        self._search_cache = OrderedDict()
        self._prepared_cache = OrderedDict()
        self._clock = time.monotonic

    async def find(self, recording: RecordingReference) -> SourceResult:
        batch = await self.discover(recording)
        result = rank_batch(recording, batch)
        candidate = next((item for item in result.candidates if item.id == batch.auto_candidate_id), None)
        if (candidate and candidate.match_score is not None and candidate.match_score >= .90
                and exact_metadata(recording, candidate)
                and versions(f"{candidate.title} {candidate.artist or ''}")
                == versions(f"{recording.title} {recording.artist}")):
            candidate.selection_reason = "unique_high_confidence"
            result.selection = "automatic"
            result.selected_id = candidate.id
            result.selected_source = "hitmos"
            result.reason = "unique_high_confidence"
        return result

    async def prepare(self, recording: RecordingReference, candidate_id: str) -> SourceResult:
        key = self._prepared_key(recording, candidate_id)
        now = self._clock()
        for expired in [key for key, (until, _) in self._prepared_cache.items() if until <= now]:
            del self._prepared_cache[expired]
        cached = self._prepared_cache.get(key)
        if cached:
            self._prepared_cache.move_to_end(key)
            return cached[1].model_copy(deep=True, update={"diagnostics": None, "reused_cache": True})
        result = await self.lookup(recording, candidate_id)
        if result.reason != "request_budget_exhausted":
            self._cache_prepared(recording, candidate_id, result,
                                 45 if result.status == "available" else 15)
        return result

    @staticmethod
    def _prepared_key(recording: RecordingReference, candidate_id: str):
        return (recording.title, recording.artist, recording.duration_seconds,
                recording.explicit_lyrics, candidate_id)

    def _cache_prepared(self, recording: RecordingReference, candidate_id: str,
                        result: SourceResult, ttl: int = 45) -> None:
        self._prepared_cache[self._prepared_key(recording, candidate_id)] = (
            self._clock() + ttl, result.model_copy(deep=True))
        self._prepared_cache.move_to_end(self._prepared_key(recording, candidate_id))
        while len(self._prepared_cache) > 64:
            self._prepared_cache.popitem(last=False)

    async def _enrich_missing_durations(self, recording: RecordingReference,
                                        recordings: list[DiscoveredRecording], diagnostics=None
                                        ) -> tuple[list[DiscoveredRecording], str | None, bool, int | None]:
        enrichment_started = time.monotonic()
        request_count = 0
        request_ids = []
        metadata_lookup_ms = 0
        stream_preparation_ms = 0
        browser_profile_fields = (
            "browser_context_create_elapsed_ms", "browser_page_create_elapsed_ms",
            "domcontentloaded_elapsed_ms", "document_body_elapsed_ms",
            "jsonld_extraction_elapsed_ms", "player_json_extraction_elapsed_ms",
            "player_redirect_elapsed_ms",
            "browser_request_count", "browser_navigation_count",
        )
        browser_profile_totals = {name: 0 for name in browser_profile_fields}
        if recording.duration_seconds is None:
            if diagnostics is not None:
                diagnostics.metadata_enrichment_elapsed_ms = 0
                diagnostics.metadata_enrichment_requests = 0
                diagnostics.metadata_enrichment_ids = []
                diagnostics.metadata_lookup_elapsed_ms = 0
                diagnostics.stream_preparation_elapsed_ms = 0
            return recordings, None, False, None

        ranked = rank_batch(recording, DiscoveryBatch(
            source="hitmos", status="ok", reason="candidates_found", message="",
            search_url="https://ru.hitmox.org/search?" + urlencode({"q": f"{recording.artist} {recording.title}"}),
            recordings=recordings,
        )).candidates
        # Keep the ranker's stable source order for ties. A Hitmos ID is not
        # evidence of match quality.
        ranked.sort(key=lambda candidate: (
            -(candidate.match_score or 0),
            {"likely": 0, "uncertain": 1, "mismatch": 2}[candidate.match.status],
        ))
        wanted_title, wanted_artist = normalize(recording.title), normalize(recording.artist)
        # A tied group with missing durations needs comparison before selection.
        require_unique = sum(hitmos_missing_duration_match(recording, candidate) for candidate in ranked) > 1
        by_id = {item.id: item for item in recordings}
        selected_id = None
        budget_wait = None
        for ranked_candidate in ranked:
            if (ranked_candidate.match_score is None or ranked_candidate.match_score < .90
                    or normalize(ranked_candidate.title) != wanted_title
                    or normalize(ranked_candidate.artist or "") != wanted_artist
                    or versions(f"{ranked_candidate.title} {ranked_candidate.artist or ''}")
                    != versions(f"{recording.title} {recording.artist}")):
                continue

            candidate = ranked_candidate
            if candidate.duration_seconds is None:
                if (self.reader is None or not hasattr(self.reader, "read_song")
                        or request_count >= MAX_METADATA_ENRICHMENTS):
                    continue
                request_started = time.monotonic()
                try:
                    request_count += 1
                    request_ids.append(candidate.id)
                    # Read metadata and prepare the stream in one Browser Reader
                    # request. A successful result is cached for the player's
                    # follow-up resolve, so the detail page is not opened twice.
                    async with asyncio.timeout(STREAM_PREPARATION_ENRICHMENT_TIMEOUT_SECONDS):
                        checked = await self.prepare(recording, candidate.id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    continue
                finally:
                    stream_preparation_ms += round((time.monotonic() - request_started) * 1000)
                if checked.reason == "request_budget_exhausted":
                    budget_wait = checked.retry_after_seconds
                    break
                checked_candidate = next((item for item in checked.candidates if item.id == candidate.id), None)
                if checked_candidate is None:
                    continue
                checked_diagnostics = checked.diagnostics
                if checked_diagnostics is not None:
                    for name in browser_profile_fields:
                        value = getattr(checked_diagnostics, name, None)
                        if isinstance(value, int):
                            browser_profile_totals[name] += value
                original = by_id[candidate.id]
                by_id[candidate.id] = original.model_copy(update={
                    "title": checked_candidate.title,
                    "artist": checked_candidate.artist,
                    "duration_seconds": checked_candidate.duration_seconds,
                })
                candidate = rank_batch(recording, DiscoveryBatch(
                    source="hitmos", status="ok", reason="candidate_checked", message="",
                    search_url="https://ru.hitmox.org/song/" + candidate.id,
                    recordings=[by_id[candidate.id]],
                )).candidates[0]
                if (checked.status != "available" or checked_candidate.method != "html_audio"
                        or not checked_candidate.stream_url):
                    continue
            elif require_unique or candidate.match.status != "likely":
                continue

            if (not require_unique and candidate.match_score is not None and candidate.match_score >= .90
                    and candidate.match.status == "likely"
                    and normalize(candidate.title) == wanted_title
                    and normalize(candidate.artist or "") == wanted_artist
                    and versions(f"{candidate.title} {candidate.artist or ''}")
                    == versions(f"{recording.title} {recording.artist}")):
                selected_id = candidate.id
                break
        if diagnostics is not None:
            for name, value in browser_profile_totals.items():
                if value:
                    setattr(diagnostics, name, (getattr(diagnostics, name, None) or 0) + value)
            diagnostics.metadata_enrichment_elapsed_ms = round((time.monotonic() - enrichment_started) * 1000)
            diagnostics.metadata_enrichment_requests = request_count
            diagnostics.metadata_enrichment_ids = request_ids
            diagnostics.metadata_lookup_elapsed_ms = metadata_lookup_ms
            diagnostics.stream_preparation_elapsed_ms = stream_preparation_ms
        return [by_id[item.id] for item in recordings], selected_id, require_unique, budget_wait

    @staticmethod
    def _search_recordings(document, recording):
        html = document.html if isinstance(document, SourceDocument) else document
        ids = parse_search_ids(html)
        metadata = parse_search_metadata(html, ids)
        recordings = []
        for song_id in ids[:MAX_SEARCH_CANDIDATES]:
            title, artist, duration = metadata.get(song_id, (recording.title, None, None))
            recordings.append(DiscoveredRecording(id=song_id, title=title, artist=artist,
                duration_seconds=duration, method="external_link",
                external_url=f"https://ru.hitmox.org/song/{song_id}"))
        return recordings, len(ids) <= MAX_SEARCH_CANDIDATES

    async def _with_track_credits(self, recording):
        if not recording.deezer_id or self.track_credits is None or recording.duration_seconds is None:
            return recording
        try:
            async with asyncio.timeout(3):
                title, primary_artist, duration, credits = await self.track_credits(recording.deezer_id)
        except (ProviderError, TimeoutError):
            return recording
        primary = normalize(recording.artist)
        if (normalize(title or "") != normalize(recording.title) or normalize(primary_artist or "") != primary
                or duration is None or abs(duration - recording.duration_seconds) > max(5, recording.duration_seconds * .03)):
            return recording
        names = {}
        for credit in credits:
            name = normalize(credit)
            if name and "," not in credit:
                names.setdefault(name, credit.strip())
        if primary not in names or len(names) < 2:
            return recording
        artist = ", ".join([names.pop(primary), *names.values()])
        return recording.model_copy(update={"artist": artist}) if len(artist) <= 200 else recording

    async def _search_by_coartist(self, recording, primary_recordings):
        wanted_title, wanted_artist = normalize(recording.title), normalize(recording.artist)
        if any(normalize(item.title) == wanted_title
               and normalize(item.artist or "") in {"", wanted_artist}
               and assess(recording, item.title, item.artist, item.duration_seconds).status != "mismatch"
               for item in primary_recordings):
            return None

        if "," not in recording.artist:
            recording = await self._with_track_credits(recording)
        artists = [name.strip() for name in recording.artist.split(",")]
        if len(artists) < 2 or not all(artists) or len({normalize(name) for name in artists}) < 2:
            return None
        wanted_artist = normalize(recording.artist)
        coartist = artists[-1]
        query = f"{coartist} {recording.title}"
        try:
            document = await self.reader.read_search(query)
            recordings, complete = self._search_recordings(document, recording)
        except SourceFailure as exc:
            if exc.reason == "request_budget_exhausted":
                raise
            return None
        candidates = [item for item in recordings if normalize(item.title) == wanted_title
                      and normalize(coartist) in {normalize(name) for name in (item.artist or "").split(",")}
                      and assess(recording, item.title, item.artist, item.duration_seconds).status != "mismatch"]
        if not candidates:
            return None
        # Search credits may use a stage name. Only the full song page can
        # confirm the original artist list before automatic playback.
        if len(candidates) == 1 and complete:
            candidate = candidates[0]
            if not hasattr(self.reader, "read_song"):
                return None
            try:
                async with asyncio.timeout(STREAM_PREPARATION_ENRICHMENT_TIMEOUT_SECONDS):
                    prepared = await self.prepare(recording, candidate.id)
            except TimeoutError:
                return None
            if prepared.reason == "request_budget_exhausted":
                raise SourceFailure(prepared.reason, retry_after_seconds=prepared.retry_after_seconds)
            verified = next((item for item in prepared.candidates if item.id == candidate.id), None)
            if not verified:
                return None
            exact_names = (normalize(verified.title) == wanted_title
                           and normalize(verified.artist or "") == wanted_artist)
            explicit_unknown = (exact_names and verified.match.explicit_status == "unknown"
                                and verified.match.reasons == ["explicit_unconfirmed"])
            explicit_conflict = (verified.match.explicit_status == "conflicting"
                                 and normalize(verified.title).removesuffix(" clean") == wanted_title
                                 and normalize(verified.artist or "") == wanted_artist
                                 and "duration_differs" not in verified.match.reasons
                                 and "duration_unknown" not in verified.match.reasons)
            if not (exact_metadata(recording, verified) or explicit_unknown or explicit_conflict):
                return None
            candidates = [candidate.model_copy(update={
                "title": verified.title, "artist": verified.artist, "duration_seconds": verified.duration_seconds,
            })]
        return candidates, complete, document, query, recording.artist

    async def discover(self, recording: RecordingReference) -> DiscoveryBatch:
        fallback = DiscoveryBatch(
            source="hitmos", status="blocked", reason="manual_search_required",
            message="Автоматический поиск Hitmos не настроен. Можно открыть поиск на сайте; ручная проверка находится в разделе «Дополнительно».",
            search_url="https://ru.hitmox.org/search?" + urlencode({"q": f"{recording.artist} {recording.title}"}),
        )
        if self.reader is None or not hasattr(self.reader, "read_search"):
            return fallback
        key = (recording.title, recording.artist, recording.duration_seconds, recording.explicit_lyrics,
               recording.deezer_id)
        async with self._search_gate:
            now = self._clock()
            for expired in [key for key, (until, _, _) in self._search_cache.items() if until <= now]:
                del self._search_cache[expired]
            cached = self._search_cache.get(key)
            if cached and (cached[2] is None or cached[2] > now):
                _, batch, retry_at = cached
                remaining = math.ceil(retry_at - now) if retry_at is not None else None
                return batch.model_copy(deep=True, update={
                    "diagnostics": None, "reused_cache": True, "retry_after_seconds": remaining})
            try:
                started_at = time.monotonic()
                document = None
                if cached:
                    # The search itself succeeded. Spend the next slot on the
                    # pending enrichment instead of fetching the same page again.
                    previous = cached[1]
                    recordings, complete = previous.recordings, previous.complete
                else:
                    document = await self.reader.read_search(f"{recording.artist} {recording.title}")
                    extraction_started = time.monotonic()
                    recordings, complete = self._search_recordings(document, recording)
                if isinstance(document, SourceDocument) and document.diagnostics is not None:
                    document.diagnostics.search_elapsed_ms = document.diagnostics.elapsed_ms
                    document.diagnostics.search_extraction_elapsed_ms = round(
                        (time.monotonic() - extraction_started) * 1000)
                recordings, auto_candidate_id, require_unique, budget_wait = await self._enrich_missing_durations(
                    recording, recordings, document.diagnostics if isinstance(document, SourceDocument) else None)
                if cached and cached[1].require_unique_hitmos_match:
                    auto_candidate_id, require_unique = None, True
                search_url = fallback.search_url
                manual_selection = False
                hitmos_artist = None
                try:
                    alternate = await self._search_by_coartist(recording, recordings) if budget_wait is None else None
                except SourceFailure as exc:
                    if exc.reason != "request_budget_exhausted":
                        raise
                    budget_wait, alternate = exc.retry_after_seconds, None
                if alternate is not None:
                    recordings, complete, alternate_document, query, hitmos_artist = alternate
                    search_url = "https://ru.hitmox.org/search?" + urlencode({"q": query})
                    auto_candidate_id, require_unique = None, False
                    manual_selection = len(recordings) > 1 or not complete
                    if (isinstance(document, SourceDocument) and document.diagnostics is not None
                            and isinstance(alternate_document, SourceDocument) and alternate_document.diagnostics is not None):
                        document.diagnostics.search_elapsed_ms = sum(
                            value or 0 for value in (document.diagnostics.search_elapsed_ms,
                                                     alternate_document.diagnostics.elapsed_ms))
                if isinstance(document, SourceDocument) and document.diagnostics is not None:
                    document.diagnostics.elapsed_ms = round((time.monotonic() - started_at) * 1000)
                result = DiscoveryBatch(source="hitmos", status="error" if budget_wait is not None and not recordings else "ok",
                    reason="request_budget_exhausted" if budget_wait is not None else "candidates_found" if recordings else "no_results",
                    message=HITMOS_BUDGET_MESSAGE if budget_wait is not None else
                            "Метаданные найдены; поток будет проверен по Play." if recordings else "Hitmos не вернул кандидатов.",
                    search_url=search_url, recordings=recordings,
                    complete=complete, auto_candidate_id=auto_candidate_id,
                    require_unique_hitmos_match=require_unique,
                    hitmos_manual_selection=manual_selection,
                    hitmos_artist=hitmos_artist,
                    retry_after_seconds=budget_wait,
                    reused_cache=cached is not None,
                    diagnostics=(document.diagnostics if self.diagnostics_enabled
                                 and isinstance(document, SourceDocument) else None))
            except SourceFailure as exc:
                diag = exc.diagnostics
                if diag is not None:
                    diag.stage = exc.stage or "source_search"
                    diag.rejected_by = exc.owner
                    diag.http_status = exc.http_status
                    diag.retry_after_seconds = exc.retry_after_seconds
                    diag.internal_code = exc.reason
                result = fallback.model_copy(update={"reason": exc.reason,
                    "status": "error",
                    "message": HITMOS_BUDGET_MESSAGE if exc.reason == "request_budget_exhausted" else
                               "Поиск Hitmos недоступен. Можно открыть исходный сайт или повторить позже.",
                    "retry_after_seconds": exc.retry_after_seconds if exc.reason == "request_budget_exhausted" else None,
                    "diagnostics": diag if self.diagnostics_enabled else None})
            if result.reason != "request_budget_exhausted" or result.recordings:
                now = self._clock()
                retry_at = now + result.retry_after_seconds if result.retry_after_seconds is not None else None
                ttl = 30 if result.status == "error" else 60
                self._search_cache[key] = ((retry_at or now) + ttl, result.model_copy(deep=True), retry_at)
            while len(self._search_cache) > 64:
                self._search_cache.popitem(last=False)
            return result


    async def lookup(self, recording: RecordingReference, song_id: str, *, metadata_only=False) -> SourceResult:
        if not re.fullmatch(r"[0-9]{1,12}", song_id):
            raise SourceFailure("invalid_song_id")
        url = f"https://ru.hitmox.org/song/{song_id}"
        if self.reader is None:
            return SourceResult(source="hitmos", status="external_player", reason="browser_not_configured",
                                stage="stream_resolution",
                                message="Проверка метаданных Hitmos через Chrome не включена на backend. Можно открыть выбранную запись на Hitmos.", search_url=url)
        document = None
        try:
            document = await self.reader.read_song(song_id, metadata_only=True) if metadata_only else await self.reader.read_song(song_id)
            html = document.html if isinstance(document, SourceDocument) else document
            if isinstance(document, SourceDocument) and document.diagnostics is not None:
                document.diagnostics.network_route = ("local_http_proxy"
                    if self.lab_mode and self.stream_proxy and self.stream_proxy.proxy_url else
                    "local_http_proxy" if getattr(self.reader, "proxy_url", None) else "direct")
            if len(html.encode("utf-8")) > 262144:
                raise SourceFailure("response_too_large")
            extraction_started = time.monotonic()
            title, artist, duration = parse_song(html, song_id)
            if isinstance(document, SourceDocument) and document.diagnostics is not None:
                document.diagnostics.jsonld_extraction_elapsed_ms = round(
                    (time.monotonic() - extraction_started) * 1000)
            player_parse_started = time.monotonic()
            stream_url, _ = inspect_player_url(html, song_id)
            if isinstance(document, SourceDocument) and document.diagnostics is not None:
                document.diagnostics.player_json_extraction_elapsed_ms = round(
                    (time.monotonic() - player_parse_started) * 1000)
            if isinstance(document, SourceDocument):
                # Only the Browser Reader can supply this URL after observing
                # the requested Hitmos page's player response and redirect.
                stream_url = document.stream_url if not document.playback_reason else None
                if not (stream_url and valid_stream_url(stream_url, song_id)):
                    stream_url = None
            match = assess_hitmos(recording, title, artist, duration)
            if metadata_only:
                return SourceResult(source="hitmos", status="external_player", reason="metadata_checked",
                    stage="matching", message="Метаданные проверены; доступность потока будет проверена по Play.",
                    search_url=url, candidates=[AudioCandidate(id=song_id, title=title, artist=artist,
                        duration_seconds=duration, match=match, method="external_link", external_url=url)])
            direct = bool(stream_url) and match.status != "mismatch"
            if direct and self.stream_proxy is not None:
                # Keep the HTMLAudioElement on a same-origin URL. Its Range
                # requests follow a bounded, DNS-pinned and audio-checked chain.
                validated = await self.stream_proxy.register(stream_url)
                stream_url = f"/api/audio/hitmos-stream/{validated.token}"
                if isinstance(document, SourceDocument) and document.diagnostics is not None:
                    document.diagnostics.stage = "stream_resolution"
                    if self.lab_mode:
                        document.diagnostics.redirect_host = validated.host
                    document.diagnostics.path_type = validated.path_type
            candidate = AudioCandidate(id=song_id, title=title, artist=artist, duration_seconds=duration,
                                       match=match, method="html_audio" if direct else "external_link",
                                       external_url=url, stream_url=stream_url if direct else None)
            reason = "public_stream_found" if direct else "source_site_player"
            stage = "stream_resolution"
            message = "Адрес потока получен; загрузку CDN и воспроизведение ещё должен подтвердить браузер."
            if not direct and isinstance(document, SourceDocument) and document.playback_reason:
                reason = document.playback_reason
            if match.status == "mismatch":
                stage, reason = "matching", "recording_mismatch"
                message = "Запись отклонена сопоставлением. Причины и метаданные обеих записей показаны ниже."
            elif not stream_url:
                message = {
                    "stream_redirect_not_allowed": "Метаданные сопоставлены. Адрес потока отклонён проверкой CDN или формата; это не ошибка совпадения.",
                    "stream_redirect_unavailable": "Метаданные сопоставлены, но источник не выдал адрес CDN вовремя.",
                    "stream_response_unexpected": "Метаданные сопоставлены, но источник вернул неожиданный ответ вместо адреса CDN.",
                    "stream_not_found": "Метаданные сопоставлены, но адрес плеера отсутствует или имеет неподдерживаемый формат.",
                }.get(reason, "Метаданные сопоставлены, но получить подходящий адрес аудио не удалось. Можно открыть Hitmos.")
            return SourceResult(source="hitmos", status="available" if direct else "external_player",
                                reason=reason, stage=stage, message=message,
                                search_url=url, candidates=[candidate],
                                diagnostics=document.diagnostics if self.diagnostics_enabled and isinstance(document, SourceDocument) else None)
        except SourceFailure as exc:
            diag = exc.diagnostics or (document.diagnostics if isinstance(document, SourceDocument) else None)
            if diag is not None:
                if self.lab_mode and self.stream_proxy and self.stream_proxy.proxy_url:
                    diag.network_route = "local_http_proxy"
                diag.stage = exc.stage or "stream_resolution"
                diag.rejected_by = exc.owner
                diag.http_status = exc.http_status
                diag.retry_after_seconds = exc.retry_after_seconds
                diag.internal_code = exc.reason
                diag.range_status = exc.http_status if exc.stage == "http_range" else None
                diag.redirect_host = ("host_" + hashlib.sha256(exc.host.lower().encode()).hexdigest()[:12]
                                      if self.lab_mode and exc.host else exc.host if exc.host and re.fullmatch(r"[a-z0-9.-]{1,253}", exc.host) else diag.redirect_host)
                diag.path_type = exc.path_type
                if exc.path_type and diag.stage == "stream_resolution":
                    diag.stage = "redirect"
            return SourceResult(source="hitmos", status="error", reason=exc.reason,
                                stage=exc.stage or "source_metadata",
                                message=HITMOS_BUDGET_MESSAGE if exc.reason == "request_budget_exhausted" else
                                        "Не удалось получить или прочитать метаданные Hitmos. Сопоставление и воспроизведение ещё не подтверждены. Можно открыть исходную запись.",
                                search_url=url,
                                retry_after_seconds=exc.retry_after_seconds if exc.reason == "request_budget_exhausted" else None,
                                diagnostics=diag if self.diagnostics_enabled else None)
