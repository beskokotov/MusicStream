# Local automatic Play experiment

Automatic torrent Play is disabled at normal FastAPI startup. The browser
discovers it through a local-only capability route. When enabled, Play creates
one server-side preparation job. The server searches a bounded set of releases,
checks each file list with TrackMatcher, requires an MP3 match, and waits for
verified first bytes from an explicit loopback peer. Only then does it return
an opaque HTTP Range path to the browser. The browser sees `preparing`,
`buffering`, `ready`, `unavailable`, or `error`; no torrent URL, indexer ID,
API key, or file path is included in the response.

## Reproduce with the prepared local MP3 album

In one PowerShell terminal, run the existing loopback seeder and note its port:

```powershell
.venv\Scripts\python.exe scripts\local_torrent_seeder.py test_media\TorrentTestAlbum
```

In another terminal, set the printed port and start the local app:

```powershell
$env:MUSICSTREAM_TORRENT_HTTP_ENABLED = 'true'
$env:MUSICSTREAM_TORRENT_AUTO_ENABLED = 'true'
$env:MUSICSTREAM_TORRENT_AUTO_TEST_SOURCE = (Resolve-Path 'test_media\TorrentTestAlbum.local.torrent').Path
$env:MUSICSTREAM_TORRENT_HTTP_PEER_PORT = '<printed loopback port>'
.venv\Scripts\python.exe -m uvicorn musicstream.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/app`, search Deezer for `DMXS Death Desert`,
select the track and press Play. The existing bottom controls handle Pause,
resume and Seek. Selecting another track cancels the previous preparation and
revokes its Range URL. This mode reads only the configured local `.torrent` and
explicit loopback peer. It does not query Prowlarr or public peers.

The Prowlarr path is implemented but requires explicit local configuration:
`MUSICSTREAM_TORRENT_ALLOWED_INDEXER_IDS` (comma-separated IDs),
`MUSICSTREAM_TORRENT_APPROVED_SOURCE_IDS` (JSON array of exact source IDs),
`MUSICSTREAM_PROWLARR_API_KEY`, and the same loopback peer port. Only approved
search hits are resolved. The current `TorrentSessionManager` intentionally
accepts only loopback peers, so this is not a general public torrent player.
Public release playback, rights checks, and peer policy need a separate review.

Targeted automated checks:

```powershell
.venv\Scripts\python.exe -m pytest -q tests/test_torrent_auto_play.py tests/test_torrent_auto_browser.py tests/test_torrent_http.py
```

The browser test uses the prepared local test album and Edge if both are
available; it stubs only the Deezer search response with the same metadata
shape. It verifies actual MP3 playback, Pause, Seek and switching tracks.
