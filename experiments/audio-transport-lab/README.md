# MusicStream test playback fixture

`src/musicstream/static/audio-transport-test.mp3` is a synthetic 440 Hz sine
tone generated locally from PCM with the installed LAME encoder DLL. It
contains no third-party recording. The fixture exists only to verify the
MusicStream byte-range endpoint and the existing `#hitmos-audio` element; it is
not a VK recording and does not demonstrate that VK permits external playback.

The route is disabled by default. Enable it for a local experiment with
`MUSICSTREAM_AUDIO_TRANSPORT_TEST=true`. Grants are random, process-local, and
expire after two minutes. No source URL, cookies, or credentials are involved.
