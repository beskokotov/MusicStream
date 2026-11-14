# Explicit public peer discovery

`public_discovery` is an experimental, opt-in BitTorrent mode. Normal FastAPI
startup and the existing `loopback` and `remote_tcp` modes are unchanged. This
mode uses trackers embedded in an approved `.torrent` and libtorrent DHT to find
peers. It does not require manually entering a peer IP. PEX, local service
discovery, UPnP, NAT-PMP, web seeds, and incoming peer connections remain off.
To use DHT and UDP trackers, libtorrent binds an ephemeral UDP port on the
selected local interface; no Windows firewall or router settings are changed.
On a computer with multiple adapters, set `MUSICSTREAM_TORRENT_PUBLIC_BIND_IP`
to the specific local IPv4 address that routes to the trackers. Public mode
binds its UDP socket and outgoing TCP peer connections to that address. It
rejects a missing, loopback, or wildcard address; the other network modes do
not use this setting. This avoids wildcard binding to unrelated adapters and
loopback, which can produce `announce_skipped` tracker errors.

Approve only a torrent whose content you own or have permission to share and
whose tracker rules permit this use. The application rejects torrents marked
`private` even when their info hash is approved. A missing or different hash is
also rejected before a libtorrent session is created. The allowlist is local
configuration and must not be sent to the browser.

For a permitted `.torrent`, inspect its v1 hash locally:

```powershell
.venv\Scripts\python.exe -c "import libtorrent as lt; p=lt.load_torrent_file(r'C:\path\to\approved.torrent'); print('private:', p.ti.priv()); print('v1:', p.ti.info_hashes().v1)"
```

For a v2-only torrent, use `.v2` instead of `.v1`. Then enable the existing
local HTTP experiment and set the exact hash:

```powershell
$env:MUSICSTREAM_TORRENT_HTTP_ENABLED = 'true'
$env:MUSICSTREAM_TORRENT_AUTO_ENABLED = 'true'
$env:MUSICSTREAM_TORRENT_NETWORK_MODE = 'public_discovery'
$env:MUSICSTREAM_TORRENT_PUBLIC_BIND_IP = '<local routed IPv4 address>'
$env:MUSICSTREAM_TORRENT_PUBLIC_APPROVED_INFO_HASHES = '["<approved-info-hash>"]'
$env:MUSICSTREAM_TORRENT_AUTO_TEST_SOURCE = 'C:\path\to\approved.torrent'
.venv\Scripts\python.exe -m uvicorn musicstream.main:app --host 127.0.0.1 --port 8000
```

The owned test source is useful only if its seeder announces to an appropriate
tracker or DHT; the existing loopback seeder intentionally does neither. The
Prowlarr path still needs its separate approved source IDs and indexer IDs.
Public mode adds an info-hash allowlist on top of those checks.

The default first-range wait is 30 seconds (`MUSICSTREAM_TORRENT_AUTO_READ_TIMEOUT`,
allowed 1-60). Connection and handshake timeout defaults to 8 seconds
(`MUSICSTREAM_TORRENT_PEER_CONNECT_TIMEOUT`, allowed 2-30). The session permits
four connections and one concurrent torrent in the app. The selected file and
requested pieces retain priority; data is returned only after piece hash
verification. Disk cache defaults to 16 MiB and can be changed with
`MUSICSTREAM_TORRENT_CACHE_MAX_BYTES`; temporary files are removed on close.

Internal callers can inspect `TorrentPlaybackRegistry.diagnostics(playback_id)`
while a transfer is active. It returns peer-list count, connected-peer count,
tracker replies and errors, DHT nodes and bootstrap status, connected seeds,
completed peer handshakes, requested-piece availability, peers choking that
piece, downloaded payload bytes, verified requested-piece bytes, and seconds
from the first range request to its first ready audio fragment. It returns no IP
addresses, torrent URLs, tracker names, credentials, or file paths. No HTTP
diagnostic route or player UI was added.

The first live Internet Archive test confirmed tracker and DHT discovery, but
the connected seed did not upload the requested piece. See
[`torrent-public-live-test.md`](torrent-public-live-test.md). Browser playback
from a public swarm remains unverified. A loopback swarm does not establish
public discovery.
