import asyncio
import html
import json

import pytest

from musicstream.audio_providers.base import RecordingReference
from musicstream.audio_providers.hitmos import HitmosProvider, parse_stream_url, valid_stream_url

ID = "81423717"
URL = f"https://ru.hitmox.org/get/cuts/ab/cd/{'a' * 32}/{ID}/Artist_-_Song_b128.mp3"
REF = RecordingReference(title="Song", artist="Artist", duration_seconds=219)


def page(url=URL, entry_id=ID, parent_id=ID, title="Song"):
    data = {"@type": "MusicRecording", "@id": f"https://ru.hitmox.org/song/{ID}",
            "name": title, "byArtist": [{"name":"Artist"}], "duration":"P0DT0H3M39S"}
    entry = {"id": f"track-id-{entry_id}", "url": url, "title": title, "artist":"Artist"}
    return (f'<script type="application/ld+json">{json.dumps(data)}</script>'
            f'<div data-musid="track-id-{parent_id}" data-musmeta="{html.escape(json.dumps(entry), quote=True)}"></div>')


class Reader:
    def __init__(self, document):
        self.document = document

    async def read_song(self, song_id):
        assert song_id == ID
        return self.document


def test_public_player_url_is_parsed():
    assert parse_stream_url(page(), ID) == URL


def test_matching_source_can_offer_html_audio_without_server_audio_fetch():
    result = asyncio.run(HitmosProvider(Reader(page())).lookup(REF, ID))
    assert result.status == "available"
    assert result.reason == "public_stream_found"
    assert result.candidates[0].method == "html_audio"
    assert result.candidates[0].stream_url == URL


def test_different_recording_does_not_offer_direct_audio():
    result = asyncio.run(HitmosProvider(Reader(page(title="Song Live"))).lookup(REF, ID))
    assert result.status == "external_player"
    assert result.candidates[0].stream_url is None
    assert result.candidates[0].match.status == "mismatch"


@pytest.mark.parametrize(("entry", "parent"), [("1", ID), (ID, "1"), ("1", "1")])
def test_other_tracks_on_page_are_not_selected(entry, parent):
    assert parse_stream_url(page(entry_id=entry, parent_id=parent), ID) is None


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/audio.mp3", URL.replace("https:", "http:"),
    URL.replace("ru.hitmox.org", "ru.hitmox.org.evil.invalid"),
    URL.replace("ru.hitmox.org", "user:password@ru.hitmox.org"),
    URL.replace("ru.hitmox.org", "ru.hitmox.org:8443"),
    URL.replace(f"/{ID}/", "/1/"), URL + "?url=http://localhost", URL + "#fragment",
    URL.replace("/get/cuts/", "/get/music/"), URL.replace("Artist_-_Song_b128.mp3", "../private.mp3"),
    "javascript:alert(1)", None,
])
def test_stream_destination_allowlist(url):
    assert not valid_stream_url(url, ID)
    assert parse_stream_url(page(url=url), ID) is None


def test_download_link_alone_is_not_a_player_source():
    assert parse_stream_url(f'<a href="{URL}">download</a>', ID) is None


def test_broken_attribute_is_ignored():
    assert parse_stream_url(f'<div data-musid="track-id-{ID}" data-musmeta="invalid"></div>', ID) is None
