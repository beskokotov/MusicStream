"""Opt-in torrent metadata inspection. Importing this package starts no session."""

from .metadata import (
    TorrentFile,
    TorrentMetadata,
    TorrentSettings,
    fetch_magnet_metadata,
    inspect_magnet,
    inspect_torrent,
    select_audio_file,
)
from .session import CacheLimitError, TorrentSessionManager

__all__ = [
    "TorrentFile", "TorrentMetadata", "TorrentSettings", "fetch_magnet_metadata",
    "inspect_magnet", "inspect_torrent", "select_audio_file",
    "CacheLimitError", "TorrentSessionManager",
]
