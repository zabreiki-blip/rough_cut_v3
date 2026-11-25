from stage2.sync_models import AlignmentRequest, SyncSource
from translated_sync.matching import match_translated_quote
from translated_sync.transcript import TranscriptCue


def _source(id, speaker):
    return SyncSource(id=id, speaker=speaker, premiere_sequence=f"seq-{id}", duration_sec=100)


def test_reordered_short_quote_matches_but_always_needs_review():
    cue = TranscriptCue(
        "c",
        "s",
        "Анна",
        "Мы приехали в Москву вчера вечером",
        "We arrived in Moscow yesterday evening",
        10,
        16,
        True,
        2,
    )
    match = match_translated_quote(
        AlignmentRequest("seg", "Вчера в Москву мы приехали"), [cue], [_source("s", "Анна")]
    )
    assert match.status == "needs_review" and match.strategy == "translated_docx"
    assert match.metadata["timing_precision"] == "coarse_window"
    assert match.source_in_sec == 10 and match.source_out_sec == 16


def test_speaker_and_source_hint_choose_repeated_quote(tmp_path=None):
    a = TranscriptCue("a", "a", "Иван", "Я всё понял", "I understand", 1, 3, False, 0)
    b = TranscriptCue("b", "b", "Мария", "Я всё понял", "I understand", 20, 24, True, 1)
    req = AlignmentRequest("seg", "Я всё понял", speaker="Мария", source_hint="seq-b")
    match = match_translated_quote(req, [a, b], [_source("a", "Иван"), _source("b", "Мария")])
    assert match.source_id == "b"
    assert match.metadata["approximate"]
    assert match.alternatives and match.alternatives[0].source_id == "a"


def test_no_overlap_returns_not_found():
    cue = TranscriptCue("c", "s", "Анна", "Мы приехали домой", "We came home", 1, 3, False, 0)
    match = match_translated_quote(
        AlignmentRequest("seg", "Совершенно другой разговор о погоде"),
        [cue],
        [_source("s", "Анна")],
    )
    assert match.status == "not_found"


def test_one_shared_word_does_not_match_short_quote():
    cue = TranscriptCue("c", "s", "Анна", "Я сегодня дома", "I am home today", 1, 3, False, 0)
    match = match_translated_quote(
        AlignmentRequest("seg", "Я завтра уеду"), [cue], [_source("s", "Анна")]
    )
    assert match.status == "not_found"


def test_display_time_hint_is_converted_from_each_source_start():
    cue_near = TranscriptCue("near", "s", "Анна", "Я всё понял", "I understand", 20, 24, True, 0)
    cue_far = TranscriptCue("far", "s", "Анна", "Я всё понял", "I understand", 5, 9, False, 1)
    source = SyncSource(
        id="s",
        speaker="Анна",
        premiere_sequence="seq-s",
        sequence_start_tc="01:00:00:00",
        fps=25,
        duration_sec=100,
    )
    request = AlignmentRequest(
        "seg", "Я всё понял", approximate_timecode="01:00:20:00", timecode_mode="display"
    )
    match = match_translated_quote(request, [cue_near, cue_far], [source])
    assert match.source_in_sec == 20


def test_media_filename_hint_selects_source():
    a = TranscriptCue("a", "a", "", "Я всё понял", "", 1, 3, True, 0)
    b = TranscriptCue("b", "b", "", "Я всё понял", "", 1, 3, True, 0)
    sources = [
        SyncSource(
            id="a",
            speaker="",
            premiere_sequence="A",
            duration_sec=10,
            media_path="/media/first.wav",
        ),
        SyncSource(
            id="b",
            speaker="",
            premiere_sequence="B",
            duration_sec=10,
            media_path="/media/second.wav",
        ),
    ]
    match = match_translated_quote(
        AlignmentRequest("seg", "Я всё понял", source_hint="second.wav"), [a, b], sources
    )
    assert match.source_id == "b"


def test_relative_hint_uses_viewing_file_offset_and_alternative_has_cue_id():
    near = TranscriptCue("near", "s", "", "Одинаковая фраза", "", 21, 23, False, 0)
    far = TranscriptCue("far", "s", "", "Одинаковая фраза", "", 1, 3, False, 1)
    source = SyncSource(
        id="s", speaker="", premiere_sequence="S", duration_sec=60, guide_offset_sec=20
    )
    result = match_translated_quote(
        AlignmentRequest("b", "Одинаковая фраза", approximate_timecode="00:01"),
        [near, far],
        [source],
    )
    assert result.metadata["document_cue_id"] == "near"
    assert result.alternatives[0].to_dict()["document_cue_id"] == "far"
