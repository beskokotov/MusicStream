import asyncio
import html
import json

import pytest

from musicstream.audio_providers.base import RecordingReference, SourceDocument
from musicstream.audio_providers.hitmos import HitmosProvider, valid_stream_url
from musicstream.audio_providers.hitmos_browser import redirect_target, request_action

ID = "81423717"
PATH = f"/get/cuts/ab/cd/{'a' * 32}/{ID}/Artist_-_Song_b128.mp3"
PLAYER = "https://ru.hitmox.org" + PATH
CDN = "https://cdn13.deliciousoranges.com/s1" + PATH
REF = RecordingReference(title="Song", artist="Artist", duration_seconds=219)


def document():
    metadata = {"@type":"MusicRecording", "@id":f"https://ru.hitmox.org/song/{ID}",
                "name":"Song", "byArtist":[{"name":"Artist"}], "duration":"P0DT0H3M39S"}
    player = {"id":f"track-id-{ID}", "url":PLAYER}
    return (f'<script type="application/ld+json">{json.dumps(metadata)}</script>'
            f'<div data-musid="track-id-{ID}" data-musmeta="{html.escape(json.dumps(player),quote=True)}"></div>'
            '<!-- internal-document-not-for-client -->')


class Reader:
    def __init__(self, url=CDN, reason=None):
        self.url = url
        self.reason = reason

    async def read_song(self, song_id):
        assert song_id == ID
        return SourceDocument(document(), self.url, self.reason)


def action(url, resource="Document", method="GET", frame="main", player=PLAYER, started=False):
    return request_action(url, resource, method, frame, "main", ID, player, started)


def test_resolved_cdn_is_returned_without_html_or_first_hop():
    result = asyncio.run(HitmosProvider(Reader()).lookup(REF, ID))
    assert result.status == "available"
    assert result.candidates[0].method == "html_audio"
    assert result.candidates[0].stream_url == CDN
    assert PLAYER not in result.model_dump_json()
    assert "internal-document-not-for-client" not in result.model_dump_json()


@pytest.mark.parametrize("url", [None, PLAYER, "https://127.0.0.1/private", CDN.replace(ID,"1"),
    CDN.replace("cdn13.deliciousoranges.com","cdn15.deliciousoranges.com"), CDN + "?token=private"])
def test_failed_or_untrusted_resolution_does_not_fall_back_to_first_hop(url):
    result = asyncio.run(HitmosProvider(Reader(url, "stream_redirect_not_allowed")).lookup(REF, ID))
    assert result.status == "external_player"
    assert result.reason == "stream_redirect_not_allowed"
    assert result.candidates[0].stream_url is None


def test_only_document_and_single_player_request_are_allowed():
    assert action("https://ru.hitmox.org/unapproved") == "block"
    assert action(f"https://ru.hitmox.org/song/{ID}") == "document"
    assert action(PLAYER, "Media") == "player"
    assert action(PLAYER, "Media", started=True) == "block"


@pytest.mark.parametrize(("url", "resource", "method", "frame"), [
    (CDN,"Media","GET","main"), (CDN,"Document","GET","main"),
    ("http://127.0.0.1/private","Document","GET","main"),
    ("http://169.254.169.254/latest/meta-data","Document","GET","main"),
    (f"https://ru.hitmox.org/song/{ID}","Document","POST","main"),
    (f"https://ru.hitmox.org/song/{ID}","Document","GET","iframe"),
    (PLAYER,"Fetch","GET","main"), (PLAYER,"Media","POST","main"),
    ("https://ru.hitmox.org/search?q=Song","Document","GET","main"),
    ("https://ru.hitmox.org/static/player.js","Script","GET","main"),
])
def test_browser_request_gate_blocks_ssrf_subframes_assets_and_cdn_networking(url, resource, method, frame):
    assert action(url, resource, method, frame) == "block"
    assert action(url, resource, method, frame, started=True) == "block"


@pytest.mark.parametrize("status", [301,302,303,307,308])
def test_source_issued_cdn_redirect_is_validated(status):
    assert redirect_target(status,[{"name":"Location","value":CDN}],PLAYER,ID) == CDN
    assert valid_stream_url(CDN,ID)


@pytest.mark.parametrize("location", ["/song/81423717", "http://127.0.0.1/private",
    "https://169.254.169.254/latest/meta-data", "https://untrusted.invalid/file.mp3",
    CDN.replace("cdn13.deliciousoranges.com","cdn13.deliciousoranges.com.evil.invalid"),
    CDN.replace("cdn13.deliciousoranges.com","user:secret@cdn13.deliciousoranges.com"),
    CDN.replace("cdn13.deliciousoranges.com","cdn13.deliciousoranges.com:8443"),
    CDN.replace("https:","http:"), CDN.replace(ID,"1"), CDN+"?token=private", CDN+"#fragment"])
def test_redirect_does_not_authorize_other_destinations(location):
    assert redirect_target(302,[{"name":"location","value":location}],PLAYER,ID) is None


@pytest.mark.parametrize("status", [200,206,401,403,404,500])
def test_non_redirect_responses_never_authorize_audio_body_fetch(status):
    assert redirect_target(status,[{"name":"Location","value":CDN}],PLAYER,ID) is None


def test_duplicate_locations_rejected():
    assert redirect_target(302,[{"name":"Location","value":CDN},{"name":"Location","value":CDN}],PLAYER,ID) is None


def test_cdn_requests_remain_blocked_after_first_hop():
    assert action(CDN,"Media",started=True) == "block"
