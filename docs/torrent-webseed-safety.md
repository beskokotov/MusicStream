# Web Seed safety assessment — 2026-11-13

`public_webseed` is **not enabled**. The existing modes and default startup
behavior are unchanged. This is a deliberate fail-closed result of the URL
validation requirement, not a claim that libtorrent cannot download web seeds.

## Evidence from the approved test torrent

The local `amp021.torrent` matches the single approved v1 info hash
`3191c92459c15c0aac4a79ff3e2b7c7914eb3922` and has no `private` flag.
libtorrent 2.1.1.0 loads these GetRight-style URL seeds:

| URL in torrent | Decision before any session |
| --- | --- |
| `https://archive.org/download/` | HTTPS and an Archive hostname, but it redirects |
| `http://ia902708.us.archive.org/34/items/` | Reject: plain HTTP |
| `/34/items/` | Reject: relative URL |

A headers-only `HEAD` to the first MP3 path on `archive.org` returned HTTP 302
with `Location: https://dn801706.us.archive.org/0/items/amp021/1_piscis_nocturnal.mp3`.
The probe used TLS certificate validation through the existing local SOCKS5
proxy. It did not fetch audio. The redirect host is an Archive host in this
sample, but redirects and DNS answers can change on subsequent requests.

[Libtorrent's security audit](https://github.com/arvidn/libtorrent/blob/RC_2_1/docs/security-audit.rst)
describes its SSRF mitigation: a web seed resolved to a global address cannot
redirect to a non-global IP. It also documents that web seeds may redirect.
The mitigation does not enforce MusicStream's required hostname allowlist on
every redirect or pin every DNS resolution. [Libtorrent's web seed behavior](https://github.com/arvidn/libtorrent/issues/7325)
can add a redirected URL as a new web seed. Checking only the `.torrent` URLs,
or prechecking the first redirect, therefore leaves a time-of-check/time-of-use
gap and permits later redirects to other global hosts. IP filtering alone
cannot establish the required hostname policy on shared hosting.

## Required design for a later iteration

Use a separate controlled HTTP Range fetcher for Web Seeds. Before every
connection and redirect hop, it should validate the full URL (HTTPS, exact
approved Archive host policy, no credentials or tokens, allowed port and path),
resolve the hostname, reject all non-global addresses including cloud metadata
endpoints, and bind the request to the validated address while preserving TLS
hostname verification. It should cap redirect hops, connection count, request
time, range size, total bytes, and cache usage. Download complete torrent
pieces, verify their hashes against the approved `.torrent`, and expose bytes
to the existing HTTP Range reader only after verification. A network-isolated
helper process with an egress allowlist would provide a stronger boundary.

No `public_webseed` network test, HTTP Range playback, Play, Pause, or Seek was
run because the required URL boundary is not available in the current
libtorrent integration. No MP3 bytes were downloaded in this assessment.

## Follow-up: one controlled piece

The separate [SafeWebSeedFetcher](../src/musicstream/torrent/safe_webseed.py)
now implements the bounded HTTP Range fetch described above for an explicitly
approved v1 torrent. It does not use libtorrent's Web Seed network stack or
create a BitTorrent session. The initial URL is exactly
`https://archive.org/download/`; the two other `amp021` URL seeds are rejected.
Redirects are handled manually. Only `archive.org` and Archive download-node
hostnames matching `dn[0-9]{6}.(us|ca).archive.org` are permitted, with the
path confined to the requested file under `amp021`. Every hop resolves all
DNS answers, rejects the hop if any answer is non-global, connects to a checked
literal IP on port 443, and verifies TLS with the hostname as SNI. No cookies,
authorization headers, URL credentials, or torrent tracker URLs are sent.

Each response must be HTTP 206 with the exact `Content-Range`, file length,
and body length. The fetcher limits a piece to 1 MiB by default, three
redirects, eight connections, and 15 seconds. It assembles a piece across file
boundaries, including zero-filled torrent padding files, and returns it only
after matching the v1 SHA-1 stored in the `.torrent`. No cache is retained by
this component. Ten isolated tests cover crossing a file boundary, redirects,
DNS rebinding at a redirect, an unexpected connected IP, incorrect ranges,
truncated responses, a wrong hash, and rejection of unsafe seeds.

One live request used only the approved `amp021` hash, with a 512 KiB piece
limit and 30-second deadline. The HTTPS origin returned one redirect to
`dn710700.ca.archive.org`. Two TLS connections were made; the fetcher read
524,288 body bytes, verified piece 0 against the torrent SHA-1, and reported
zero HTTP errors. The verified piece was ready after 14.953 seconds. This was
HTTP Web Seed traffic, not BitTorrent peer traffic: the component has no P2P,
DHT, tracker, or libtorrent session and held no local audio cache.

The fetcher is intentionally not wired to `TorrentSessionManager`, HTTP Range,
or Play. Browser playback, Seek, and time to audible audio remain untested.
The synchronous system DNS resolver can take longer than the configured
request deadline before returning; a future streaming adapter should provide
a separately bounded resolver and cancellation path. The narrow download-node
hostname pattern may reject a legitimate future Archive redirect, which is a
safe failure requiring policy review.

The later [opt-in HTTP Range playback iteration](torrent-safe-webseed-playback.md)
adds a killable worker process and a verified-piece memory cache. The
standalone assessment above predates that integration.
