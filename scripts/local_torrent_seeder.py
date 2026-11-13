"""Prepare and seed a local MP3 folder on loopback for manual HTTP Range checks."""

from __future__ import annotations

import argparse
import hashlib
import time
from pathlib import Path


PIECE_LENGTH = 64 * 1024


def bencode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(map(bencode, value)) + b"e"
    return b"d" + b"".join(bencode(key) + bencode(value[key]) for key in sorted(value)) + b"e"


def prepare(directory: Path) -> tuple[Path, list[Path]]:
    files = sorted(path for path in directory.iterdir() if path.is_file() and path.suffix.lower() == ".mp3")
    if not files:
        raise ValueError("No MP3 files in the local folder")
    pieces = []
    pending = bytearray()
    for path in files:
        with path.open("rb") as stream:
            while block := stream.read(PIECE_LENGTH):
                pending.extend(block)
                while len(pending) >= PIECE_LENGTH:
                    pieces.append(hashlib.sha1(pending[:PIECE_LENGTH]).digest())
                    del pending[:PIECE_LENGTH]
    if pending:
        pieces.append(hashlib.sha1(pending).digest())
    info = {
        b"name": directory.name.encode("utf-8"),
        b"piece length": PIECE_LENGTH,
        b"pieces": b"".join(pieces),
        b"files": [
            {b"length": path.stat().st_size, b"path": [path.name.encode("utf-8")]}
            for path in files
        ],
    }
    torrent_path = directory.parent / f"{directory.name}.local.torrent"
    torrent_path.write_bytes(bencode({b"info": info}))
    return torrent_path, files


def main() -> None:
    import libtorrent as lt

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    directory = args.directory.resolve()
    torrent_path, files = prepare(directory)
    print(f"Torrent: {torrent_path}", flush=True)
    for index, path in enumerate(files):
        print(f"track-{index}: {path.name}", flush=True)
    if args.prepare_only:
        return

    session = lt.session({
        "listen_interfaces": "127.0.0.1:0", "enable_dht": False,
        "enable_upnp": False, "enable_natpmp": False, "enable_lsd": False,
        "connections_limit": 4,
    })
    params = lt.add_torrent_params()
    params.ti = lt.torrent_info(str(torrent_path))
    params.save_path = str(directory.parent)
    params.flags |= lt.torrent_flags.seed_mode | lt.torrent_flags.override_trackers
    params.trackers = []
    handle = session.add_torrent(params)
    while session.listen_port() == 0:
        time.sleep(0.05)
    print(f"Loopback peer port: {session.listen_port()}", flush=True)
    print("Keep this process running; press Ctrl+C to stop.", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        session.pause()
        session.remove_torrent(handle)


if __name__ == "__main__":
    main()
