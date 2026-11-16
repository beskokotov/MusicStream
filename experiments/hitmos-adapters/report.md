# Hitmoz / Hitmos.fm technical probe

## Scope

Independent parsers and a constrained metadata-only HTTP probe were created in
this directory. HomeMusic source code was not copied. No MusicStream routes,
Auto Play, Prowlarr, BitTorrent code, or running server were changed.

The probe is limited to `robots.txt` and public HTML search/song pages. It never
requests MP3/HLS bytes, never publishes a media URL, and never starts playback.
Media observations contain only presence, coarse type, and hostname. No
playback allowlist was created because rights and host approval remain
unverified.

## Network diagnosis and site rules

DNS resolution worked for `ru.hitmoz.org`, `rus.hitmos.fm`, and PyPI. Plain
outbound TCP initially failed with Windows `PermissionError` / `WinError 10013`
for all three hosts, so the first connection failures were caused by the
workspace's network permission boundary, not DNS, TLS, or a demonstrated host
outage. With the user-authorized elevated network run, package installation and
HTTPS access worked.

`ru.hitmoz.org/robots.txt` returned **HTTP 403**. Its rules could not be read,
so the probe fails closed and made no Hitmoz search request. This is an access
restriction response; no attempt was made to get around it.

`rus.hitmos.fm/robots.txt` returned **HTTP 200**. Its relevant directives were:

```text
User-agent: Yandex / Googlebot / *
Allow: */_next/*?*
Allow: */opengraph-image/*
Allow: */image?url=*
Disallow: /?gclid=
Disallow: /?_openstat=
Disallow: /*?*
Disallow: /auth
Disallow: /user
Disallow: */preload$

User-agent: ia_archiver
Disallow: /
```

The bare `/search` path is allowed, and `/song/7874240` is allowed. A search
request needs a query parameter, however, and `Disallow: /*?*` disallows that
full URL. The adapter now checks the complete query URL before fetching. The
site's search form names its query field `query`.

Several query-search GETs were sent before the full robots rule was discovered:
the earlier guard checked only the bare `/search` path. This was an execution
mistake. No audio or media URL was requested. Once the wildcard rule was found,
all query-search testing stopped and the guard was fixed and covered by tests.

During those earlier requests, a page showed the exact `Rammstein — Sonne`
record (ID `7874240`, 4:33) and a media field on `pl1.hitmos.fm`. That result
came from a robots-disallowed query request, so it is recorded as a technical
observation only and is not used for integration. `pl1.hitmos.fm` is not an
approved audio host. No MP3 bytes, redirect chain, MIME response, or Range were
checked. Site terms were not found/verified.

| Provider / case | robots | Search | Media | Playback |
|---|---|---|---|---|
| Hitmoz — Sonne, Mutter, Russian track | HTTP 403 on robots | Not reached | Not checked | Not attempted |
| Hitmos.fm — Sonne | HTTP 200; query URL disallowed | Query result observed before rule discovery | Host observed but not trusted | Not attempted |
| Hitmos.fm — Mutter, Russian track | HTTP 200; query URL disallowed | Query result not accepted as a compliant test | Not checked | Not attempted |
| Missing / wrong version / CDN error | Synthetic fixtures only | Parser/matcher checks | Unknown host stays unapproved | Not attempted |

## Synthetic checks

- Hitmos.fm RSC records with exact metadata are recognized and matched using
  MusicStream's existing artist/title/version/duration assessment.
- A synthetic `Mutter (Live)` record is rejected for the original `Mutter`.
- Missing/malformed RSC data returns no candidates without raising.
- Private, mixed public/private, invalid, and empty DNS answer sets are
  rejected; only the fixed metadata hosts are accepted.
- Media metadata exposes no URL/query string, and arbitrary media hosts remain
  unapproved.
- A `robots.txt` denial prevents a search request.

`selectolax 0.4.13` was installed into the project `.venv` from the existing
experiment requirements. The experiment tests passed (**16 passed, 1 skipped**;
the skipped case is only the no-selectolax error-path check). Python compilation
passed.

The full project suite ran with a workspace-local pytest temp path: **521 passed,
3 failed**. The three failures are in pre-existing torrent work and outside this
experiment: an undefined `track` in `torrent/http.py`, and two fake torrent
handles missing `file_progress()` in torrent tests. No torrent code was changed
for this task. The first full run used the system temp folder and hit 101
`PermissionError`s while pytest tried to scan it; rerunning with `--basetemp`
inside the workspace removed those environment errors.

## Outcome

The technical prototype includes fixed-origin HTTP, DNS-pinned TLS connections,
manual redirect validation, robots rules that account for query strings,
response/rate limits, Hitmoz `data-musmeta` parsing, song metadata inspection,
and Next.js RSC parsing. It is not connected to Play.

**No permitted automated search path was confirmed for either provider.**
Hitmoz's robots endpoint returns 403; Hitmos.fm disallows its query-based search
URL. Therefore no provider has been added to Auto Play. The existing Prowlarr
fallback remains untouched. No audio Range, browser playback, Pause, Seek,
switching, or long-duration playback was performed.
