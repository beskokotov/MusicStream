from musicstream.audio_providers.base import RecordingReference
from musicstream.audio_providers.vk import clean_recordings, parse_duration, recording_id, VKAudioSearchResult, match_recordings
from musicstream.audio_providers.vk_browser import _search_recording_visible_in_rows, _visible_search_recordings


def test_extracts_only_stable_vk_owner_audio_identity():
    assert recording_id("-12345_67890") == "-12345_67890"
    assert recording_id('[ -12345, 67890, "private_access_key" ]') == "-12345_67890"
    assert recording_id('{"owner_id":-12345,"id":67890,"access_key":"secret"}') == "-12345_67890"
    assert recording_id("67890") is None


def test_parses_displayed_duration_without_guessing_player_position():
    assert parse_duration("4:32") == 272
    assert parse_duration("1:02:03") == 3723
    assert parse_duration("0:08 / 3:20") is None
    assert parse_duration("") is None


def test_filters_ads_unidentified_rows_and_duplicate_ids():
    rows = [
        {"full_id": "1_2", "title": "Sonne", "artist": "Rammstein", "duration": "4:32"},
        {"full_id": "1_2", "title": "Duplicate", "artist": "Rammstein"},
        {"full_id": "3_4", "title": "Sponsored track", "artist": "Brand", "label": "Sponsored"},
        {"full_id": "5_6", "title": "Предыдущая аудиозапись", "artist": "Пауза"},
        {"full_id": "7_8", "title": "Сообщества", "artist": "Показать все"},
        {"title": "Sonne", "artist": "Rammstein"},
    ]
    recordings = clean_recordings(rows)
    assert len(recordings) == 1
    assert recordings[0].id == "1_2"
    assert recordings[0].duration_seconds == 272


def test_extracts_stable_identity_from_visible_audio_link_metadata():
    recordings = clean_recordings([{
        "href_id": "-2001871978_6871978",
        "title": "Sonne",
        "artist": "Rammstein",
        "duration": "4:32",
    }])
    assert len(recordings) == 1
    assert recordings[0].id == "-2001871978_6871978"


def test_combines_separate_visible_owner_and_audio_ids():
    recordings = clean_recordings([{
        "owner_id": "-2001871978",
        "local_audio_id": "6871978",
        "title": "Sonne",
        "artist": "Rammstein",
        "duration": "4:32",
    }])
    assert len(recordings) == 1
    assert recordings[0].id == "-2001871978_6871978"


def test_distills_first_party_search_json_to_minimal_record_metadata():
    rows = _visible_search_recordings({"response": {"items": [
        {"owner_id": -2001871978, "id": 6871978, "title": "Sonne", "artist": "Rammstein",
         "duration": 272, "access_key": "must-not-be-returned"},
        {"owner_id": -2001871978, "id": 6871979, "title": "Another", "duration": 100,
         "access_key": "must-not-be-returned"},
    ]}})
    assert rows == [{"full_id": "-2001871978_6871978", "title": "Sonne", "artist": "Rammstein",
                     "duration": "272", "text": "", "label": "", "class_name": ""},
                    {"full_id": "-2001871978_6871979", "title": "Another", "artist": "",
                     "duration": "100", "text": "", "label": "", "class_name": ""}]
    cleaned = clean_recordings(rows)
    assert cleaned[0].duration_seconds == 272


def test_network_record_must_match_a_rendered_track_card():
    record = {"title": "Sonne", "artist": "Rammstein", "duration": "272"}
    assert _search_recording_visible_in_rows(record, [{"text": "Sonne Rammstein 4:32"}])
    assert not _search_recording_visible_in_rows(record, [{"text": "Sonne Rammstein 4:05"}])


def test_unknown_duration_never_confirms_a_match():
    ref = RecordingReference(artist="Rammstein", title="Sonne", duration_seconds=272)
    batch = VKAudioSearchResult("Rammstein Sonne", 1, clean_recordings([
        {"full_id": "1_2", "title": "Sonne", "artist": "Rammstein"},
    ]))
    matches = match_recordings(ref, batch)
    assert matches[0].assessment.status == "uncertain"
    assert "duration_unknown" in matches[0].assessment.reasons


def test_unknown_reference_duration_still_ranks_exact_artist_before_alias():
    ref = RecordingReference(artist="Machine Gun Kelly", title="FIX UR FACE")
    rows = [
        {"full_id": "1_2", "title": "FIX UR FACE", "artist": "mgk, Fred Durst", "duration": "3:19"},
        {"full_id": "3_4", "title": "FIX UR FACE", "artist": "Machine Gun Kelly ft. Fred Durst", "duration": "3:19"},
    ]
    batch = VKAudioSearchResult("MGK FIX UR FACE", 2, clean_recordings(rows))
    matches = match_recordings(ref, batch)
    assert matches[0].recording.id == "3_4"
    assert matches[0].assessment.status == "uncertain"
    assert matches[0].assessment.reasons == ["duration_unknown"]
