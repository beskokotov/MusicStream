"""Killable bounded HTTPS worker for the opt-in local playback adapter."""

import json
import struct
import sys

from .safe_webseed import (
    PieceHashMismatchError, SafeWebSeedFetcher, UnsafeWebSeedError,
    WebSeedHTTPError,
)


def main() -> int:
    try:
        request = json.loads(sys.stdin.buffer.readline(4096))
        fetcher = SafeWebSeedFetcher(
            request["torrent_path"],
            approved_info_hashes=frozenset({request["info_hash"]}),
            archive_item=request["archive_item"], max_piece_bytes=1024 * 1024,
            max_redirects=3, max_connections=8, timeout_seconds=30,
        )
        count = request.get("piece_count", 1)
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 4:
            raise ValueError("Invalid piece count")
        pieces = []
        try:
            for offset in range(0, count, 2):
                pieces.extend(fetcher.fetch_pieces(request["piece_index"] + offset,
                                                   min(2, count - offset)))
        finally:
            fetcher.close()
        payload = b"".join(pieces)
        diagnostics = fetcher.diagnostics()
        summary = json.dumps({
            "connections": diagnostics.connections,
            "received_bytes": diagnostics.received_bytes,
            "http_errors": diagnostics.http_errors,
            "reused_connections": diagnostics.reused_connections,
            "redirects": diagnostics.redirects,
            "timings": {key: getattr(diagnostics, key + "_seconds") for key in
                        ("dns", "tcp", "tls", "response", "body", "sha1")},
        }).encode("ascii")
        sys.stdout.buffer.write(struct.pack("!I", len(payload)))
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.write(struct.pack("!H", len(summary)))
        sys.stdout.buffer.write(summary)
        sys.stdout.buffer.flush()
        return 0
    except UnsafeWebSeedError:
        return 10
    except WebSeedHTTPError:
        return 11
    except PieceHashMismatchError:
        return 12
    except TimeoutError:
        return 13
    except Exception:
        # URLs, file paths, IPs and environment values must not reach logs.
        return 14


if __name__ == "__main__":
    raise SystemExit(main())
