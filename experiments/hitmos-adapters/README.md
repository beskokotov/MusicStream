# Hitmoz / Hitmos.fm adapter research

This directory contains independent parser prototypes. It is separate from
MusicStream's routes, Auto Play, and running server.

The adapters:

- fetch only fixed-origin HTML pages over HTTPS;
- connect to DNS-checked public IPs while retaining hostname TLS verification;
- limit each HTML response to 512 KiB, redirects to three, and requests to one
  per second per provider;
- parse provider metadata and report only media presence, coarse type, and host;
- do not fetch audio bytes, return media URLs, or start playback.

The adapters do not request or interpret `robots.txt`. This is separate from
network safety: requests remain limited to fixed HTTPS origins, public DNS
addresses, checked redirects, bounded response sizes, and a low request rate.
The current site terms remain unverified. The probe does not attempt to bypass
CAPTCHA, Cloudflare, authentication, or DRM.

Install the experiment dependencies into a disposable environment:

```powershell
python -m pip install -r experiments/hitmos-adapters/requirements.txt
$env:PYTHONPATH = "src"
python -m pytest experiments/hitmos-adapters/tests
python experiments/hitmos-adapters/probe_sources.py --run-live
```

The live probe checks three metadata searches per site and one Hitmoz song
metadata page for a likely match. It never follows an unapproved hostname or
requests an MP3/HLS body. A discovered media host is deliberately not treated
as trusted; playback integration requires separate rights and allowlist review.
