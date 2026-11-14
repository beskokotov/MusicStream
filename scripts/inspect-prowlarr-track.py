"""Read-only local approval preview. Downloads torrent metadata, never audio."""

import argparse
import asyncio
import os
import tempfile
from pathlib import Path
from xml.etree import ElementTree

import httpx
import libtorrent as lt

from musicstream.torrent.metadata_resolver import TorrentMetadataResolver
from musicstream.torrent.prowlarr_discovery import ProwlarrDiscoveryProvider, release_fingerprint
from musicstream.torrent.release_selection import rank_releases
from musicstream.torrent.track_matcher import TrackReference, match_track


def label(value: str) -> str:
    return "".join(char if char.isprintable() else " " for char in value)[:160]


async def inspect(artist: str, album: str, title: str, release_index: int | None) -> None:
    config = Path(os.environ["LOCALAPPDATA"]) / "MusicStream" / "Prowlarr" / "data" / "config.xml"
    key = ElementTree.parse(config).getroot().findtext("ApiKey")
    if not key:
        raise ValueError("Prowlarr API key is missing")
    reference = TrackReference(artist, title, album)
    async with httpx.AsyncClient(trust_env=False) as client:
        provider = ProwlarrDiscoveryProvider(client, api_key=key, indexer_ids=(1,))
        releases = await provider.search(artist, album, limit=20)
        print("Search results:", len(releases))
        for index, release in enumerate(releases):
            print(index, label(release.title), "seeders:", release.seeders,
                  "approval fingerprint:", release_fingerprint(release))
        if release_index is None:
            return
        if not 0 <= release_index < len(releases):
            raise ValueError("Release index is outside the search results")
        release = releases[release_index]
        resolver = TorrentMetadataResolver(client, api_key=key, allowed_indexer_ids=(1,))
        resolved = await resolver.resolve(release)
        print("Torrent metadata:", resolved.status)
        if resolved.status != "ok" or resolved.metadata is None or resolved.torrent_bytes is None:
            return
        with tempfile.TemporaryDirectory(prefix="musicstream-approval-preview-") as directory:
            path = Path(directory) / "release.torrent"
            path.write_bytes(resolved.torrent_bytes)
            params = lt.load_torrent_file(str(path))
            hashes = params.ti.info_hashes()
            print("Private:", params.ti.priv(), "trackers:", len(params.trackers),
                  "web seeds:", len(params.url_seeds) + len(params.http_seeds))
            if hashes.has_v1():
                print("Info hash v1:", hashes.v1)
            if hashes.has_v2():
                print("Info hash v2:", hashes.v2)
        matched = match_track(reference, resolved.metadata)
        selection = rank_releases(reference, (release,),
                                  {release.source_id: resolved.metadata})
        print("Track match:", matched.status, "confidence:", matched.confidence,
              "release selection:", selection.reason)
        if matched.file_index is not None:
            chosen = resolved.metadata.files[matched.file_index]
            print("Audio file:", label(chosen.name), "index:", chosen.index,
                  "size:", chosen.size, "format:", chosen.extension)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artist")
    parser.add_argument("album")
    parser.add_argument("title")
    parser.add_argument("--release-index", type=int)
    args = parser.parse_args()
    asyncio.run(inspect(args.artist, args.album, args.title, args.release_index))
