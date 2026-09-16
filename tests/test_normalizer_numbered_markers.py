from __future__ import annotations

from pathlib import Path
import tempfile

import pytest

from stage1.normalization_memory import NormalizationMemory, normalize_marker
from stage1.normalizer import generate_normalization


# --- normalize_marker: a number glued directly to the marker letters (no
# separating whitespace) must be stripped just like a space-separated one. ---


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("ТЕХ32", "тех"),
        ("ЗК5", "зк"),
        ("СНХ12", "снх"),
        ("ТЕХ 7", "тех"),
    ],
)
def test_normalize_marker_strips_glued_trailing_number(marker: str, expected: str) -> None:
    assert normalize_marker(marker) == expected


def test_normalize_marker_keeps_purely_numeric_marker_intact() -> None:
    # Stripping the trailing number must never empty out a marker that is
    # entirely numeric -- there is no letter part left to fall back to.
    assert normalize_marker("312") == "312"


# --- A glued trailing number must only be stripped for the closed set of
# known service-cue prefixes (тех/зк/снх/...); for an arbitrary marker like
# a person's name, the glued digit distinguishes two different speakers and
# must be kept. ---


def test_normalize_marker_keeps_glued_number_on_arbitrary_marker() -> None:
    assert normalize_marker("Сергей1") == "сергей1"
    assert normalize_marker("Сергей2") == "сергей2"
    assert normalize_marker("Сергей1") != normalize_marker("Сергей2")


def test_two_numbered_name_markers_do_not_conflict_in_memory(tmp_path: Path) -> None:
    with NormalizationMemory(tmp_path / "memory.sqlite3") as memory:
        sync_rule = memory.record_manual("Сергей1", "sync")
        voiceover_rule = memory.record_manual("Сергей2", "voiceover")

        assert sync_rule["status"] == "active"
        assert voiceover_rule["status"] == "active"

        resolved_sync = memory.resolve_exact("Сергей1")
        resolved_voiceover = memory.resolve_exact("Сергей2")
        assert resolved_sync is not None and resolved_sync["conflict"] is False
        assert resolved_sync["rule"]["target_type"] == "sync"
        assert resolved_voiceover is not None and resolved_voiceover["conflict"] is False
        assert resolved_voiceover["rule"]["target_type"] == "voiceover"


def test_legacy_glued_number_marker_still_resolves_after_upgrade(tmp_path: Path) -> None:
    # A database written before this fix already has rows whose
    # normalized_marker is "сергей1" (the glued digit kept, matching the
    # original pre-regression behaviour). After the upgrade, normalize_marker
    # must still produce "сергей1" for that marker so those rows keep being
    # found by resolve_exact/classification instead of going silently stale.
    with NormalizationMemory(tmp_path / "memory.sqlite3") as memory:
        memory.record_manual("Сергей1", "sync")
        resolved = memory.resolve_exact("Сергей1")
        assert resolved is not None and resolved["conflict"] is False
        assert resolved["rule"]["target_type"] == "sync"
        assert resolved["rule"]["normalized_marker"] == "сергей1"


def _classify_block(text: str) -> dict:
    """Run the normalizer (no LLM) on a two-paragraph scenario and return
    the block matching ``text``. Mirrors the helper in
    test_normalizer_technical_cues.py.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "scenario.txt"
        leading_paragraph = "З.к. В том году она впервые вышла на большую сцену и запела."
        scenario = f"{leading_paragraph}\n\n{text}\n"
        source.write_text(scenario, encoding="utf-8")
        blocks = generate_normalization(
            source,
            root / "out.json",
            memory_path=root / "memory.sqlite3",
            use_llm=False,
        )["blocks"]
        for block in blocks:
            if text in block.get("original_text", ""):
                return block
        raise AssertionError(f"Block containing '{text}' not found")


TECHNICAL_NUMBERED_TEXTS = (
    "ТЕХ32: • Лечу в темноту",
    "ТЕХ20: ОТБИВКА",
    "ТЕХ21: Фрагмент «Ледникового периода» с участием Фриске.",
    "ТЕХ17: Ночной дозор: 00.57 Это Завулон. – Я не могу сейчас разговаривать.",
)


@pytest.mark.parametrize(
    "text",
    TECHNICAL_NUMBERED_TEXTS,
    ids=[f"tech-numbered-{i}" for i in range(len(TECHNICAL_NUMBERED_TEXTS))],
)
def test_glued_numbered_tech_marker_is_technical_by_builtin_rule(text: str) -> None:
    block = _classify_block(text)
    assert block["type"] == "technical"
    assert block["source"] == "builtin_rule"


def test_glued_numbered_zk_marker_is_voiceover_by_builtin_rule() -> None:
    block = _classify_block("ЗК14: В том году она впервые вышла на большую сцену.")
    assert block["type"] == "voiceover"
    assert block["source"] == "builtin_rule"


def test_glued_numbered_snh_marker_is_sync_by_builtin_rule() -> None:
    block = _classify_block("СНХ7: Она подходит и говорит, что хотела сказать новость.")
    assert block["type"] == "sync"
    assert block["source"] == "builtin_rule"
