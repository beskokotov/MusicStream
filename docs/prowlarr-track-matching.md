# Experimental Prowlarr track discovery

`ProwlarrDiscoveryProvider` is separate from MusicStream's current Source Manager and
public API. It searches Prowlarr's `/api/v1/search` with the Audio (3000) category.
Set `MUSICSTREAM_PROWLARR_API_KEY` in the local process environment, or pass the key
to the constructor from a local secret store. The key is sent in `X-Api-Key`; the
adapter removes Prowlarr's embedded `apikey` parameter from returned local
metadata URLs. Do not log the raw Prowlarr response or use its download URL
without stripping the key.

`TrackReference.from_deezer(track)` converts the existing Deezer `Track`. Call
`discover_and_match(reference, provider, metadata_resolver)` to rank search hits,
request file lists for at most five leading releases, and pick a unique matching
audio file index. The injected resolver is responsible only for obtaining
`TorrentMetadata`; it can read a local `.torrent` for offline diagnostics.
Nothing here starts a BitTorrent session or downloads audio. The result contains
no chosen release when the title, album, or file match is weak or ambiguous.

The matcher handles MP3, FLAC, OGG and M4A file names, including Cyrillic,
Latin transliteration, leading track numbers and optional artist aliases. The
ranking slightly favors MP3 because the current player supports it best. A
single audio image plus `.cue` is marked unsupported. Duration is kept in the
reference but cannot be checked from torrent file lists alone. Metadata lookup
for public releases, format probing, playback integration and any fallback
policy belong to a later iteration.

Offline check:

```powershell
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/test_torrent_discovery_match.py
```

The live diagnostic uses only Prowlarr search. It requires a locally running
Prowlarr instance and a valid local API key; it does not fetch torrent files.

`TorrentMetadataResolver` accepts only an allowlisted indexer ID and the exact
local Prowlarr `/<indexer-id>/download` route with its opaque `link` parameter.
It sends the key in a header, rejects redirects, and limits the response to 4 MiB
by default. It writes the bounded `.torrent` to a temporary directory and calls
the existing `inspect_torrent`, then removes the file. It never contacts a
`guid`, `infoUrl`, magnet, or arbitrary external URL. A Prowlarr installation
that redirects its download route externally will return `redirect_rejected`
until that source has a separately reviewed metadata path.

`resolve_track` returns an explicit status such as `matched`, `missing`,
`ambiguous`, `unsupported`, `auth_error`, `too_large`, or `invalid_torrent`.
The local HTTP integration test serves a `.torrent` made from tiny original
test files and confirms the real libtorrent file indexes and sizes without a
BitTorrent session.

For an optional live Prowlarr check, run
`.venv\Scripts\python.exe scripts\local_prowlarr_metadata_check.py` while the
local Prowlarr instance is running. The script creates a temporary loopback
Torznab indexer for its own tiny test files, searches through Prowlarr, resolves
the `.torrent` through Prowlarr, prints only status and file metadata, and
removes the temporary indexer in a `finally` block. It reads the local
Prowlarr key from that instance's config file but never prints it.
