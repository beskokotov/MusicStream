from __future__ import annotations

import asyncio
import time

import pytest

from musicstream.audio_providers.vk_browser import VKAudioBrowserReader, VKResourceDescriptor
from musicstream.audio_providers.vk_playback import VKPlaybackAdapter


def test_progressive_mp3_uses_short_local_lease_and_range_transport(monkeypatch):
    async def run():
        adapter = VKPlaybackAdapter()
        descriptor = VKResourceDescriptor(vk_audio_id="-1_2", media_type="audio/mpeg", codec="mp3",
            manifest_url="", media_url="https://cdn.example/audio.mp3?temporary=redacted",
            segment_origins=("https://cdn.example",), detected_at=time.monotonic(),
            expires_at=time.monotonic() + 60)
        grant = await adapter.prepare(descriptor)
        assert grant["mode"] == "progressive_audio"
        assert grant["resource_url"].startswith("/api/audio/vk/playback/")
        token = grant["playback_id"]
        upstream_url, origins, origin = adapter.upstream_url(token, "0", "", {})
        assert upstream_url == descriptor.media_url
        assert origins == descriptor.segment_origins
        assert origin == ""

        seen = {}

        async def open_upstream(url, allowed_origins, musicstream_origin, method, range_header, *, require_cors=True):
            seen.update(url=url, origins=allowed_origins, method=method,
                        range_header=range_header, require_cors=require_cors)
            return "client", "response"

        monkeypatch.setattr(adapter, "open_upstream", open_upstream)
        assert await adapter.open_resource(token, "0", "", {}, "GET", "bytes=1024-") == ("client", "response")
        assert seen == {"url": descriptor.media_url, "origins": descriptor.segment_origins,
                        "method": "GET", "range_header": "bytes=1024-", "require_cors": False}
    asyncio.run(run())


def test_progressive_mp3_rejects_non_audio_and_expired_resource():
    async def run():
        adapter = VKPlaybackAdapter()
        descriptor = VKResourceDescriptor(vk_audio_id="-1_2", media_type="video/mp4", codec=None,
            manifest_url="", media_url="https://cdn.example/media", segment_origins=("https://cdn.example",),
            expires_at=time.monotonic() + 60)
        with pytest.raises(ValueError, match="vk_progressive_resource_invalid"):
            await adapter.prepare(descriptor)

        expired = VKResourceDescriptor(vk_audio_id="-1_2", media_type="audio/mpeg", codec="mp3",
            manifest_url="", media_url="https://cdn.example/audio.mp3", segment_origins=("https://cdn.example",),
            expires_at=time.monotonic() - 1)
        with pytest.raises(ValueError, match="vk_source_expired"):
            await adapter.prepare(expired)
    asyncio.run(run())


def test_mp3_descriptor_requires_exact_id_linked_media_request():
    reader = VKAudioBrowserReader()
    url = "https://cdn.example/audio.mp3?temporary=redacted"
    reader._resource_capture = {"vk_audio_id": "-1_2", "play_action_sequence": 17,
        "expected_media_urls": {url}, "musicstream_origin": "http://127.0.0.1",
        "reason": "id_correlated_play_request_seen"}
    reader._resource_descriptors = {}
    response = {"url": url, "mimeType": "audio/mpeg", "status": 206,
        "headers": {"Accept-Ranges": "bytes", "Content-Range": "bytes 0-99/100"}}
    wrong_request = {"_capture_audio_id": "-1_3", "_capture_action_sequence": 17,
        "_capture_url": url, "request_cookie_present": False,
        "request_authorization_present": False}
    reader._capture_progressive_media_response("wrong", wrong_request, response)
    assert "descriptor" not in reader._resource_capture

    request = {"_capture_audio_id": "-1_2", "_capture_action_sequence": 17,
        "_capture_url": url, "request_cookie_present": False,
        "request_authorization_present": False}
    reader._capture_progressive_media_response("exact", request, response)
    descriptor = reader.get_resource_descriptor("-1_2")
    assert descriptor is not None
    assert descriptor.media_type == "audio/mpeg"
    assert descriptor.segment_origins == ("https://cdn.example",)
    assert reader._resource_capture["reason"] == "descriptor_ready"
