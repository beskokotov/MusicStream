# Local Prowlarr indexer priority

The local Auto Play server reads `MUSICSTREAM_TORRENT_INDEXER_PRIORITY` as a JSON
array of configured Prowlarr names. The launcher defaults to The Pirate Bay,
then NoNaMe Club, and accepts `-IndexerPriority` to change the order.
At startup MusicStream calls Prowlarr's local `/api/v1/indexer` endpoint and
resolves each enabled torrent indexer name to its current ID. It does not depend
on a permanent The Pirate Bay ID. The same resolved IDs restrict search and
metadata retrieval.

For each Play request, Auto Play searches the primary indexer for the artist and
album, then artist and track title. It ranks releases by artist, album, version,
format and seeders, fetches bounded metadata, and checks the actual MP3 or FLAC
file. A version mismatch cannot become eligible merely because its indexer is
first. If no suitable file yields verified audio within the primary's
60-second preparation window, Auto Play searches the next indexer. A two-source
request has a 120-second overall limit. The browser's Play flow is unchanged.

The Pirate Bay results may provide only an info hash. In that case the resolver
requests metadata through DHT for at most 15 seconds without requesting audio
pieces. It requires the existing info-hash allowlist before doing so unless the
server was already explicitly launched with catalog mode. Private torrents and
unverified file matches remain unavailable. This change adds no release or hash
to an allowlist. Search results alone do not prove that a torrent can deliver
metadata or verified audio.

The primary search resolves at most two top-ranked torrent file lists before
trying audio, preserving time for a verified range and the backup indexer.
NoNaMe Club retains the existing five-candidate limit. Live, remix, Deluxe,
anniversary, reissue and vinyl editions must match the Deezer version to be
eligible, regardless of indexer priority or reported seeders.

Server logs record the indexer name and ID, a bounded release title, file name,
selection stage and aggregate transfer counters. They omit API keys, download
links, tracker URLs and peer addresses.

On 2026-11-14, a live local Prowlarr search for `Rammstein Zeit` returned seven
The Pirate Bay results and six NoNaMe Club results. The configured IDs were 3
and 1 respectively at that time. With the final version check, the running
server searched The Pirate Bay first, found no eligible verified file among its
two leading results, then searched NoNaMe Club and matched `02 Zeit.mp3` at
confidence 1.0. That backup swarm did not supply verified audio before the
bounded timeout, so this live request ended `unavailable`. An earlier run had
received audio from a vinyl edition; that result motivated the stricter edition
check and is not counted as correct playback. Synthetic tests cover ID changes,
source filtering, wrong-version rejection and fallback after missing verified
bytes.
