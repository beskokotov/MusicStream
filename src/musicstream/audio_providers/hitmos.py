import json
import asyncio
import hashlib
import re
import time
from collections import OrderedDict
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit

from musicstream.audio_providers.base import AudioCandidate, AudioProvider, DiscoveredRecording, DiscoveryBatch, RecordingReference, ResolutionDiagnostics, SourceDocument, SourceResult
from musicstream.audio_providers.matching import assess
from musicstream.audio_providers.selection import rank_batch
from musicstream.audio_providers.transport import SourceFailure

MAX_SEARCH_CANDIDATES = 4


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


STREAM_PREFIXES = {
    "ru.hitmox.org": "/get",
    "cdn13.deliciousoranges.com": "/s1/get",
    "cdn14.deliciousoranges.com": "/s1/get",
    "cdn11.deliciousoranges.com": "/s1/get",
    "cdn7.deliciousoranges.com": "/s2/get",
    "s4.deliciouspeaches.com": "/get",
}
CDN_HOSTS = frozenset(STREAM_PREFIXES) - {"ru.hitmox.org"}


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
    if not isinstance(url, str) or not re.fullmatch(r"[0-9]{1,12}", song_id):
        return False
    if url != url.strip() or any(ord(char) < 32 for char in url):
        return False
    try:
        parsed = urlsplit(url)
        prefix = STREAM_PREFIXES.get(parsed.hostname)
        return (bool(prefix) and parsed.scheme == "https"
                and parsed.port in (None, 443) and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment
                and bool(re.fullmatch(
                    rf"{prefix}/(?:cuts/[a-f0-9]{{2}}/[a-f0-9]{{2}}/[a-f0-9]{{32}}/{song_id}/[A-Za-z0-9_-]+|music/[0-9]{{8}}/[A-Za-z0-9_-]+_{song_id})\.mp3",
                    parsed.path)))
    except ValueError:
        return False


def inspect_player_url(html: str, song_id: str, *, lab_mode=False) -> tuple[str | None, ResolutionDiagnostics]:
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
        if valid_stream_url(url, song_id) or (lab_mode and _lab_url_valid(url)):
            diagnostics.player_valid = True
            return url, diagnostics
    return None, diagnostics


def _lab_url_valid(url: str) -> bool:
    # Import locally to avoid changing strict-mode startup behavior.
    from musicstream.audio_providers.hitmos_stream import validated_audio_url
    return validated_audio_url(url)


def parse_stream_url(html: str, song_id: str) -> str | None:
    return inspect_player_url(html, song_id)[0]


