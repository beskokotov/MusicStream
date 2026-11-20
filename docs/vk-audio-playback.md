# VK DASH playback experiment

The experimental path is enabled only when both local environment flags are true:

```powershell
$env:MUSICSTREAM_VK_ENABLED = "true"
$env:MUSICSTREAM_VK_PLAYBACK_EXPERIMENTAL = "true"
uv run uvicorn musicstream.main:app
```

On Windows, do not use `--reload` for this VK experiment: Uvicorn selects a Selector event loop when reload subprocesses are enabled, while Playwright needs subprocess support. The safe startup diagnostic reports `NotImplementedError` as `chrome_launch_failed` in that configuration.

Auto Play includes VK only with the playback flag. VK candidates still pass through the common Matching Engine; an unknown duration does not qualify as a confirmed automatic match.

For a selected candidate, the server Reader searches the visible VK Audio UI, locates that exact `owner_audio` row, and clicks its normal Play control. The Reader keeps the ID-correlated clear AAC/DASH manifest in process memory. A short-lived local playback ID maps the shared DASH/MSE adapter to a rewritten manifest and proxied segments. The browser receives no VK manifest or segment URL, cookies, or access key. Requests are credential-free, restricted to the captured public HTTPS origins, checked across redirects, and require the source's CORS response to allow the MusicStream origin. DRM, dynamic MPD, unsupported URL templates, expired sources, denied CORS, and upstream HTTP errors return concrete failure reasons.

The DASH adapter attaches to the existing HTMLAudioElement; Pause, Seek, and Volume stay on the existing controls. With either flag disabled, the VK playback and Auto Play path remains off. Hitmos behavior is unchanged.

The current Uvicorn process must be restarted manually for the flags and code to take effect. Restarting also discards the in-memory VK browser session and captured resources, so sign in in the new Reader window when prompted.
