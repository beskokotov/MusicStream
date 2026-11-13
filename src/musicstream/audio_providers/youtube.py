import html
import re
from urllib.parse import urlencode

from pydantic import BaseModel, Field, ValidationError

from musicstream.audio_providers.base import AudioProvider, DiscoveredRecording, DiscoveryBatch, RecordingReference, SourceResult
from musicstream.audio_providers.selection import rank_batch
from musicstream.audio_providers.transport import BoundedSourceClient, SourceFailure

VIDEO_ID = r"^[a-zA-Z0-9_-]{11}$"


class _VideoId(BaseModel):
    videoId: str = Field(pattern=VIDEO_ID)


class _SearchItem(BaseModel):
    id: _VideoId


class _Search(BaseModel):
    items: list[_SearchItem]


class _Snippet(BaseModel):
    title: str = Field(max_length=500)
    channelTitle: str | None = Field(default=None, max_length=500)
    liveBroadcastContent: str = "none"


class _Details(BaseModel):
    duration: str


class _Status(BaseModel):
    embeddable: bool
    privacyStatus: str


class _Video(BaseModel):
    id: str = Field(pattern=VIDEO_ID)
    snippet: _Snippet
    contentDetails: _Details
    status: _Status


class _Videos(BaseModel):
    items: list[_Video]


def duration_seconds(value: str) -> float | None:
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value)
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return float(hours * 3600 + minutes * 60 + seconds)


class YouTubeProvider(AudioProvider):
    def __init__(self, http: BoundedSourceClient, api_key: str | None):
        self.http = http
        self.api_key = api_key

    async def find(self, recording: RecordingReference) -> SourceResult:
        return rank_batch(recording, await self.discover(recording))

    async def discover(self, recording: RecordingReference) -> DiscoveryBatch:
        search_url = "https://www.youtube.com/results?" + urlencode({"search_query": f"{recording.artist} {recording.title}"})
        result = dict(source="youtube", search_url=search_url)
        if not self.api_key:
            return DiscoveryBatch(**result, status="blocked", reason="api_key_missing",
                                message="Автопоиск YouTube не настроен: на backend отсутствует API-ключ. Доступны исходный сервис и ручной выбор в разделе «Дополнительно».")
        try:
            payload = await self.http.get_json("https://www.googleapis.com/youtube/v3/search", params={
                "part": "snippet", "type": "video", "videoEmbeddable": "true",
                "maxResults": 5, "q": f"{recording.artist} {recording.title}",
            }, api_key=self.api_key)
            ids = [item.id.videoId for item in _Search.model_validate(payload).items[:5]]
            if not ids:
                return DiscoveryBatch(**result, status="ok", reason="no_results", message="YouTube не вернул подходящих видео.")
            payload = await self.http.get_json("https://www.googleapis.com/youtube/v3/videos", params={
                "part": "snippet,contentDetails,status", "id": ",".join(ids),
            }, api_key=self.api_key)
            candidates = self._recordings(payload, ids)
            return DiscoveryBatch(**result, status="ok", reason="candidates_found" if candidates else "no_playable_results",
                                  message="Метаданные YouTube получены. Доступность воспроизведения подтверждает видимый плеер.", recordings=candidates)
        except (SourceFailure, ValidationError) as exc:
            reason = exc.reason if isinstance(exc, SourceFailure) else "invalid_response"
            return DiscoveryBatch(**result, status="error", reason=reason,
                                message="Поиск YouTube недоступен: проверьте доступ, квоту или сеть. Можно открыть исходный сервис.")

    def _recordings(self, payload: dict, ids: list[str]) -> list[DiscoveredRecording]:
        candidates = []
        for video in _Videos.model_validate(payload).items[:5]:
            if video.id not in ids or video.status.privacyStatus != "public" or video.snippet.liveBroadcastContent != "none":
                continue
            title = html.unescape(video.snippet.title)
            artist = html.unescape(video.snippet.channelTitle or "") or None
            duration = duration_seconds(video.contentDetails.duration)
            candidates.append(DiscoveredRecording(
                id=video.id, title=title, artist=artist, duration_seconds=duration,
                method="youtube_iframe" if video.status.embeddable else "external_link",
                external_url=f"https://www.youtube.com/watch?v={video.id}",
            ))
        return candidates

    async def prepare(self, recording: RecordingReference, candidate_id: str) -> SourceResult:
        if not re.fullmatch(VIDEO_ID, candidate_id):
            raise SourceFailure("invalid_video_id")
        search_url = f"https://www.youtube.com/watch?v={candidate_id}"
        if not self.api_key:
            return rank_batch(recording, DiscoveryBatch(source="youtube", status="blocked", reason="api_key_missing",
                message="API-ключ YouTube не настроен. Можно открыть видео на исходном сервисе.", search_url=search_url))
        try:
            payload = await self.http.get_json("https://www.googleapis.com/youtube/v3/videos", params={
                "part": "snippet,contentDetails,status", "id": candidate_id,
            }, api_key=self.api_key)
            batch = DiscoveryBatch(source="youtube", status="ok", reason="video_unavailable",
                message="Видео недоступно или не допускает встроенное воспроизведение.", search_url=search_url,
                recordings=self._recordings(payload, [candidate_id]))
            return rank_batch(recording, batch)
        except (SourceFailure, ValidationError) as exc:
            reason = exc.reason if isinstance(exc, SourceFailure) else "invalid_response"
            return SourceResult(source="youtube", status="error", reason=reason, stage="source_metadata",
                                search_url=search_url, message="Не удалось проверить видео YouTube. Можно открыть исходный сервис.")