class HitmosProvider(AudioProvider):
    def __init__(self, reader=None, *, diagnostics_enabled=False, lab_mode=False, stream_proxy=None):
        self.reader = reader
        self.diagnostics_enabled = diagnostics_enabled
        self.lab_mode = bool(lab_mode)
        self.stream_proxy = stream_proxy
        self._search_gate = asyncio.Semaphore(1)
        self._search_cache = OrderedDict()
        self._prepared_cache = OrderedDict()
        self._clock = time.monotonic

    async def find(self, recording: RecordingReference) -> SourceResult:
        return rank_batch(recording, await self.discover(recording))

    async def prepare(self, recording: RecordingReference, candidate_id: str) -> SourceResult:
        key = self._prepared_key(recording, candidate_id)
        now = self._clock()
        for expired in [key for key, (until, _) in self._prepared_cache.items() if until <= now]:
            del self._prepared_cache[expired]
        cached = self._prepared_cache.get(key)
        if cached:
            self._prepared_cache.move_to_end(key)
            return cached[1].model_copy(deep=True)
        result = await self.lookup(recording, candidate_id)
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

    async def discover(self, recording: RecordingReference) -> DiscoveryBatch:
        fallback = DiscoveryBatch(
            source="hitmos", status="blocked", reason="manual_search_required",
            message="Автоматический поиск Hitmos не настроен. Можно открыть поиск на сайте; ручная проверка находится в разделе «Дополнительно».",
            search_url="https://ru.hitmox.org/search?" + urlencode({"q": f"{recording.artist} {recording.title}"}),
        )
        if self.reader is None or not hasattr(self.reader, "read_search"):
            return fallback
        key = (recording.title, recording.artist, recording.duration_seconds, recording.explicit_lyrics)
        async with self._search_gate:
            now = self._clock()
            for expired in [key for key, (until, _) in self._search_cache.items() if until <= now]:
                del self._search_cache[expired]
            cached = self._search_cache.get(key)
            if cached:
                return cached[1].model_copy(deep=True)
            try:
                document = await self.reader.read_search(f"{recording.artist} {recording.title}")
                html = document.html if isinstance(document, SourceDocument) else document
                ids = parse_search_ids(html)
                search_metadata = parse_search_metadata(html, ids)
                recordings = []
                failures = 0
                for song_id in ids[:MAX_SEARCH_CANDIDATES]:
                    title, artist, duration = search_metadata.get(song_id, (recording.title, None, None))
                    recordings.append(DiscoveredRecording(id=song_id, title=title, artist=artist,
                        duration_seconds=duration, method="external_link",
                        external_url=f"https://ru.hitmox.org/song/{song_id}"))
                result = DiscoveryBatch(source="hitmos", status="ok",
                    reason="candidates_found" if recordings else "no_results",
                    message="Метаданные найдены; поток будет проверен по Play." if recordings else
                            "Не удалось проверить метаданные найденных записей." if failures else "Hitmos не вернул кандидатов.",
                    search_url=fallback.search_url, recordings=recordings,
                    complete=(len(ids) <= MAX_SEARCH_CANDIDATES))
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
                    "message": "Поиск Hitmos недоступен. Можно открыть исходный сайт или повторить позже.",
                    "diagnostics": diag if self.diagnostics_enabled else None})
            self._search_cache[key] = (self._clock() + (30 if result.status == "error" else 60), result.model_copy(deep=True))
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
            title, artist, duration = parse_song(html, song_id)
            stream_url, _ = inspect_player_url(html, song_id, lab_mode=self.lab_mode)
            if isinstance(document, SourceDocument):
                # Never publish the session-dependent first hop after a failed
                # resolution, and never accept a redirect not on the CDN allowlist.
                stream_url = document.stream_url
                strict_ok = bool(stream_url and valid_stream_url(stream_url, song_id)
                                 and urlsplit(stream_url).hostname in CDN_HOSTS)
                lab_ok = bool(self.lab_mode and stream_url and _lab_url_valid(stream_url))
                if not (strict_ok or lab_ok):
                    stream_url = None
            match = assess(recording, title, artist, duration)
            if metadata_only:
                return SourceResult(source="hitmos", status="external_player", reason="metadata_checked",
                    stage="matching", message="Метаданные проверены; доступность потока будет проверена по Play.",
                    search_url=url, candidates=[AudioCandidate(id=song_id, title=title, artist=artist,
                        duration_seconds=duration, match=match, method="external_link", external_url=url)])
            direct = bool(stream_url) and match.status != "mismatch"
            if direct and self.lab_mode:
                if self.stream_proxy is None:
                    raise SourceFailure("lab_proxy_unavailable", stage="stream_resolution", owner="local_policy")
                validated = await self.stream_proxy.probe_and_register(stream_url)
                stream_url = f"/api/audio/hitmos-stream/{validated.token}"
                if isinstance(document, SourceDocument) and document.diagnostics is not None:
                    document.diagnostics.stage = "http_range"
                    document.diagnostics.range_status = 206
                    document.diagnostics.content_type = validated.content_type
                    document.diagnostics.audio_signature_valid = True
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
                                message="Не удалось получить или прочитать метаданные Hitmos. Сопоставление и воспроизведение ещё не подтверждены. Можно открыть исходную запись.", search_url=url,
                                diagnostics=diag if self.diagnostics_enabled else None)
