"""Monotonic, local-only Auto Play timing without source URLs or credentials."""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass


logger = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class PlaybackTiming:
    job_id: str
    started_at: float

    def mark(self, stage: str, *, operation_started: float | None = None,
             indexer_id: int | None = None, status: str | None = None) -> None:
        now = time.monotonic()
        logger.info("Auto timing job=%s stage=%s elapsed_ms=%.1f duration_ms=%s indexer_id=%s status=%s",
                    self.job_id, stage, (now - self.started_at) * 1000,
                    f"{(now - operation_started) * 1000:.1f}" if operation_started else "-",
                    indexer_id if indexer_id is not None else "-", status or "-")


current_timing: ContextVar[PlaybackTiming | None] = ContextVar("musicstream_playback_timing", default=None)
