# First public discovery network check — 2026-11-13

Source: [Piscis — Nocturnal, AMP Records, Internet Archive](https://archive.org/details/amp021).
The [Archive metadata](https://archive.org/metadata/amp021) lists the AMP Records
uploader, original MP3 files, a `.torrent`, and the
[CC BY-NC-ND 2.5 MX license](https://creativecommons.org/licenses/by-nc-nd/2.5/mx/).
This was a limited, noncommercial test; no audio file was redistributed.

The 9,737-byte `.torrent` has six MP3 files, two Internet Archive trackers,
three web seeds, and no `private` flag. Only its v1 info hash
`3191c92459c15c0aac4a79ff3e2b7c7914eb3922` was allowed. The local copy
and single-hash allowlist are under ignored `test_media/public_discovery/`.
MusicStream's public mode disabled the web seeds, so HTTP audio could not be
mistaken for BitTorrent payload. A fresh temporary cache was empty before the
session. No local seeder was started.

The first attempt requested bytes 0–16383 of the first MP3, waited 60 seconds,
and timed out. During that time libtorrent listed two peers and had up to two
connections, but `total_payload_download` remained zero. A second, 20-second
diagnostic of the same torrent confirmed that 1–2 connections were to
non-loopback IPs and `num_seeds` stayed zero. It recorded four tracker errors
with code 180. Libtorrent defines this as
[`announce_skipped`](https://libtorrent.org/reference-Error_Codes.html): the
tracker announce was skipped because the source interface was considered
unreachable. The exact network-interface cause remains unverified.

| Measurement | Result |
| --- | --- |
| Discovered peers | 2 |
| Connected remote peers | 1–2 |
| Connected seeds | 0 |
| BitTorrent payload bytes | 0 |
| Verified requested-piece bytes | 0 |
| First ready audio fragment | Not obtained within 60 seconds |
| Browser Play/Pause/Seek | Not run; no verified audio was available |
| Time to playback | Not measurable |

No audio bytes were downloaded from this or another public torrent. The
temporary cache and libtorrent session were closed after each attempt. The
existing local HTTP Range and browser tests remain the only playback proof.
Nine targeted public-discovery and HTTP Range tests passed after this check.

## Network configuration diagnosis and one retest

The original `listen_interfaces=0.0.0.0:0` expanded into sockets on the tunnel,
Ethernet, and loopback IPv4 interfaces, with different ephemeral ports.
`outgoing_interfaces` was empty, so Windows chose the route. In a read-only
socket check, the tunnel was the preferred default route. The previous alert
recorded error 180 but not its `local_endpoint`, so its precise socket cannot
be proven retrospectively. It is consistent with libtorrent skipping announces
from an interface it considers unable to reach the public tracker.

`public_discovery` now requires one explicit local IPv4 bind address and uses
it for both `listen_interfaces` and `outgoing_interfaces`. The retest selected
the IPv4 address of the routed tunnel without changing any adapter settings.
The session listened on one ephemeral port, 53720. No peer or private local IP
address was written to the application log.

The retest used the same approved hash, an empty cache, disabled web seeds,
four connection slots, and one 60-second request for the first MP3 range:

| Measurement | Retest result |
| --- | --- |
| Successful tracker replies / errors | 1 / 0 |
| DHT bootstrap / nodes | Complete / up to 30 |
| Discovered / connected peers | 2 / up to 2 |
| Connected seed with requested piece | 1 at several samples |
| Completed handshake | 1 at several samples |
| Unchoked peers with requested piece | 0 |
| BitTorrent payload / verified bytes | 0 / 0 |
| First audio range | Timeout after 60 seconds |

This confirms that tracker announces and DHT discovery work with the selected
interface. It also separates network discovery from data availability: the
connected seed advertised the requested piece but kept this client choked, and
the connection intermittently dropped. The exact remote seed policy is not
observable from MusicStream. No MP3 bytes or browser playback were obtained.

To rerun this exact local-only test setup, use the scripts in
`test_media/public_discovery/` after reviewing the license and confirming that
this torrent remains approved. They request only the first and middle ranges of
the first MP3 and print aggregate counters without peer addresses. The
allowlist is never enabled at normal FastAPI startup.

## Follow-up: peer interest and block lifecycle

`TorrentDiagnostics` now reports the number of peers with the requested piece
that see us as `interested`, the number of those peers that still choke us,
connections awaiting handshake, queued and sent block requests, finished
blocks, timed-out requests, and dropped requests. These are aggregate server
counters; no peer addresses, tracker URLs, or credentials are recorded. The
requested piece retains priority 7 during `read_range`; priorities for other
pieces stay zero. A completed piece is still required before data is returned.

A second small CC-licensed candidate was [ST — I'll Meet You There EP
(`mtk140`)](https://archive.org/details/mtk140), with three original MP3 files
and a public `.torrent` (v1 hash
`c466eca67a9cab007414d42dd283a61d61a620f1`). The [Archive metadata](https://archive.org/metadata/mtk140)
lists CC BY-NC-ND 2.0. Historic download counts did not establish live seeder
availability; Archive tracker scrape responses contained no swarm statistics.
The live probe was therefore also a peer availability check, not a claim of
confirmed active seeds.

For `mtk140`, the single-hash test used an empty cache, disabled all webseeds,
and requested only bytes 0–16383 of the first MP3 for 60 seconds. Piece 0 had
priority 7. The Archive tracker replied once without error, DHT bootstrapped
to 91 nodes, and libtorrent discovered five peers. Up to four connection
attempts were present, but none completed a BitTorrent handshake during the
samples. There were no connected seeds, sent or queued block requests, payload
bytes, or verified pieces. The range timed out. A 35-second follow-up of the
previously approved `amp021` hash also had a successful tracker reply and DHT
bootstrap, but no stable completed handshake and zero payload bytes.

The earlier `amp021` sample showed a remote seed with the wanted piece while
`remote_choked` was set. It did not record the client's `interesting` flag, so
that historical state cannot prove the seed received an interest message.
In the new probes, no peer reached a state where `interested` or block request
behavior could be observed. Thus the current evidence separates a working
tracker/DHT path and correct piece priority from unavailable peer payload; it
does not isolate the original remote seed's policy. HTTP Range playback, Play,
and Seek were not run because no verified audio became available. First-byte
latency is not measurable. Thirteen targeted public-discovery, session, and
HTTP Range tests passed; the live probes themselves timed out cleanly and
removed their temporary caches.

These measurements concern P2P `public_discovery` only. A later, separate
[safe HTTPS Web Seed playback test](torrent-safe-webseed-playback.md) obtained
verified pieces and played `amp021` in Edge without any BitTorrent peers.

## Outgoing uTP/PEX configuration and bounded follow-up

The prior public configuration permitted outgoing TCP only, disabled PEX, and
used four connection slots in the app. It also disabled all incoming peer
connections. libtorrent 2.1.1.0 defaults to supporting outgoing uTP; the public
mode now permits outgoing TCP and uTP plus PEX, while retaining the selected
IPv4 interface, disabled incoming connections, disabled web seeds, hash checks,
one concurrent torrent, and bounded memory and disk usage. The app allows up to
12 connections in this opt-in mode. This removes three possible limitations;
it does not prove they caused the earlier remote choke or handshake failures.

An original MP3 with a local HTTP tracker and local seeder passed a public-mode
test: the tracker provided the peer, a BitTorrent connection delivered a piece,
and libtorrent verified it before returning the requested range. DHT was
disabled only in this local fixture to avoid announcing test metadata outside
the machine. This is a local transport check, not a remote P2P success.

One follow-up on the previously approved `amp021` hash used the routed local
IPv4 address, an empty temporary cache, no web seeds, 12 connection slots,
and a single 16 KiB range request. It timed out after 45 seconds. The final
sample occurred at about 50 seconds because diagnostics sampled every 5 seconds.
It observed zero tracker replies, four tracker errors, zero DHT nodes, zero
discovered or connected peers, zero payload bytes, and zero verified pieces.
The selected local route matched the route to the Archive tracker; the tracker
error code was not captured in this probe, so the immediate tracker failure
cannot be assigned to a specific network or source condition. No peer reached
handshake, interested, or block request state. The cache and session were
removed. No remote BitTorrent audio or browser playback was obtained.
