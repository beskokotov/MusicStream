"""Only locally generated torrent metadata; no peers or audio transfers."""

import hashlib
import sys
from pathlib import Path

import pytest

from musicstream.torrent import (
    TorrentSettings, fetch_magnet_metadata, inspect_magnet, inspect_torrent,
    select_audio_file,
)


def bencode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(map(bencode, value)) + b"e"
    return b"d" + b"".join(bencode(key) + bencode(value[key]) for key in sorted(value)) + b"e"


def test_local_torrent_lists_and_selects_files_without_session(tmp_path, monkeypatch):
    lt = pytest.importorskip("libtorrent")
    monkeypatch.setattr(lt, "session", lambda *args, **kwargs: pytest.fail("Reading a file must not open a session"))
    files = [(b"Demo.MP3", 3), (b"other.FLAC", 4), (b"notes.txt", 5), (b"song.ogg", 6), (b"song.m4a", 7)]
    info = {
        b"name": b"local-test",
        b"piece length": 16384,
        b"pieces": hashlib.sha1(b"x" * sum(size for _, size in files)).digest(),
        b"files": [{b"length": size, b"path": [name]} for name, size in files],
    }
    source = tmp_path / "local.torrent"
    source.write_bytes(bencode({b"info": info}))

    result = inspect_torrent(source)
    assert result.name == "local-test"
    assert [(f.index, f.name, f.size, f.extension) for f in result.files] == [
        (i, name.decode(), size, Path(name.decode()).suffix.lower())
        for i, (name, size) in enumerate(files)
    ]
    assert [select_audio_file(result, i).index for i in (0, 1, 3, 4)] == [0, 1, 3, 4]
    with pytest.raises(ValueError, match="not MP3"):
        select_audio_file(result, 2)
    with pytest.raises(ValueError, match="Invalid"):
        select_audio_file(result, 99)


def test_magnet_inspection_is_offline(monkeypatch):
    monkeypatch.setitem(sys.modules, "libtorrent", None)
    magnet = "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=Local%20Test"
    result = inspect_magnet(magnet)
    assert result.name == "Local Test"
    assert result.files == ()
    assert result.info_hash == "urn:btih:" + "a" * 40
    with pytest.raises(ValueError, match="info hash"):
        inspect_magnet("magnet:?dn=no-hash")
    with pytest.raises(ValueError, match="timeout"):
        fetch_magnet_metadata(magnet, timeout_seconds=0)


def test_settings_limit_and_temp_path(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSICSTREAM_TORRENT_TEMP_DIR", str(tmp_path))
    monkeypatch.setenv("MUSICSTREAM_TORRENT_CACHE_MAX_BYTES", "4096")
    settings = TorrentSettings.from_env()
    assert settings.temp_dir == tmp_path
    assert settings.cache_max_bytes == 4096
    with pytest.raises(ValueError, match="positive"):
        TorrentSettings(cache_max_bytes=0)


def test_explicit_magnet_fetch_disables_tracker_and_payload(tmp_path, monkeypatch):
    lt = pytest.importorskip("libtorrent")
    captured = {}

    class Handle:
        def has_metadata(self):
            return False

    class Session:
        def __init__(self, options):
            captured["options"] = options

        def add_torrent(self, params):
            captured["params"] = params
            return Handle()

        def pause(self):
            captured["paused"] = True

        def remove_torrent(self, handle):
            captured["removed"] = True

    monkeypatch.setattr(lt, "session", Session)
    magnet = "magnet:?xt=urn:btih:" + "a" * 40 + "&tr=https%3A%2F%2Ftracker.example%2Fa"
    with pytest.raises(TimeoutError):
        fetch_magnet_metadata(magnet, settings=TorrentSettings(tmp_path, 4096), timeout_seconds=0.01)
    assert captured["options"]["listen_interfaces"] == ""
    assert captured["options"]["enable_upnp"] is False
    assert captured["options"]["max_metadata_size"] == 4096
    assert captured["params"].flags & lt.torrent_flags.upload_mode
    assert captured["params"].trackers == []
    assert captured["paused"] and captured["removed"]
