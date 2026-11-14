# NoNaMe Club metadata check — 2026-11-14

One bounded query term, `Rammstein Mutter`, used the existing
`ProwlarrDiscoveryProvider` against local Prowlarr 2.7.0 and the enabled
NoNaMe Club indexer (ID 1). It returned five music releases. The JSON carried
titles, categories, sizes, seeder counts, information-page links, and local
Prowlarr `/1/download` routes. None of these five rows carried a magnet URI
or info hash. No URL or API key was sent to the browser or written here.

The chosen result was **Rammstein — Mutter (2001), FLAC tracks + cue**, in
`Audio/Lossless`, approximately 373,616,064 bytes with eight reported
seeders. The same query was sent twice to the Prowlarr API: first to inspect
the results, then to pass the chosen result directly to
`TorrentMetadataResolver`. No different music query or audio download
followed. The resolver accepted the local Prowlarr route and
retrieved a 16,373-byte `.torrent` without following an external redirect.

The torrent metadata contained 28 files, including 11 separate FLAC tracks.
It has a v1 info hash, five tracker entries, no Web Seeds, and no `private`
flag. `TrackMatcher` matched the composition `Mutter` to file index **5**,
`06 Mutter.flac` (34,496,471 bytes), with confidence **0.88**. File indices
were read from torrent metadata, not inferred from track numbers.

This confirms `Prowlarr result → local .torrent route → file list → matched
track`. It does **not** confirm audio availability. The search seeder count is
not a verified live P2P connection. `safe_webseed` cannot serve this release
because it has no Web Seeds and is outside its fixed approved hash. The
existing `public_discovery` mode could only be considered after separate
rights and info-hash approval; no hash was added to its allowlist here. Auto
Play currently prepares MP3 only, so this FLAC result would also be skipped
by that path. No BitTorrent session or audio request was started.
