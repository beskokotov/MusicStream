# VK Audio in Audio LAB

VK Audio is an optional, metadata-only source. Enable it for a local run by
setting `MUSICSTREAM_VK_ENABLED=true` in the MusicStream process environment,
then restart the app. The source appears in the Audio LAB selector only when the
backend reports that it is enabled. Auto Play continues to use its existing
sources.

The first VK lookup opens a visible Chrome window. Sign in through VK itself if
needed, return to MusicStream, and retry. The browser context exists only in
process memory and is discarded when MusicStream exits; the app does not read,
write, or copy credentials, cookies, or browser storage. If automatic extraction
does not see rows, search in the open VK Audio window and use **Проверить видимые
результаты VK** in MusicStream.

Run MusicStream in the same interactive Windows user session where you can see
Chrome. Do not start this local experiment as Administrator or as a background
service; an elevated/background process may open its browser in another desktop.

MusicStream shows VK IDs, visible duration, the shared Matching Engine result,
and a link to the matching VK page. Missing duration remains unknown and cannot
confirm a match. VK candidates use `playback_unavailable`; their link opens the
standard VK player. The VKCOM API object schema documents `url` and
`access_key` fields, but does not by itself establish a permitted public method
for handing audio to an external player. MusicStream therefore does not extract
or transport VK media URLs. This integration does not add a stream proxy or an
audio player.
