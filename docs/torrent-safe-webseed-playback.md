# Safe Web Seed HTTP Range playback — 2026-11-14

`safe_webseed` is an explicit local mode. The default remains `loopback`;
`remote_tcp` and `public_discovery` still use their existing libtorrent paths.
The new mode uses the standalone `SafeWebSeedFetcher` in a killable worker
process for each missing piece. The parent checks each returned piece's v1
SHA-1 again before caching or sending any bytes. There is no libtorrent
session, tracker, DHT, PEX, or P2P connection in this mode.

The worker is killed and awaited on task cancellation, HTTP disconnect, track
switch, or timeout. This also bounds a stuck system DNS lookup. Only one
transfer and one worker are active at a time. A transfer keeps at most 4 MiB of
verified pieces in memory; it has a 16 MiB default attempted-download budget,
including failed attempts. A worker has a 30-second fetch deadline and the
parent enforces a 40-second wall clock limit. The existing local-only HTTP
Range router supplies `200`, `206`, `416`, `Content-Range`, and streaming
blocks. The normal MusicStream interface is unchanged.

## Live Edge check

The one-off test server was bound to `127.0.0.1` and registered only the first
two MP3 files in the already approved `amp021.torrent`. The browser was Edge.
The test page used the existing `/api/experimental/torrent/{playback_id}`
router; it did not change the application UI. The first HTTP response was
`206 bytes 0-14675967/14675968`. Seeking caused another `206` starting at
byte 2621440. Switching to the second track produced
`206 bytes 0-17532927/17532928`.

The first verified piece of track 0 was ready after **3.766 s**. Edge emitted
`playing` and advanced `currentTime` after **55.234 s** from Play. Pause kept
`currentTime` stable; seeking to 50 seconds and resuming worked. Switching to
the second MP3 also produced `playing` and advancing `currentTime`. These are
browser media events, not an acoustic measurement of speaker output. The
first MP3 starts with an approximately 3.7 MiB ID3 tag, accounting for much
of the initial buffering delay. Track 1's first verified piece was ready
after 5.203 s from its first range read.

The server recorded at least 12 verified pieces / 6,291,456 bytes for track 0
and at least 13 pieces / 6,815,744 bytes for track 1 before the browser test
ended. Every piece came through HTTPS Web Seed redirects to approved Archive
download-node hosts. No local audio files, P2P peers, or libtorrent network
session were used. An intermittent worker failure (HTTP-response category,
code 11) occurred for one piece; Edge issued a later range and playback
continued. If such a failure occurs after streaming headers are sent, that
particular HTTP response ends early. This remains a reliability limitation.

## Manual local check

In PowerShell, with only the approved local test torrent:

```powershell
$env:MUSICSTREAM_TORRENT_HTTP_ENABLED = 'true'
$env:MUSICSTREAM_TORRENT_NETWORK_MODE = 'safe_webseed'
$env:MUSICSTREAM_TORRENT_PUBLIC_APPROVED_INFO_HASHES = '["3191c92459c15c0aac4a79ff3e2b7c7914eb3922"]'
$env:MUSICSTREAM_TORRENT_HTTP_SOURCE = (Resolve-Path 'test_media/public_discovery/amp021.torrent').Path
$env:MUSICSTREAM_TORRENT_HTTP_FILE_INDEXES = '0,1'
$env:MUSICSTREAM_TORRENT_AUTO_READ_TIMEOUT = '45'
uv run --extra torrent uvicorn musicstream.main:app --host 127.0.0.1 --port 18747
```

Open `http://127.0.0.1:18747/api/experimental/torrent/track-0` in Edge. The
mode remains inaccessible to remote clients through the router's loopback
check. A transient Archive redirect outside the narrow hostname policy is
rejected. The 16 MiB per-transfer budget may stop full playback of longer
tracks; increasing it requires an explicit policy decision. No public
discovery or ordinary startup behavior changes with this opt-in mode.

## Reliability and startup diagnosis (repeat Edge run)

The first verified HTTP block is now prepared before a `200`/`206` response
starts. A first-piece failure returns `502` or `504`, and a client disconnect
while preparing it cancels the worker. If a later piece fails after headers,
the response intentionally omits the terminal body and has fewer bytes than
its declared `Content-Length`; the client cannot treat it as a complete Range.
Uvicorn logs an incomplete-response error, and Edge can request another Range.
This is an HTTP framing limitation once headers are sent. Server counters
record interrupted ranges without exposing URLs, IPs, or credentials.

Only transient worker HTTP failures and timeouts get one retry, within the
same Range deadline and attempted-byte budget. Unsafe DNS/redirects and SHA-1
failures are not retried. The verified-piece cache still stores only complete
SHA-1-checked pieces. A worker process is killed on cancellation, including
blocked DNS. No P2P, tracker, or other music source was enabled.

In one repeat of the approved `amp021` Edge test, the first verified piece and
first HTTP Range block were ready in **13.61 s**. The first piece needed one
retry. Edge emitted `loadedmetadata` at **69.81 s** and `playing` at about
**69.89 s** on its page clock; the external wall clock measured **70.64 s**
from Play until `currentTime` advanced. The previous run took 55.234 s, so
startup was **15.406 s slower**, not faster, in this noisy live comparison.
Pause, Seek, and the next MP3 still played. One later Range ended incomplete
after two failed attempts; Edge retried at byte 5,242,880.

