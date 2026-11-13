"""Application settings supplied by the environment."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    musicbrainz_contact: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        contact = os.getenv("MUSICBRAINZ_CONTACT", "").strip()
        if contact and (not contact.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in contact)):
            raise ValueError("MUSICBRAINZ_CONTACT must contain printable ASCII characters")
        return cls(musicbrainz_contact=contact or None)

    @property
    def user_agent(self) -> str | None:
        if self.musicbrainz_contact:
            return f"MusicStream/0.1.0 ({self.musicbrainz_contact})"
        return None
