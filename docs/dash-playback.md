# DASH/MSE playback

MusicStream's DASH adapter feeds an approved, clear DASH source into the existing
`#hitmos-audio` element through dash.js and Media Source Extensions. It does not
create another audio element or a server-side stream proxy.

## Source provider contract

A provider implements `getPlaybackSource()` and returns a descriptor containing:

- `manifestUrl`: HTTPS MPD URL;
- `allowedOrigins`: exact HTTPS origins permitted for the manifest and segments;
- optional display metadata such as `title`, `artist`, and `language`.

The adapter fetches the MPD without credentials, verifies redirects and declared
origins, rejects `ContentProtection` and video adaptations, and requires a
browser-supported `audio/mp4` AAC representation. dash.js request modification
blocks manifest or segment requests whose origin is not in the provider's list.
The provider is responsible for returning a source it is allowed to offer and for
including every origin the MPD legitimately uses.

The open BBC R&D radio sample is a manual Audio LAB source. A user loads it, then
uses MusicStream's shared Play/Pause, seek, and volume controls. dash.js handles
segment scheduling and its built-in retry path; a prolonged buffer stall triggers
a seek at the current position to resume segment fetching.

The pinned dash.js bundle is loaded from the DASH-IF CDN only when the open DASH
sample is requested. Browser playback requires network access to that bundle and
to the provider's CORS-enabled manifest and media origins.

VK remains a metadata-only provider until it exposes a supported source contract.
