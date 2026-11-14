import json
import asyncio
import re
import time
from collections import OrderedDict
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit

from musicstream.audio_providers.base import AudioCandidate, AudioProvider, DiscoveredRecording, DiscoveryBatch, RecordingReference, ResolutionDiagnostics, SourceDocument, SourceResult
from musicstream.audio_providers.matching import assess
from musicstream.audio_providers.selection import rank_batch
from musicstream.audio_providers.transport import SourceFailure


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
    return parser.song_ids[:4]


STREAM_PREFIXES = {
    "ru.hitmox.org": "/get",
    "cdn13.deliciousoranges.com": "/s1/get",
    "cdn14.deliciousoranges.com": "/s1/get",
    "cdn11.deliciousoranges.com": "/s1/get",
    "s4.deliciouspeaches.com": "/get",
}
CDN_HOSTS = frozenset(STREAM_PREFIXES) - {"ru.hitmox.org"}


def stream_format(url: str) -> str:
    try:
        path = urlsplit(url).path
        if re.match(r"^/(?:s1/)?get/cuts/", path):
            return "cuts"
        if re.match(r"^/(?:s1/)?get/music/", path):
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
        if valid_stream_url(url, song_id):
            diagnostics.player_valid = True
            return url, diagnostics
    return None, diagnostics


def parse_stream_url(html: str, song_id: str) -> str | None:
    return inspect_player_url(html, song_id)[0]


class HitmosProvider(AudioProvider):
    def __init__(self, reader=None, *, diagnostics_enabled=False):
        self.reader = reader
        self.diagnostics_enabled = diagnostics_enabled
        self._search_gate = asyncio.Semaphore(1)
        self._search_cache = OrderedDict()
        self._clock = time.monotonic

    async def find(self, recording: RecordingReference) -> SourceResult:
        return rank_batch(recording, await self.discover(recording))

    async def prepare(self, recording: RecordingReference, candidate_id: str) -> SourceResult:
        return await self.lookup(recording, candidate_id)

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
                recordings = []
                failures = 0
                exact_match = False
                for song_id in ids[:3]:
                    try:
                        document = await self.reader.read_song(song_id, metadata_only=True)
                        html = document.html if isinstance(document, SourceDocument) else document
                        if len(html.encode("utf-8")) > 262144:
                            raise SourceFailure("response_too_large")
                        title, artist, duration = parse_song(html, song_id)
                        candidate = DiscoveredRecording(id=song_id, title=title, artist=artist,
                            duration_seconds=duration, method="external_link",
                            external_url=f"https://ru.hitmox.org/song/{song_id}")
                        if assess(recording, title, artist, duration).status == "likely":
                            # Search results are relevance ordered. The first
                            # exact title/artist/duration match is sufficient;
                            # checking duplicate IDs adds latency without improving
                            # the selected recording.
                            recordings = [candidate]
                            exact_match = True
                            break
                        recordings.append(candidate)
                    except SourceFailure:
                        failures += 1
                result = DiscoveryBatch(source="hitmos", status="error" if failures and not recordings else "ok",
                    reason="candidates_found" if recordings else "candidate_metadata_failed" if failures else "no_results",
                    message="Метаданные найдены; поток будет проверен по Play." if recordings else
                            "Не удалось проверить метаданные найденных записей." if failures else "Hitmos не вернул кандидатов.",
                    search_url=fallback.search_url, recordings=recordings,
                    complete=exact_match or (not failures and len(ids) <= 3))
            except SourceFailure as exc:
                result = fallback.model_copy(update={"reason": exc.reason,
                    "status": "error",
                    "message": "Поиск Hitmos недоступен. Можно открыть исходный сайт или повторить позже.",
                    "diagnostics": exc.diagnostics if self.diagnostics_enabled else None})
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
            if len(html.encode("utf-8")) > 262144:
                raise SourceFailure("response_too_large")
            title, artist, duration = parse_song(html, song_id)
            stream_url = parse_stream_url(html, song_id)
            if isinstance(document, SourceDocument):
                # Never publish the session-dependent first hop after a failed
                # resolution, and never accept a redirect not on the CDN allowlist.
                stream_url = document.stream_url
                if (not stream_url or not valid_stream_url(stream_url, song_id)
                        or urlsplit(stream_url).hostname not in CDN_HOSTS):
                    stream_url = None
            match = assess(recording, title, artist, duration)
            if metadata_only:
                return SourceResult(source="hitmos", status="external_player", reason="metadata_checked",
                    stage="matching", message="Метаданные проверены; доступность потока будет проверена по Play.",
                    search_url=url, candidates=[AudioCandidate(id=song_id, title=title, artist=artist,
                        duration_seconds=duration, match=match, method="external_link", external_url=url)])
            direct = bool(stream_url) and match.status != "mismatch"
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
            return SourceResult(source="hitmos", status="error", reason=exc.reason,
                                stage=exc.stage or "source_metadata",
                                message="Не удалось получить или прочитать метаданные Hitmos. Сопоставление и воспроизведение ещё не подтверждены. Можно открыть исходную запись.", search_url=url,
                                diagnostics=(exc.diagnostics or (document.diagnostics if isinstance(document, SourceDocument) else None)) if self.diagnostics_enabled else None)
