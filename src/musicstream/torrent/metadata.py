"""Inspect torrent file lists without downloading media payloads."""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlsplit


AUDIO_EXTENSIONS = frozenset({".mp3", ".flac", ".ogg", ".m4a"})


def _libtorrent():
    try:
        import libtorrent as lt
    except ImportError as exc:
        raise RuntimeError("Install the torrent extra: uv sync --extra torrent") from exc
    return lt


@dataclass(frozen=True)
class TorrentSettings:
    temp_dir: Path = Path(tempfile.gettempdir()) / "musicstream-torrent"
    cache_max_bytes: int = 16 * 1024 * 1024

    def __post_init__(self):
        if self.cache_max_bytes <= 0:
            raise ValueError("cache_max_bytes must be positive")

    @classmethod
    def from_env(cls) -> TorrentSettings:
        return cls(
            temp_dir=Path(os.environ.get("MUSICSTREAM_TORRENT_TEMP_DIR") or Path(tempfile.gettempdir()) / "musicstream-torrent"),
            cache_max_bytes=int(os.environ.get("MUSICSTREAM_TORRENT_CACHE_MAX_BYTES", 16 * 1024 * 1024)),
        )


@dataclass(frozen=True)
class TorrentFile:
    index: int
    name: str
    path: str
    size: int
    extension: str

    @property
    def is_audio(self) -> bool:
        return self.extension in AUDIO_EXTENSIONS


@dataclass(frozen=True)
class TorrentMetadata:
    name: str
    files: tuple[TorrentFile, ...]
    info_hash: str | None = None


def _from_info(info) -> TorrentMetadata:
    storage = info.files()
    files = tuple(
        TorrentFile(
            index=i,
            name=PurePosixPath(storage.file_path(i).replace("\\", "/")).name,
            path=storage.file_path(i).replace("\\", "/"),
            size=storage.file_size(i),
            extension=PurePosixPath(storage.file_path(i).replace("\\", "/")).suffix.lower(),
        )
        for i in range(storage.num_files())
    )
    return TorrentMetadata(name=info.name(), files=files)


def inspect_torrent(path: str | Path) -> TorrentMetadata:
    """Read a local .torrent. No libtorrent session or network access is created."""
    source = Path(path)
    if source.suffix.lower() != ".torrent" or not source.is_file():
        raise ValueError("Expected an existing .torrent file")
    return _from_info(_libtorrent().torrent_info(str(source)))


def inspect_magnet(uri: str) -> TorrentMetadata:
    """Inspect only URI fields; a magnet alone does not contain a file list."""
    parts = urlsplit(uri)
    if parts.scheme.lower() != "magnet":
        raise ValueError("Expected a magnet URI")
    fields = parse_qs(parts.query)
    hashes = [value for value in fields.get("xt", []) if value.lower().startswith(("urn:btih:", "urn:btmh:"))]
    if not hashes:
        raise ValueError("Magnet URI has no BitTorrent info hash")
    return TorrentMetadata(name=fields.get("dn", [""])[0], files=(), info_hash=hashes[0])


def select_audio_file(metadata: TorrentMetadata, index: int) -> TorrentFile:
    """Select an audio entry by its stable libtorrent file index; no transfer starts."""
    if not isinstance(index, int) or isinstance(index, bool) or index < 0 or index >= len(metadata.files):
        raise ValueError("Invalid torrent file index")
    chosen = metadata.files[index]
    if chosen.index != index or not chosen.is_audio:
        raise ValueError("Selected file is not MP3, FLAC, OGG or M4A")
    return chosen


def fetch_magnet_metadata(
    uri: str, *, settings: TorrentSettings | None = None, timeout_seconds: float = 30
) -> TorrentMetadata:
    """Explicitly contact peers for metadata only. Never request torrent pieces."""
    inspect_magnet(uri)
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    settings = settings or TorrentSettings.from_env()
    lt = _libtorrent()
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.temp_dir) as save_path:
        session = lt.session({
            "listen_interfaces": "",
            "enable_upnp": False,
            "enable_natpmp": False,
            "enable_lsd": False,
            "max_metadata_size": settings.cache_max_bytes,
        })
        try:
            params = lt.parse_magnet_uri(uri)
            params.save_path = save_path
            params.flags |= lt.torrent_flags.upload_mode
            # Magnet links may carry tracker URLs; this feature never contacts them.
            params.trackers = []
            params.url_seeds = []
            params.http_seeds = []
            handle = session.add_torrent(params)
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                if handle.has_metadata():
                    return _from_info(handle.torrent_file())
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
            raise TimeoutError("Magnet metadata was not received in time")
        finally:
            session.pause()
            if "handle" in locals():
                session.remove_torrent(handle)
                del handle
            del session
