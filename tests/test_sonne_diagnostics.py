import asyncio
import html
import json

import pytest
from fastapi.testclient import TestClient

from musicstream.audio_providers.base import RecordingReference, ResolutionDiagnostics, SourceDocument
from musicstream.audio_providers.hitmos import HitmosProvider, parse_stream_url, valid_stream_url
from musicstream.audio_providers.hitmos_browser import redirect_target, request_action
from musicstream.audio_providers.matching import assess
from musicstream.audio_providers.transport import SourceFailure
from musicstream.main import app as local_app
from musicstream.public_api import app as public_app

REF = RecordingReference(title="Sonne",artist="Rammstein",duration_seconds=272)


def cut_path(song_id):
    return f"/get/cuts/ab/cd/{'a'*32}/{song_id}/Rammstein_-_Sonne_b128.mp3"


def music_path(song_id):
    return f"/get/music/20251120/Rammstein_-_Sonne_{song_id}.mp3"


CASES = [
    ("57658982", "https://ru.hitmox.org" + cut_path("57658982"), "https://s4.deliciouspeaches.com" + cut_path("57658982")),
    ("79306580", "https://ru.hitmox.org" + cut_path("79306580"), "https://cdn14.deliciousoranges.com/s1" + cut_path("79306580")),
    ("80274130", music_path("80274130"), "https://cdn11.deliciousoranges.com/s1" + music_path("80274130")),
]


def document(song_id="57658982", player=None, title="Sonne", duration="P0DT0H4M32S"):
    metadata={"@type":"MusicRecording","@id":f"https://ru.hitmox.org/song/{song_id}",
              "name":title,"byArtist":[{"name":"Rammstein"}],"duration":duration}
    entry={"id":f"track-id-{song_id}","url":player or "https://ru.hitmox.org"+cut_path(song_id)}
    return (f'<script type="application/ld+json">{json.dumps(metadata)}</script>'
            f'<div data-musid="track-id-{song_id}" data-musmeta="{html.escape(json.dumps(entry),quote=True)}"></div>')


class Reader:
    def __init__(self, value): self.value=value
    async def read_song(self, song_id):
        if isinstance(self.value, Exception): raise self.value
        return self.value


@pytest.mark.parametrize(("song_id","player","cdn"),CASES)
def test_three_verified_sonne_formats_and_cdns(song_id,player,cdn):
    parsed=parse_stream_url(document(song_id,player),song_id)
    assert parsed == ("https://ru.hitmox.org"+player if player.startswith('/') else player)
    assert valid_stream_url(parsed,song_id)
    assert redirect_target(302,[{"name":"Location","value":cdn}],parsed,song_id)==cdn
    diag=ResolutionDiagnostics(metadata_status=200,redirect_status=302,redirect_valid=True)
    result=asyncio.run(HitmosProvider(Reader(SourceDocument(document(song_id,player),cdn,None,diag))).lookup(REF,song_id))
    assert result.status=="available"
    assert result.stage=="stream_resolution"
    assert result.candidates[0].match.status=="likely"
    assert result.candidates[0].match.reasons==[]
    assert result.candidates[0].match.comparison.duration_difference_seconds==0
    assert result.candidates[0].match.comparison.duration_tolerance_seconds==pytest.approx(8.16)


@pytest.mark.parametrize("reason",["stream_not_found","stream_redirect_not_allowed","stream_redirect_unavailable","stream_response_unexpected"])
def test_matching_recording_with_missing_address_is_not_called_mismatch(reason):
    value=SourceDocument(document(),None,reason)
    result=asyncio.run(HitmosProvider(Reader(value)).lookup(REF,"57658982"))
    assert result.stage=="stream_resolution"
    assert result.reason==reason
    assert result.candidates[0].match.status=="likely"
    assert result.candidates[0].match.comparison.duration_difference_seconds==0
    assert "запись не совпадает" not in result.message.lower()


@pytest.mark.parametrize(("title","duration","reason"),[
    ("Sonne Live","P0DT0H4M32S","version_differs"),
    ("Sonne","P0DT0H5M0S","duration_differs"),
    ("Other song","P0DT0H4M32S","title_differs"),
])
def test_real_mismatch_is_distinct(title,duration,reason):
    result=asyncio.run(HitmosProvider(Reader(SourceDocument(document(title=title,duration=duration),None,"stream_not_found"))).lookup(REF,"57658982"))
    assert result.stage=="matching"
    assert result.reason=="recording_mismatch"
    assert reason in result.candidates[0].match.reasons
    assert result.candidates[0].stream_url is None


