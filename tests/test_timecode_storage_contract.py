"""Consumers of stored timecodes must compare normalized values, not spellings.

Storage contract: ``timecode_*`` is ``HH:MM:SS`` (or ``MM:SS`` without hours,
fraction dropped); the author's spelling stays in ``original_text``.  Older
projects stored the spot spelling (``01.02.03``), so every consumer accepts both.
"""

from __future__ import annotations

import pytest

import api
from stage1.scenario_structure import normalize_timecode_value
from stage2.timeline_parser import _spoken_text
from stage4.versioning import _restore_exact_auto_classifications, compare_blocks


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("01.02.03", "01:02:03"),
        ("01:02:03", "01:02:03"),
        ("(00:42:32)", "00:42:32"),
        ("29:41.93", "29:41"),
        ("35:45", "35:45"),
        ("", ""),
        ("не таймкод", "не таймкод"),
    ],
)
def test_normalize_timecode_value(value: str, expected: str) -> None:
    assert normalize_timecode_value(value) == expected


@pytest.mark.parametrize(
    ("text", "start", "old_start", "end", "old_end"),
    [
        ("СНХ ИМЯ 01.02.03 текст", "01:02:03", "01.02.03", "", ""),
        ("СНХ ИМЯ, роль 01.02.03 текст", "01:02:03", "01.02.03", "", ""),
        ("СНХ ИМЯ (00:42:32) текст", "00:42:32", "00:42:32", "", ""),
        ("СНХ ИМЯ [29:41.93 - 30:42.20] текст", "29:41", "29:41.93", "30:42", "30:42.20"),
        ("СНХ ИМЯ 00.05.20 - 00.05.40 текст", "00:05:20", "00.05.20", "00:05:40", "00.05.40"),
    ],
)
def test_sync_quote_strips_prefix_for_normalized_and_legacy_storage(
    text: str, start: str, old_start: str, end: str, old_end: str
) -> None:
    for tc_start, tc_end in ((start, end), (old_start, old_end)):
        block = {"normalized_text": text, "timecode_start": tc_start, "timecode_end": tc_end}
        assert api._sync_quote(block) == "текст", (tc_start, tc_end)


def test_sync_quote_keeps_later_timecode_in_speech() -> None:
    block = {"normalized_text": "СНХ ИМЯ 01.02.03 в 01:02:03 было", "timecode_start": "01:02:03"}
    assert api._sync_quote(block) == "в 01:02:03 было"


def _block(uid: str, **extra: object) -> dict:
    return {
        "uid": uid,
        "normalized_text": "Текст синхрона",
        "original_text": "Текст синхрона",
        "type": "sync",
        "speaker": "Иван",
        "original_label": "Иван",
        "text_for_tts": "",
        **extra,
    }


def test_downstream_timecode_spelling_is_not_a_change() -> None:
    old = [
        _block(
            "stable",
            timecode_start="01.02.03",
            timecode_end="01.02.09",
            sync_fragments=[{"text": "Т", "timecode_start": "01.02.03", "timecode_end": ""}],
        )
    ]
    new = [
        _block(
            "imported",
            timecode_start="01:02:03",
            timecode_end="01:02:09",
            sync_fragments=[{"text": "Т", "timecode_start": "01:02:03", "timecode_end": ""}],
        )
    ]
    diff = compare_blocks(old, new)
    assert diff["summary"] == {"unchanged": 1}
    assert diff["changes"][0]["downstream_changed_fields"] == []


def test_downstream_real_timecode_change_is_still_a_change() -> None:
    old = [_block("stable", timecode_start="01.02.03")]
    new = [_block("imported", timecode_start="01:02:04")]
    diff = compare_blocks(old, new)
    assert diff["changes"][0]["downstream_changed_fields"] == ["timecode_start"]


def test_restore_does_not_bring_back_legacy_timecode_spelling() -> None:
    old = [
        _block(
            "stable",
            type="voiceover",
            timecode_start="01.02.03",
            sync_fragments=[{"text": "Т", "timecode_start": "01.02.03", "timecode_end": ""}],
        )
    ]
    new = [
        _block(
            "imported",
            type="unknown",
            source="llm",
            original_label="",
            timecode_start="01:02:03",
            sync_fragments=[{"text": "Т", "timecode_start": "01:02:03", "timecode_end": ""}],
        )
    ]
    _restore_exact_auto_classifications(old, new)
    assert new[0]["type"] == "voiceover"
    assert new[0]["timecode_start"] == "01:02:03"
    assert new[0]["sync_fragments"][0]["timecode_start"] == "01:02:03"


def test_spoken_text_keeps_dates_and_clock_times_in_speech() -> None:
    text = "В 12.05.1915 было, в 5:30 утра"
    assert _spoken_text(text, block_type="sync") == text
    assert _spoken_text(text, block_type="voiceover") == text


def test_spoken_text_removes_only_structural_timecodes() -> None:
    assert _spoken_text("(00:05:20) Реплика в 5:30", block_type="sync") == "Реплика в 5:30"
    assert _spoken_text("00:05:20 Реплика в 5:30", block_type="sync") == "Реплика в 5:30"
    assert (
        _spoken_text("Реплика 00.05.20 дальше в 5:30", block_type="sync", timecodes=("00:05:20",))
        == "Реплика дальше в 5:30"
    )
    assert (
        _spoken_text("Реплика 00:05:20 дальше", block_type="sync", timecodes=("01:00:00",))
        == "Реплика 00:05:20 дальше"
    )
