# Local, explicitly approved NoNaMe Club Auto Play

The ordinary `/app` Play button already sends the selected Deezer track to
`/api/experimental/playback/prepare`. Auto Play searches the configured local
Prowlarr indexer, resolves only approved search results, matches a specific
MP3/FLAC file, and starts `public_discovery` only after a second allowlist check
on the torrent info hash. The response to the browser contains only the status
and a local Range URL. This mode remains off in `scripts/start-local.ps1` unless
explicitly enabled.

## Review metadata before approval

With Prowlarr running, preview search results without starting BitTorrent:

```powershell
.venv\Scripts\python.exe scripts\inspect-prowlarr-track.py Rammstein Mutter Mutter
.venv\Scripts\python.exe scripts\inspect-prowlarr-track.py Rammstein Mutter Mutter --release-index 1
```

The first command prints release names, reported seeder counts, and SHA-256
approval fingerprints instead of source URLs. The second fetches only one
`.torrent` through the local resolver, prints its info hash, private flag,
tracker and web-seed counts, and TrackMatcher's file index and confidence. The
result index is specific to that search response; inspect the title before
using it. Neither command starts a BitTorrent session or downloads audio.

Approve a release and its exact info hash only after checking that the test is
permitted by the rights holder and tracker rules. For an approved public
torrent, stop the ordinary MusicStream service on port 8000 and launch:

```powershell
.\scripts\start-approved-public-play.ps1 `
  -ReleaseFingerprint 'sha256:<64 lowercase hex digits from the preview>' `
  -InfoHash '<40 or 64 hex digits from the inspected torrent>' `
  -BindIp '<IPv4 address of the local routed interface>'
