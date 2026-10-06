from pathlib import Path

from api import _sync_alignment_units
from stage2.timeline_parser import (
    normalized_sync_fragments,
    parse_normalized_timeline,
    split_compound_quote,
)
from stage2.models import TimelineSegment
from stage2.timeline_parser import _merge_adjacent_sync_fragments


def test_compound_quote_splits_splices_and_sentences() -> None:
    text = (
        "Это первая длинная реплика из начала интервью... "
        "А это длинная реплика из другого места записи. "
        "После неё звучит отдельное предложение из того же интервью."
    )
    assert split_compound_quote(text) == [
        "Это первая длинная реплика из начала интервью",
        "А это длинная реплика из другого места записи.",
        "После неё звучит отдельное предложение из того же интервью.",
    ]


def test_short_pause_stays_with_speech_and_only_first_part_keeps_timecode() -> None:
    block = {
        "sync_fragments": [
            {
                "text": "Ну... я думаю, что это важно. Потом мы говорили о другой теме.",
                "timecode_start": "00:12:00",
                "timecode_end": "",
            }
        ]
    }
    fragments = normalized_sync_fragments(block)
    assert len(fragments) == 2
    assert fragments[0]["text"].startswith("Ну я думаю")
    assert [item["timecode_start"] for item in fragments] == ["00:12:00", ""]


def test_confirmed_parent_keeps_stable_match_id_and_range(tmp_path: Path) -> None:
    block = {
        "uid": "block-one",
        "order": 1,
        "type": "sync",
        "speaker": "Интервьюер",
        "normalized_text": "Первая часть интервью. Вторая часть интервью.",
    }
    match = {
        "segment_id": "block-one",
        "status": "confirmed",
        "source_id": "source-one",
        "source_in_sec": 10.0,
        "source_out_sec": 22.0,
        "confidence": 1.0,
    }
    timeline = parse_normalized_timeline(
        {"status": "confirmed", "blocks": [block]},
        tmp_path,
        tmp_path,
        "TEST",
        sync_matches={"block-one": match},
    )
    assert len(timeline.segments) == 1
    assert timeline.segments[0].block_uid == "block-one"
    assert timeline.segments[0].match_status == "confirmed"
    assert timeline.segments[0].duration_sec == 12.0


def test_alignment_units_preserve_confirmed_parent_id() -> None:
    block = {
        "uid": "block-one",
        "order": 1,
        "type": "sync",
        "speaker": "Интервьюер",
        "normalized_text": "Первая длинная часть интервью. Вторая длинная часть интервью.",
    }
    split_units = _sync_alignment_units([block])
    kept_units = _sync_alignment_units([block], preserve_whole_uids={"block-one"})
    assert [unit["segment_id"] for unit in split_units] == [
        "block-one::fragment:1",
        "block-one::fragment:2",
    ]
    assert [unit["segment_id"] for unit in kept_units] == ["block-one"]


def test_adjacent_speech_from_same_take_merges_without_losing_range() -> None:
    first = TimelineSegment(
        id="snh1",
        type="snh",
        text="Первая фраза.",
        duration_sec=8,
        parent_block_uid="block-one",
        source_id="source-one",
        source_in_sec=10,
        source_out_sec=18,
        speech_in_sec=12,
        speech_out_sec=16.5,
        match_status="auto_matched",
        match_confidence=0.96,
    )
    second = TimelineSegment(
        id="snh2",
        type="snh",
        text="Вторая фраза.",
        duration_sec=8,
        parent_block_uid="block-one",
        source_id="source-one",
        source_in_sec=15,
        source_out_sec=23,
        speech_in_sec=17,
        speech_out_sec=21.5,
        match_status="auto_matched",
        match_confidence=0.94,
    )
    merged = _merge_adjacent_sync_fragments([first, second])
    assert len(merged) == 1
    assert merged[0].text == "Первая фраза. Вторая фраза."
    assert (merged[0].source_in_sec, merged[0].source_out_sec) == (10, 23)


def test_distant_takes_remain_separate() -> None:
    first = TimelineSegment(
        id="snh1",
        type="snh",
        text="Первая.",
        duration_sec=8,
        parent_block_uid="block-one",
        source_id="source-one",
        source_in_sec=10,
        source_out_sec=18,
        speech_in_sec=12,
        speech_out_sec=16,
        match_status="auto_matched",
    )
    second = TimelineSegment(
        id="snh2",
        type="snh",
        text="Вторая.",
        duration_sec=8,
        parent_block_uid="block-one",
        source_id="source-one",
        source_in_sec=30,
        source_out_sec=38,
        speech_in_sec=32,
        speech_out_sec=36,
        match_status="auto_matched",
    )
    assert len(_merge_adjacent_sync_fragments([first, second])) == 2