def test_matching_thresholds_unchanged_and_both_recordings_are_reported():
    assert assess(REF,"Sonne","Rammstein",280).status=="likely"
    mismatch=assess(REF,"Sonne","Rammstein",281)
    assert mismatch.status=="mismatch"
    assert mismatch.comparison.reference_duration_seconds==272
    assert mismatch.comparison.candidate_duration_seconds==281
    assert mismatch.comparison.duration_difference_seconds==9
    assert mismatch.comparison.duration_tolerance_seconds==pytest.approx(8.16)
    assert mismatch.comparison.reference_artist=="Rammstein"


@pytest.mark.parametrize(("app","enabled"),[(local_app,True),(public_app,False)])
def test_diagnostics_are_opt_in_and_never_public(app,enabled,monkeypatch):
    monkeypatch.setenv("MUSICSTREAM_DIAGNOSTICS","true")
    monkeypatch.delenv("HITMOS_BROWSER_ENABLED",raising=False)
    diag=ResolutionDiagnostics(metadata_status=200,player_valid=True,redirect_status=302,
                               redirect_host="s4.deliciouspeaches.com",redirect_valid=True)
    with TestClient(app) as api:
        provider=app.state.audio_providers["hitmos"]
        provider.reader=Reader(SourceDocument(document(),CASES[0][2],None,diag))
        response=api.get("/api/audio/resolve",params={"provider":"hitmos","hitmos_id":"57658982","title":"Sonne","artist":"Rammstein","duration_seconds":272})
        assert response.status_code==200
        assert (response.json()["diagnostics"] is not None)==enabled
        assert response.json()["candidates"][0]["match"]["comparison"]["duration_difference_seconds"]==0


def test_local_diagnostics_default_off(monkeypatch):
    monkeypatch.delenv("MUSICSTREAM_DIAGNOSTICS",raising=False)
    with TestClient(local_app):
        assert not local_app.state.audio_providers["hitmos"].diagnostics_enabled


def test_private_values_are_not_in_diagnostics_or_logs(caplog):
    secret="private-cookie-token-sentinel"
    signed=CASES[0][2]+"?signature="+secret
    diag=ResolutionDiagnostics.model_validate({"redirect_host":"s4.deliciouspeaches.com","redirect_valid":False,
                                              "url":signed,"cookie":secret,"token":secret})
    result=asyncio.run(HitmosProvider(Reader(SourceDocument(document(),signed,"stream_redirect_not_allowed",diag)),diagnostics_enabled=True).lookup(REF,"57658982"))
    assert result.candidates[0].stream_url is None
    assert secret not in result.model_dump_json()
    assert secret not in caplog.text
    assert "https://" not in result.diagnostics.model_dump_json()


def test_source_http_error_is_not_a_matching_error():
    error=SourceFailure("source_http_error",stage="source_metadata",diagnostics=ResolutionDiagnostics(metadata_status=403))
    result=asyncio.run(HitmosProvider(Reader(error),diagnostics_enabled=True).lookup(REF,"57658982"))
    assert result.stage=="source_metadata"
    assert result.candidates==[]
    assert result.diagnostics.metadata_status==403


def test_manual_search_is_a_separate_stage():
    result=asyncio.run(HitmosProvider().find(REF))
    assert result.stage=="source_search"


@pytest.mark.parametrize("url",[
    CASES[0][2].replace('s4.deliciouspeaches.com','s5.deliciouspeaches.com'),
    CASES[1][2].replace('cdn14.deliciousoranges.com','cdn15.deliciousoranges.com'),
    CASES[2][2].replace('https:','http:'),
    CASES[2][2]+'?token=private',
    CASES[2][2].replace('_80274130.mp3','_1.mp3'),
    CASES[2][2].replace('/20251120/','/not-a-date/'),
])
def test_new_formats_keep_strict_security(url):
    assert not valid_stream_url(url,'80274130')


def test_relative_player_still_allows_only_one_exact_request_and_no_cdn_networking():
    song_id,raw,cdn=CASES[2]
    player=parse_stream_url(document(song_id,raw),song_id)
    assert request_action(player,"Media","GET","main","main",song_id,player,False)=="player"
    assert request_action(cdn,"Media","GET","main","main",song_id,player,True)=="block"