```

The launcher reads the existing Prowlarr API key from its local `config.xml` at
runtime. It writes no key or password to source code or arguments, sets both
allowlists only for the child MusicStream process, and refuses to use a port
already occupied by MusicStream. It does not change Windows, Prowlarr, HAPP,
or router settings. The source fingerprint is a hash of Prowlarr's internal
identifier, not an automatically granted permission. Only indexer ID 1 is
enabled. The BitTorrent mode has one session, up to 12 peer connections, a
16 MiB disk cache, a 45-second first-range wait, and hash verification.

## Local catalog mode

To search the configured NoNaMe Club music indexer for any Deezer track shown
in the local app, start the server with the explicit catalog switch:

```powershell
.\scripts\start-approved-public-play.ps1 -CatalogMode -BindIp '<local routed IPv4 address>'
```

This mode searches `artist + album` and `artist + track title`, merges duplicate
results, checks the actual torrent file list against the selected track, and
accepts public torrents without a per-release allowlist. Private torrents,
ambiguous file matches, unsupported formats, and releases without a verified
audio range remain unavailable. It is local to this process and is disabled
unless `MUSICSTREAM_TORRENT_ALLOW_UNLISTED_PUBLIC=true` is set. Prowlarr still
limits discovery to the configured music indexer; the app does not crawl or
mirror either catalog. Search results and playback depend on what the indexer
and active peers currently provide, so coverage of every Deezer track is not
guaranteed.

## Non-Mutter live check on port 8000

On 2026-11-14, the catalog mode found seven NoNaMe Club results for Deezer's
`Кино — Группа крови`. It matched `01 - Группа крови.mp3` at confidence 0.91.
The selected swarm had no peer advertising the requested pieces, so preparation
ended `unavailable` with zero payload and verified bytes.

A separate `Rammstein — Zeit` check found six results and matched `02 Zeit.mp3`
at confidence 1.0. The public swarm supplied 131,072 BitTorrent payload bytes;
the first requested piece was verified before playback. The local Range route
returned `206 audio/mpeg` and 4,096 requested bytes. Edge then played the
selected Deezer track through `/app`: its media clock passed 0.5 seconds, and
Pause worked. This proves another album path besides `Mutter`; peer availability
still varies by release and time.

Local server logs are under `%LOCALAPPDATA%\MusicStream\logs`. They contain
aggregate search, match, peer, block, payload, and verified-piece counters,
but no tracker URLs, peer IPs, torrent download links, or API keys. The normal
browser interface reports search, preparation, buffering, playing, and source
unavailability without torrent details. Cancel or switch tracks to revoke the
current transfer. Reported indexer seeders do not prove live availability.

On 2026-11-14, live Deezer returned `Rammstein — Mutter` (album `Mutter`, 268
seconds). NoNaMe Club returned five releases; the selected FLAC tracks + cue
release reported eight seeders. The resolver read a 16,373-byte `.torrent` and
TrackMatcher selected file index 5, `06 Mutter.flac`, 34,496,471 bytes, at 0.88
confidence. The torrent is public and has five trackers and no web seeds. Its
hash was **not** approved for audio transfer. No BitTorrent session or audio
request for this release was started. Remote P2P playback remains unverified.

A live Auto Play preparation using the Deezer track returned by the already
running local MusicStream and the real Prowlarr provider found five releases,
filtered them to one metadata-approved fingerprint, selected file index 5,
and returned `unavailable` at the info-hash gate with zero P2P sessions. A
separate temporary browser process could not reach Deezer (`502`), whereas the
existing MusicStream service returned the live Deezer track and the targeted
local browser tests passed. Therefore this run does not prove the complete
live browser-to-P2P-to-sound chain.

## Explicitly authorized P2P and browser check

After the user explicitly authorized the P2P → Range → sound test, the selected
public FLAC torrent was allowlisted for a temporary, single-process run. The
repository's existing allowlist was not changed. An initial sandboxed probe
could not reach trackers (errors 5 and 11001, no DHT nodes). A network-enabled
probe used an empty cache and disabled Web Seeds. It found 10 peers, completed
a remote handshake, received 1,048,576 BitTorrent payload bytes, verified
1,048,576 bytes of requested pieces, and returned the first 4,096 FLAC bytes.
Time to that verified range was 15.865 seconds. Its session and temporary
cache were closed.

In a separate network-enabled run, the ordinary `/app` loaded the live Deezer
`Mutter` track. Auto Play found five NoNaMe Club results, retained the one
approved release, and selected `06 Mutter.flac` (index 5, confidence 0.88;
Prowlarr then reported seven seeders). The first checked fragment took 2.486
seconds after the server requested it. The server reported 1,048,576 payload
and verified bytes, 64 finished blocks, one completed handshake, and no Web
Seed connection. Edge received a `206 audio/flac` Range response. Its media
clock passed 0.5 seconds after 5.735 seconds from Play; a Web Audio analyser
connected to the audio destination measured nonzero decoded-signal RMS 0.721.
Pause worked. This confirms a non-silent decoded audio signal in Edge, but is
not a physical loudspeaker measurement.

The first Seek script set the slider to 25 seconds but incorrectly waited for
the clock to exceed 50 seconds. It observed 25 seconds and therefore cannot
establish post-seek playback. A corrected repeat failed during the initial
P2P preparation: nine peers were listed, but no handshake completed within the
read timeout, no blocks or verified bytes arrived, and Auto Play reported
unavailable. That repeat did not reach a Seek request. Thus Seek on this remote
swarm remains unverified, and peer availability is intermittent.

## Fast track switching and preparation errors

During a later interactive run, the server log showed successful searches and
file matches, followed by `stage=acquire exception=HTTPException` on quick
switches. The Range registry previously allowed only 0.1 seconds for the old
HTTP stream to release its single slot. Auto Play now waits up to 8 seconds,
bounded by the remaining preparation deadline. A stream-limit HTTP 429 is
reported as `unavailable` rather than an internal preparation error. The
server log now records the failing stage and exception class (or HTTP status)
without exposing addresses or credentials. A targeted test held the old stream
lease during a switch and confirmed the next preparation reached `ready` once
the lease was released; 20 related Auto Play, Range, and Web Seed tests passed.

A later interactive log confirmed `stage=acquire http_status=429` on several
fast switches. Waiting alone did not solve it: a disconnected Range body could
retain its stream lease. Unregistering the old track now closes its transfer
and releases that lease immediately. The eventual HTTP-body cleanup is
idempotent, so it cannot release the semaphore twice. A targeted test holds an
old lease indefinitely, switches songs, observes the next song become ready,
and checks that a late release leaves the stream limit unchanged. The related
Auto Play, Range, Web Seed, and browser checks passed after the change.
