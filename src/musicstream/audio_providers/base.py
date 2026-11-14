from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field


class ResolutionDiagnostics(BaseModel):
    """Local-only, allowlisted facts. Never store URLs, headers or cookies."""

    elapsed_ms: int | None = None
    metadata_status: int | None = None
    player_format: Literal["cuts", "music", "missing", "unsupported"] = "missing"
    player_relative: bool = False
    player_valid: bool = False
    redirect_status: int | None = None
    redirect_host: str | None = Field(default=None, pattern=r"^(ru\.hitmox\.org|cdn(?:11|13|14)\.deliciousoranges\.com|s4\.deliciouspeaches\.com|unapproved)$")
    redirect_format: Literal["cuts", "music", "missing", "unsupported"] = "missing"
    redirect_valid: bool = False


@dataclass(frozen=True)
class SourceDocument:
    """Internal metadata document and a source-issued public playback redirect."""

    html: str
    stream_url: str | None = None
    playback_reason: str | None = None
    diagnostics: ResolutionDiagnostics | None = None


class RecordingReference(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    artist: str = Field(min_length=1, max_length=200)
    duration_seconds: float | None = Field(default=None, ge=0, le=86400)
    # Deezer false means only "not flagged"; it does not prove a clean master.
    explicit_lyrics: bool | None = None


class RecordingComparison(BaseModel):
    reference_title: str
    reference_artist: str
    reference_duration_seconds: float | None
    candidate_title: str
    candidate_artist: str | None
    candidate_duration_seconds: float | None
    duration_difference_seconds: float | None
    duration_tolerance_seconds: float | None


class MatchAssessment(BaseModel):
    status: Literal["likely", "uncertain", "mismatch"]
    reasons: list[str]
    comparison: RecordingComparison | None = None


class AudioCandidate(BaseModel):
    id: str
    title: str
    artist: str | None = None
    duration_seconds: float | None = None
    match: MatchAssessment
    method: Literal["youtube_iframe", "external_link", "html_audio"]
    external_url: str
    stream_url: str | None = None
    selection_reason: str | None = None
    source: Literal["youtube", "hitmos"] | None = None
    match_score: float | None = Field(default=None, ge=0, le=1)


class DiscoveredRecording(BaseModel):
    """Metadata only. No matching decision, session data or stream URL."""

    id: str
    title: str
    artist: str | None = None
    duration_seconds: float | None = None
    method: Literal["youtube_iframe", "external_link", "html_audio"]
    external_url: str


class DiscoveryBatch(BaseModel):
    source: Literal["youtube", "hitmos"]
    status: Literal["ok", "blocked", "error"]
    reason: str
    message: str
    search_url: str
    recordings: list[DiscoveredRecording] = Field(default_factory=list)
    complete: bool = True
    diagnostics: ResolutionDiagnostics | None = None


class ProviderSummary(BaseModel):
    source: Literal["youtube", "hitmos"]
    status: Literal["ok", "blocked", "error"]
    reason: str
    message: str
    search_url: str


class SourceResult(BaseModel):
    source: Literal["youtube", "hitmos", "auto"]
    status: Literal["available", "unavailable", "error", "external_player"]
    reason: str
    message: str
    search_url: str
    candidates: list[AudioCandidate] = Field(default_factory=list)
    stage: Literal["source_search", "source_metadata", "matching", "stream_resolution", "cdn_response", "browser_playback"] | None = None
    diagnostics: ResolutionDiagnostics | None = None
    selection: Literal["none", "automatic", "choose"] = "none"
    selected_id: str | None = None
    selected_source: Literal["youtube", "hitmos"] | None = None
    sources: list[ProviderSummary] = Field(default_factory=list)
    reused_match: bool = False


class AudioProvider(ABC):
    @abstractmethod
    async def discover(self, recording: RecordingReference) -> DiscoveryBatch:
        """Return source metadata without deciding whether the recording matches."""

    @abstractmethod
    async def prepare(self, recording: RecordingReference, candidate_id: str) -> SourceResult:
        """Revalidate the chosen source and prepare its permitted playback method."""

    @abstractmethod
    async def find(self, recording: RecordingReference) -> SourceResult:
        """Discover metadata and a permitted playback method; never return audio bytes."""