At 12 verified pieces (6,291,456 bytes), successful workers had accumulated
about 0.174 s DNS, 0.219 s TCP, 12.626 s TLS, 9.984 s waiting for HTTP
response headers, 11.859 s reading response bodies, and 0.016 s SHA-1. These
figures exclude failed-worker internals, process startup, HTTP Range writes,
and Edge decoding; they must not be subtracted from 70.64 s as exhaustive
time accounting. New server diagnostics also measure total worker-attempt
time, failed-attempt time, first Range preparation, and retry count for the
next run.

The first MP3 has an approximately 3.7 MiB ID3v2 tag. Edge did not report
`loadedmetadata` until enough sequentially fetched data passed that tag and
reached MP3 frames. At 512 KiB per piece, at least eight pieces (4 MiB) must
be checked to pass the tag; in this run Edge waited for roughly 12 pieces,
including extra buffering and a broken Range retry. Fresh HTTPS connections
and TLS handshakes for every piece contributed measurable time. The available
telemetry does not isolate an exact ID3-only delay or an Edge-only delay.

## Bounded connection reuse and adjacent-piece Ranges

The fetcher previously sent `Connection: close`, closed every TLS socket after
one piece, and launched a new worker for the next piece. In `safe_webseed`, the
first requested piece is still fetched alone. When playback continues to the
next piece, one worker fetches at most four pieces of the selected file: each
pair is one bounded HTTPS Range, and the second pair may reuse the first pair's
TLS connection. Every piece is checked against its own torrent SHA-1 both in
the worker and in the parent before it enters the 4 MiB verified cache or is
returned to HTTP. A seek starts with one piece again. A boundary piece may
include bytes from the adjacent file solely to verify its hash.

Reuse is limited to four requests on one TLS socket. The socket stays pinned
to the checked global IP and the same approved HTTPS hostname; its peer IP is
checked again before reuse. Redirect targets are validated and cached only
after a complete, exact `206` response. A replacement connection resolves and
checks every DNS answer again. The existing URL, port, path, redirect, TLS
certificate, response-length, time, attempted-byte, and process-cancellation
limits remain in force. No whole-album fetch or ID3 transformation was added.

One further Edge run used only the approved `amp021` HTTPS Web Seed and an
empty in-memory cache. The first verified piece and first Range were ready in
**3.078 s**; Edge reported `loadedmetadata` at **13.47 s** and `playing` at
**13.52 s**, with `currentTime` advancing after **14.266 s** from Play. The
first 13 verified pieces totaled 6,815,744 bytes and used eight new HTTPS
connections plus three reuse events. The prior run used 24 new connections
for its first 12 pieces. This is about 0.62 versus 2 new connections per
piece, a 69% reduction on that normalized measure. There were no worker
retries or incomplete Ranges in the optimized run. Pause, Seek, and switching
to the second MP3 passed in Edge; the second MP3 also reached `playing`.

Compared with the immediately preceding 70.640 s run, time to advancing
`currentTime` fell by **56.374 s** (about 80%). It was also **40.968 s** below
the earlier 55.234 s run. The previous run had transient Web Seed errors and
four retries, while this run had none; a single live comparison cannot assign
all of the improvement to connection reuse and batched Ranges. Browser media
events and time advancement confirm playback behavior, not acoustic output.
Thirty-two targeted fetcher, stream, HTTP Range, DNS-rebinding, and active
socket-cancellation tests passed. The one-off localhost server was stopped.

## Automatic Play in the ordinary MusicStream interface

`MUSICSTREAM_TORRENT_AUTO_ENABLED=true` now also works with
`MUSICSTREAM_TORRENT_NETWORK_MODE=safe_webseed`. The existing server-side Auto
Play path ranks releases, resolves torrent metadata, and uses TrackMatcher to
select an MP3 index. Before registering or downloading a Web Seed piece, it
checks that the resolved `.torrent` bytes match the file metadata and passes
the local torrent through the strict `SafeWebSeedFetcher` approval check. A
missing or unapproved torrent gives `unavailable`; Prowlarr results never add
info hashes to the allowlist. The client receives only an opaque local Range
URL. Switching tracks revokes that URL and closes the old verified-piece cache
and active worker, including while preparation is buffering. The existing
Hitmos path is unchanged.

For a single approved local album test, set the HTTP and Auto flags, the
network mode, the exact hash allowlist shown above, and
`MUSICSTREAM_TORRENT_AUTO_TEST_SOURCE` to the local `amp021.torrent`. Launch
the ordinary MusicStream app on `127.0.0.1` and open `/app`. This configuration
does not require a running Prowlarr instance; the existing Prowlarr path still
requires its separate approved source IDs and indexer IDs. No Web Seed mode is
enabled by default.

The limited browser exercise used the ordinary `/app` page and normal Play
button with locally supplied Deezer-shaped metadata for `Piscis — Nocturnal`.
It did not use a standalone audio test page or a live Deezer catalog query.
Play advanced the first MP3 after **14.797 s**. Pause and Seek worked, and
switching to `She Always Had a Great Loud Laugh` played the second MP3. An
absent title showed `unavailable` and disabled Play. Its first HTTP audio
response came from the opaque local Range endpoint backed by the approved
HTTPS Web Seed. The test did not establish that this Archive release appears
in the configured Prowlarr indexer or in Deezer's live catalog; those are
remaining requirements for an entirely live Deezer → Prowlarr discovery run.
Seventy-nine related Auto Play, matcher, Web Seed, cancellation, and Hitmos
tests passed.
