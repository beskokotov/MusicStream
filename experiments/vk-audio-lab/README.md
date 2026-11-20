# VK Audio LAB

The V2 metadata reader lives in `src/musicstream/audio_providers/vk.py` and
`vk_browser.py`. It reads visible result rows in VK's ordinary audio interface,
keeps only rows with a structured `owner_audio` identity, and passes them to
MusicStream's existing Matching Engine. It does not resolve or play streams.

Run the four checks with:

```powershell
.venv\Scripts\python.exe experiments\vk-audio-lab\search_vk.py
```

The script opens Chrome visibly, uses an in-memory browser context, and asks the
user to sign in through VK if needed. Closing the run discards that context. It
does not export cookies, browser storage, request bodies, media URLs, or audio.
The browser may require the user's local network to allow `vk.com`.

`observe_vk.py` is the earlier V1 investigation utility. Its old dedicated
persistent-profile workflow is not used by the V2 reader.
