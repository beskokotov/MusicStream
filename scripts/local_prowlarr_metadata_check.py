"""Optional loopback-only Prowlarr diagnostic with original tiny test files.

Creates a temporary Generic Torznab indexer, searches it once, resolves its
.torrent through Prowlarr, then removes the indexer. Never starts BitTorrent.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import threading
from datetime import datetime, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree
from xml.sax.saxutils import escape

import httpx

from musicstream.torrent.metadata_resolver import TorrentMetadataResolver
from musicstream.torrent.prowlarr_discovery import ProwlarrDiscoveryProvider
from musicstream.torrent.track_matcher import TrackReference


def bencode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(map(bencode, value)) + b"e"
    return b"d" + b"".join(bencode(key) + bencode(value[key]) for key in sorted(value)) + b"e"


def own_torrent(directory: Path) -> bytes:
    entries = []
    for name, payload in (
        ("cover.jpg", b"local-cover"),
        ("01 - Local Test Song.mp3", b"ID3" + b"A" * 64),
        ("02 - Other Song.mp3", b"ID3" + b"B" * 64),
    ):
        (directory / name).write_bytes(payload)
        entries.append((name, payload))
    data = b"".join(payload for _, payload in entries)
    info = {b"name": b"Local Test Album", b"piece length": 16384,
            b"pieces": hashlib.sha1(data).digest(),
            b"files": [{b"length": len(payload), b"path": [name.encode()]}
                       for name, payload in entries]}
    return bencode({b"info": info})


def serve(torrent: bytes):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            if url.path == "/api" and query.get("t") == ["caps"]:
                body = ("<?xml version='1.0'?><caps><server version='1.0' title='Local Test'/>"
                        "<limits max='100' default='100'/><searching><search available='yes' "
                        "supportedParams='q'/></searching><categories><category id='3000' "
                        "name='Audio'><subcat id='3010' name='MP3'/></category></categories></caps>").encode()
                content_type = "application/xml"
            elif url.path == "/api" and query.get("t") in (["search"], ["music"]):
                host = f"http://127.0.0.1:{self.server.server_port}"
                link = escape(host + "/own.torrent")
                body = ("<?xml version='1.0'?><rss version='2.0' "
                        "xmlns:torznab='http://torznab.com/schemas/2015/feed'><channel>"
                        "<title>Local Test</title><item><title>Local Artist - Local Test Album MP3 tracks</title>"
                        f"<pubDate>{format_datetime(datetime.now(timezone.utc))}</pubDate>"
                        f"<guid>{link}</guid><link>{link}</link>"
                        f"<enclosure url='{link}' length='{len(torrent)}' type='application/x-bittorrent'/>"
                        "<torznab:attr name='category' value='3010'/>"
                        "<torznab:attr name='seeders' value='1'/>"
                        f"<torznab:attr name='size' value='{len(torrent)}'/>"
                        "</item></channel></rss>").encode()
                content_type = "application/rss+xml"
            elif url.path == "/own.torrent":
                body, content_type = torrent, "application/x-bittorrent"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


async def run(base_url: str, key: str, indexer_id: int):
    async with httpx.AsyncClient(trust_env=False) as client:
        provider = ProwlarrDiscoveryProvider(client, api_key=key,
                                            base_url=base_url, indexer_ids=(indexer_id,))
        releases = await provider.search("Local Artist", "Local Test Album", limit=5)
        print("local_search_hits", len(releases))
        if len(releases) != 1:
            return
        resolver = TorrentMetadataResolver(client, api_key=key, base_url=base_url,
                                           allowed_indexer_ids=(indexer_id,))
        outcome = await resolver.resolve_track(
            TrackReference("Local Artist", "Local Test Song", "Local Test Album"), releases[0])
        print("resolution_status", outcome.status, "file_index", outcome.file_index)
        if outcome.metadata:
            print("files", [(f.index, f.name, f.size, f.extension) for f in outcome.metadata.files])


def main():
    config = Path(os.environ["LOCALAPPDATA"]) / "MusicStream" / "Prowlarr" / "data" / "config.xml"
    key = ElementTree.parse(config).getroot().findtext("ApiKey")
    base_url = "http://127.0.0.1:18746"
    with tempfile.TemporaryDirectory(prefix="musicstream-own-torrent-") as directory:
        server, thread = serve(own_torrent(Path(directory)))
        indexer_id = None
        try:
            with httpx.Client(base_url=base_url, headers={"X-Api-Key": key},
                              trust_env=False, timeout=30) as client:
                schema = next(s for s in client.get("/api/v1/indexer/schema").json()
                              if s.get("implementation") == "Torznab"
                              and s.get("name") == "Generic Torznab")
                schema["name"] = "MusicStream Own Test Torrent"
                schema["appProfileId"] = 1
                schema["enableRss"] = False
                for field in schema["fields"]:
                    if field["name"] == "baseUrl":
                        field["value"] = f"http://127.0.0.1:{server.server_port}"
                    if field["name"] == "apiPath":
                        field["value"] = "/api"
                    if field["name"] == "apiKey":
                        field["value"] = ""
                created = client.post("/api/v1/indexer", json=schema)
                print("temporary_indexer_create", created.status_code)
                if not created.is_success:
                    return
                indexer_id = created.json()["id"]
                asyncio.run(run(base_url, key, indexer_id))
                removed = client.delete(f"/api/v1/indexer/{indexer_id}")
                print("temporary_indexer_removed", removed.is_success)
                indexer_id = None
        finally:
            if indexer_id is not None:
                with httpx.Client(base_url=base_url, headers={"X-Api-Key": key},
                                  trust_env=False, timeout=10) as client:
                    client.delete(f"/api/v1/indexer/{indexer_id}")
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    main()
