# Local automatic Play experiment

Automatic torrent Play is disabled at normal FastAPI startup. The browser
discovers it through a local-only capability route. When enabled, Play creates
one server-side preparation job. The server searches a bounded set of releases,
checks each file list with TrackMatcher, requires an MP3 match, and waits for
verified first bytes from an explicit peer. Only then does it return
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
`MUSICSTREAM_PROWLARR_API_KEY`, and an explicit peer. Only approved search hits
are resolved. Public release playback, rights checks, and peer discovery policy
need a separate review.

## Controlled second-node TCP check

This check requires a second computer that you control, with your own test MP3
album and the same `.torrent` metadata. A LAN or private VPN IP of that second
computer counts as a remote peer; another address on the MusicStream computer
does not. No tracker, DHT, peer exchange, uTP, inbound MusicStream listener, or
port forwarding is used. Do not use an unrelated peer or public release.

On the second computer, copy the owned album and this seeder script. Start the
seeder bound to that computer's specific LAN IP (never a wildcard address):

```powershell
.venv\Scripts\python.exe scripts\local_torrent_seeder.py test_media\TorrentTestAlbum --bind 192.168.1.20
```

Copy the printed `.local.torrent` file to the MusicStream computer. The MP3
files stay on the second computer. Use the printed peer port below. A firewall
must already allow the two computers to communicate on that port; this procedure
does not alter Windows firewall or network settings.

```powershell
$env:MUSICSTREAM_TORRENT_HTTP_ENABLED = 'true'
$env:MUSICSTREAM_TORRENT_AUTO_ENABLED = 'true'
$env:MUSICSTREAM_TORRENT_NETWORK_MODE = 'remote_tcp'
$env:MUSICSTREAM_TORRENT_REMOTE_PEERS = '[["192.168.1.20", 43821]]'
$env:MUSICSTREAM_TORRENT_AUTO_TEST_SOURCE = 'C:\path\to\TorrentTestAlbum.local.torrent'
.venv\Scripts\python.exe -m uvicorn musicstream.main:app --host 127.0.0.1 --port 8000
```

Replace the example IP, port, and torrent path. Open `/app` locally, select the
matching Deezer test track, then check Play, Pause, Seek, and switching tracks.
For a direct Range check, the local endpoint is
`GET /api/torrent/audio/{opaque-playback-id}` with a `Range: bytes=...` header;
the automatic Play flow obtains that ID without exposing a torrent link.
Stop the second-node seeder to confirm a new uncached track becomes unavailable
within the configured read timeout. The default remote read timeout is 20 s,
and `MUSICSTREAM_TORRENT_AUTO_READ_TIMEOUT` can be set from 1 to 60 s.
`MUSICSTREAM_TORRENT_PEER_CONNECT_TIMEOUT` is 2 to 30 s (default 8).

Remote mode is enabled only when the HTTP experiment is enabled and
`MUSICSTREAM_TORRENT_NETWORK_MODE=remote_tcp` is set. It requires one to four
explicit non-loopback IP addresses in `MUSICSTREAM_TORRENT_REMOTE_PEERS`.
Normal FastAPI startup does not start libtorrent. Disk cache defaults to 16 MiB;
change it with `MUSICSTREAM_TORRENT_CACHE_MAX_BYTES` and choose the temporary
directory with `MUSICSTREAM_TORRENT_TEMP_DIR`.

Targeted automated checks:

```powershell
.venv\Scripts\python.exe -m pytest -q tests/test_torrent_auto_play.py tests/test_torrent_auto_browser.py tests/test_torrent_http.py
```

The browser test uses the prepared local test album and Edge if both are
available; it stubs only the Deezer search response with the same metadata
shape. It verifies actual MP3 playback, Pause, Seek and switching tracks.
