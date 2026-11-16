import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
sys.path.insert(0, str(Path(__file__).parents[3] / "src"))

from providers import (  # noqa: E402
    HOSTS,
    AdapterFailure,
    SafeSiteClient,
    _duration,
    _media_observation,
    parse_hitmos_fm_rsc,
)
from musicstream.audio_providers.base import RecordingReference  # noqa: E402


def test_fixed_site_hosts_and_destination_allowlist():
    assert HOSTS == {"hitmoz": "ru.hitmoz.org", "hitmos_fm": "rus.hitmos.fm"}
    client = SafeSiteClient("hitmoz")
    assert client._check_url("https://ru.hitmoz.org/search?q=Sonne") == "ru.hitmoz.org"
    for url in (
        "http://ru.hitmoz.org/search",
        "https://localhost/search",
        "https://ru.hitmoz.org.evil.example/search",
        "https://user@ru.hitmoz.org/search",
        "https://ru.hitmoz.org:444/search",
    ):
        with pytest.raises(AdapterFailure):
            client._check_url(url)


def test_dns_pinning_accepts_only_public_answer_sets():
    assert SafeSiteClient._checked_ips(("93.184.216.34",)) == ("93.184.216.34",)
    for values in (("127.0.0.1",), ("93.184.216.34", "10.0.0.2"), (), ("not-an-ip",)):
        with pytest.raises(AdapterFailure):
            SafeSiteClient._checked_ips(values)


@pytest.mark.parametrize(("raw", "seconds"), [("04:33", 273), ("1:02:03", 3723), ("PT4M33S", 273), (273, 273), ("999999", None), ("n/a", None)])
def test_duration_shapes(raw, seconds):
    assert _duration(raw) == seconds


def test_media_observation_reports_no_url_and_rejects_untyped_page_links():
    assert _media_observation("https://cdn.example/song.mp3") == type(_media_observation())(True, "mp3", "cdn.example", False)
    assert not _media_observation("https://hitmos.fm/track/123").present
    observed = _media_observation("https://cdn.example/song.mp3?token=secret")
    assert observed.present and observed.host == "cdn.example"
    assert "token" not in repr(observed)


def test_rsc_extraction_and_metadata_matching_without_exposing_media_url():
    reference = RecordingReference(title="Sonne", artist="Rammstein", duration_seconds=271)
    record = {"id": 701, "artist": "Rammstein", "title": "Sonne", "duration": 271,
              "play": "https://media.example/get/track.mp3?token=private"}
    payload = "0:" + json.dumps(record, ensure_ascii=False)
    html = "<script>self.__next_f.push(" + json.dumps([1, payload]) + ");</script>"
    candidates = parse_hitmos_fm_rsc(html, reference)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert (candidate.id, candidate.title, candidate.artist, candidate.duration_seconds) == ("701", "Sonne", "Rammstein", 271.0)
    assert candidate.match == "likely"
    assert candidate.media.present and candidate.media.kind == "mp3"
    assert candidate.media.host == "media.example" and candidate.media.allowed_host is False
    assert "token" not in repr(candidate)


def test_rsc_wrong_version_does_not_match_original():
    reference = RecordingReference(title="Mutter", artist="Rammstein", duration_seconds=268)
    record = {"id": "702", "artist": "Rammstein", "title": "Mutter (Live)", "duration": 268,
              "download": "https://cdn.example/download/702.mp3"}
    html = "<script>self.__next_f.push(" + json.dumps([1, "0:" + json.dumps(record)]) + ");</script>"
    candidates = parse_hitmos_fm_rsc(html, reference)
    assert len(candidates) == 1
    assert candidates[0].match == "mismatch"
    assert "version_differs" in candidates[0].match_reasons


def test_rsc_malformed_and_missing_media_fields_are_tolerated():
    reference = RecordingReference(title="Sonne", artist="Rammstein", duration_seconds=271)
    html = '<script>self.__next_f.push([1,"not-json {broken"]);</script>'
    assert parse_hitmos_fm_rsc(html, reference) == ()


def test_hitmoz_html_parser_requires_selectolax(monkeypatch):
    from providers import HitmozAdapter

    if __import__("providers").LexborHTMLParser is not None:
        pytest.skip("selectolax is installed; parser fixtures are covered by the integration environment")
    client = SafeSiteClient("hitmoz")

    async def fetch(_):
        from providers import FetchResult
        return FetchResult(200, "https://ru.hitmoz.org/search?q=test", "text/html", "<html></html>", 0)

    monkeypatch.setattr(client, "fetch", fetch)
    with pytest.raises(AdapterFailure, match="selectolax_not_installed"):
        import asyncio
        asyncio.run(HitmozAdapter(client).search(RecordingReference(title="Sonne", artist="Rammstein")))


@pytest.mark.skipif(__import__("providers").LexborHTMLParser is None, reason="install experiment requirements")
def test_hitmoz_search_and_song_page_parsers_when_selectolax_is_installed(monkeypatch):
    import asyncio
    from providers import FetchResult, HitmozAdapter

    reference = RecordingReference(title="Sonne", artist="Rammstein", duration_seconds=273)
    search_html = '''<div class="tracks__item" data-musmeta='{"id":"703","title":"Sonne","artist":"Rammstein","duration":"04:33"}'><a href="/song/703">Sonne</a></div>'''
    song_html = '''<script type="application/ld+json">{"@type":"MusicRecording","name":"Sonne","byArtist":{"name":"Rammstein"},"duration":"PT4M33S","audio":"https://media.invalid/get/703.mp3?token=private"}</script>'''
    client = SafeSiteClient("hitmoz")

    async def fetch(url, **kwargs):
        body = search_html if "/search?" in url else song_html
        return FetchResult(200, url, "text/html", body, 0)

    monkeypatch.setattr(client, "fetch", fetch)
    adapter = HitmozAdapter(client)
    candidates = asyncio.run(adapter.search(reference))
    assert len(candidates) == 1 and candidates[0].id == "703" and candidates[0].match == "likely"
    detail = asyncio.run(adapter.inspect_song(reference, "703"))
    assert detail.match == "likely" and detail.media.present and detail.media.kind == "mp3"
    assert detail.media.host == "media.invalid" and "token" not in repr(detail)


@pytest.mark.skipif(__import__("providers").LexborHTMLParser is None, reason="install experiment requirements")
def test_hitmos_fm_search_fetches_query_without_preflight_endpoint(monkeypatch):
    import asyncio
    from providers import FetchResult, HitmosFmAdapter

    reference = RecordingReference(title="Sonne", artist="Rammstein", duration_seconds=273)
    html = '''<div class="track-item"><a href="/song/703">Sonne</a><a href="/artist/1">Rammstein</a><span class="track-item__title">Sonne</span><span class="track-item__artist">Rammstein</span><span class="track-item__duration">04:33</span></div>'''
    client = SafeSiteClient("hitmos_fm")
    calls = []

    async def fetch(url, **kwargs):
        calls.append(url)
        return FetchResult(200, url, "text/html", html, 0)

    monkeypatch.setattr(client, "fetch", fetch)
    candidates = asyncio.run(HitmosFmAdapter(client).search(reference))
    assert candidates[0].id == "703" and candidates[0].match == "likely"
    assert calls == ["https://rus.hitmos.fm/search?query=Rammstein+Sonne"]
