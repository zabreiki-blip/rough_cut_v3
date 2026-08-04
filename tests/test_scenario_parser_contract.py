"""Characterization contracts for the three scenario-reading paths.

The legacy analyzer extracts only narration, the canonical normalizer preserves
and classifies every paragraph, and the legacy timeline parser projects text
directly into edit segments.  Their differences below are intentional: these
tests are a safety net for a future shared ``scenario_syntax`` module, not a
requirement that all three paths return the same shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stage1.analyzer import analyze_script
from stage1.normalizer import generate_normalization, import_legacy_normalization
from stage2.timeline_parser import parse_timeline


def _analyze_legacy(tmp_path: Path, text: str) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "scenario.txt"
    source.write_text(text, encoding="utf-8")
    output = tmp_path / "scenario_prepared.json"
    analyze_script(
        source,
        output,
        tmp_path / "scenario_raw.txt",
        use_llm=False,
    )
    return json.loads(output.read_text(encoding="utf-8"))


def _normalize_canonical(tmp_path: Path, text: str) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "scenario.txt"
    source.write_text(text, encoding="utf-8")
    return generate_normalization(
        source,
        tmp_path / "scenario_normalized.json",
        memory_path=tmp_path / "normalization_memory.sqlite3",
        use_llm=False,
    )


def _parse_legacy(tmp_path: Path, text: str):
    return parse_timeline(text, tmp_path, tmp_path, "contract")


@pytest.mark.parametrize(
    "text, canonical_type, canonical_text, timeline_type, timeline_text",
    [
        (
            "ЗАКАДРОВЫЙ мир описывает герой.",
            "unknown",
            "ЗАКАДРОВЫЙ мир описывает герой.",
            "tech",
            "ЗАКАДРОВЫЙ мир описывает герой.",
        ),
        (
            "ЗКАТ — это не маркер закадрового текста.",
            "unknown",
            "ЗКАТ — это не маркер закадрового текста.",
            "tech",
            "ЗКАТ — это не маркер закадрового текста.",
        ),
    ],
)
def test_marker_like_word_prefixes_have_path_specific_contracts(
    tmp_path: Path,
    text: str,
    canonical_type: str,
    canonical_text: str,
    timeline_type: str,
    timeline_text: str,
) -> None:
    legacy_analysis = _analyze_legacy(tmp_path, text)
    canonical = _normalize_canonical(tmp_path, text)
    legacy_timeline = _parse_legacy(tmp_path, text)

    # The narration-only fallback may retain ordinary prose as narration, but
    # marker-looking prefixes inside words must not be stripped.
    assert legacy_analysis["extraction_method"] == "fallback"
    assert [item["text_original"] for item in legacy_analysis["items"]] == [text]
    assert canonical["blocks"][0]["type"] == canonical_type
    assert canonical["blocks"][0]["normalized_text"] == canonical_text
    assert [(segment.type, segment.text) for segment in legacy_timeline.segments] == [
        (timeline_type, timeline_text)
    ]


def test_date_is_not_a_timecode_but_explicit_timecode_is_canonical_sync(
    tmp_path: Path,
) -> None:
    date_text = "23.05 Сегодня открывается выставка."
    timecoded_text = "[00:23] Реплика героя."

    date_analysis = _analyze_legacy(tmp_path / "date", date_text)
    timecode_analysis = _analyze_legacy(tmp_path / "timecode", timecoded_text)
    canonical = _normalize_canonical(
        tmp_path / "canonical", f"{date_text}\n{timecoded_text}"
    )

    assert [item["text_original"] for item in date_analysis["items"]] == [date_text]
    assert timecode_analysis["items"] == []
    assert [block["type"] for block in canonical["blocks"]] == ["unknown", "sync"]

    # A bare timecode is not a standalone segment in the legacy edit grammar;
    # it is meaningful there only as part of a recognized speaker form.
    legacy_timeline = _parse_legacy(tmp_path / "timeline", f"{date_text}\n{timecoded_text}")
    assert [segment.type for segment in legacy_timeline.segments] == ["tech", "tech"]


@pytest.mark.parametrize(
    "text, expected_role, analyzer_keeps_as_narration, canonical_supports",
    [
        (
            "СНХ ИВАН ИВАНОВ, эксперт, 00:01:02 Реплика героя.",
            "эксперт",
            False,
            True,
        ),
        (
            "ИВАН ИВАНОВ, эксперт: 00:01:02 Реплика героя.",
            "эксперт",
            False,
            True,
        ),
        (
            "ИВАН ИВАНОВ (эксперт) 01:02 Реплика героя.",
            "эксперт",
            True,
            False,
        ),
    ],
)
def test_snh_variants_have_path_specific_support(
    tmp_path: Path,
    text: str,
    expected_role: str,
    analyzer_keeps_as_narration: bool,
    canonical_supports: bool,
) -> None:
    legacy_analysis = _analyze_legacy(tmp_path, text)
    canonical = _normalize_canonical(tmp_path, text)
    legacy_timeline = _parse_legacy(tmp_path, text)

    assert bool(legacy_analysis["items"]) is analyzer_keeps_as_narration
    if canonical_supports:
        assert canonical["blocks"][0]["type"] == "sync"
        assert canonical["blocks"][0]["speaker"] == "ИВАН ИВАНОВ"
        assert canonical["blocks"][0]["role"] == expected_role
    else:
        assert canonical["blocks"][0]["type"] == "unknown"
        assert canonical["blocks"][0]["speaker"] == ""
        assert canonical["blocks"][0]["role"] == ""
    assert len(legacy_timeline.segments) == 1
    segment = legacy_timeline.segments[0]
    assert (segment.type, segment.speaker, segment.role) == (
        "snh",
        "ИВАН ИВАНОВ",
        expected_role,
    )


def test_multiline_boundaries_are_preserved_at_each_abstraction_level(
    tmp_path: Path,
) -> None:
    text = (
        "ЗК1: Первая строка закадрового текста.\n"
        "Вторая строка продолжает тот же закадровый текст.\n"
        "СНХ ИВАН ИВАНОВ, эксперт, 00:01:02 Первая строка ответа.\n"
        "продолжение ответа героя.\n"
    )

    legacy_analysis = _analyze_legacy(tmp_path / "analyzer", text)
    canonical = _normalize_canonical(tmp_path / "normalizer", text)
    legacy_timeline = _parse_legacy(tmp_path / "timeline", text)

    assert len(legacy_analysis["items"]) == 1
    assert legacy_analysis["items"][0]["text_original"] == (
        "Первая строка закадрового текста.\n"
        "Вторая строка продолжает тот же закадровый текст."
    )
    assert [block["type"] for block in canonical["blocks"]] == [
        "voiceover",
        "voiceover",
        "sync",
        "sync",
    ]
    assert [(segment.type, segment.text) for segment in legacy_timeline.segments] == [
        (
            "zk",
            "Первая строка закадрового текста. "
            "Вторая строка продолжает тот же закадровый текст.",
        ),
        ("snh", "Первая строка ответа. продолжение ответа героя."),
    ]


def test_legacy_import_is_a_projection_of_the_legacy_timeline_parser(
    tmp_path: Path,
) -> None:
    raw_text = (
        "ЗК7: Текст диктора.\n"
        "СНХ ИВАН ИВАНОВ, эксперт, 00:01:02 Ответ героя.\n"
    )
    raw_path = tmp_path / "scenario_raw.txt"
    raw_path.write_text(raw_text, encoding="utf-8")
    prepared_path = tmp_path / "scenario_prepared.json"
    prepared_path.write_text(
        json.dumps(
            {"items": [{"text_original": "Текст диктора.", "text_for_tts": "TTS"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    timeline = _parse_legacy(tmp_path, raw_text)
    imported = import_legacy_normalization(
        prepared_path,
        raw_path,
        tmp_path / "scenario_normalized.json",
    )

    assert [block["type"] for block in imported["blocks"]] == ["voiceover", "sync"]
    assert [block["normalized_text"] for block in imported["blocks"]] == [
        segment.text for segment in timeline.segments
    ]
    assert imported["blocks"][0]["text_for_tts"] == "TTS"
    assert imported["status"] == "draft"
    assert imported["warnings"] == ["Imported legacy data requires confirmation"]
