from __future__ import annotations

from pathlib import Path
import re
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


# --- A whitespace-separated trailing number follows the same rule as a glued
# one: it is stripped only for known service-cue markers, never for people. ---


def test_normalize_marker_keeps_spaced_number_on_person_name() -> None:
    assert normalize_marker("Сергей 1") == "сергей 1"
    assert normalize_marker("Сергей 2") == "сергей 2"
    assert normalize_marker("Сергей 1") != normalize_marker("Сергей 2")


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("диктор 1", "диктор"),
        ("Диктор 2", "диктор"),
        ("зк 5", "зк"),
        ("ЗК 5", "зк"),
        ("тех 3", "тех"),
        ("снх 12", "снх"),
        ("синхрон 2", "синхрон"),
        ("лайф 4", "лайф"),
        ("ведущий 2", "ведущий"),
        ("видео 3", "видео"),
        ("з к 5", "з к"),
        ("голос диктора 2", "голос диктора"),
    ],
)
def test_normalize_marker_strips_spaced_number_on_service_marker(
    marker: str, expected: str
) -> None:
    assert normalize_marker(marker) == expected


def test_spaced_numbered_name_rule_does_not_apply_to_other_number(tmp_path: Path) -> None:
    with NormalizationMemory(tmp_path / "memory.sqlite3") as memory:
        memory.record_manual("Сергей 1", "sync")
        assert memory.resolve_exact("Сергей 2") is None
        assert memory.resolve_exact("Сергей") is None
        resolved = memory.resolve_exact("Сергей 1")
        assert resolved is not None and resolved["rule"]["target_type"] == "sync"


def test_spaced_numbered_name_suppression_is_not_shared(tmp_path: Path) -> None:
    with NormalizationMemory(tmp_path / "memory.sqlite3") as memory:
        memory.suppress_marker("Сергей 1")
        assert memory.is_suppressed("Сергей 1")
        assert not memory.is_suppressed("Сергей 2")


def test_spaced_numbered_service_marker_rule_still_shared(tmp_path: Path) -> None:
    with NormalizationMemory(tmp_path / "memory.sqlite3") as memory:
        memory.record_manual("Диктор 1", "voiceover")
        resolved = memory.resolve_exact("Диктор 2")
        assert resolved is not None and resolved["rule"]["target_type"] == "voiceover"


def _legacy_rekey_db(path: Path) -> None:
    # Simulate rows written by the old normalize_marker(): the person's
    # numbered marker was stored under the index-less key.
    with NormalizationMemory(path) as memory:
        memory.record_manual("Сергей 2", "sync")
        memory.record_manual("Диктор 3", "voiceover")
        memory.suppress_marker("Анна 4")
        memory._conn.execute("UPDATE aliases SET normalized_marker='сергей' WHERE marker='Сергей 2'")
        memory._conn.execute(
            "UPDATE alias_suppressions SET normalized_marker='анна' WHERE marker='Анна 4'"
        )
        memory._conn.commit()


def test_legacy_ambiguous_key_is_rekeyed_from_raw_marker_on_load(tmp_path: Path) -> None:
    db = tmp_path / "memory.sqlite3"
    _legacy_rekey_db(db)
    with NormalizationMemory(db) as memory:
        keys = {row["marker"]: row["normalized_marker"] for row in memory.list_rules()}
        assert keys["Сергей 2"] == "сергей 2"
        assert keys["Диктор 3"] == "диктор"
        assert memory.resolve_exact("Сергей 1") is None
        assert memory.resolve_exact("Сергей 2") is not None
        assert memory.is_suppressed("Анна 4")
        assert not memory.is_suppressed("Анна 5")


def test_legacy_rekey_drops_stale_duplicate_instead_of_failing(tmp_path: Path) -> None:
    db = tmp_path / "memory.sqlite3"
    _legacy_rekey_db(db)
    with NormalizationMemory(db) as memory:
        memory.record_manual("Сергей 2", "sync")  # already correct key
        memory._conn.execute(
            """INSERT INTO aliases(marker, normalized_marker, target_type, source, status,
               confirmations, corrections, confidence, created_at, updated_at, scope)
               VALUES ('Сергей 2', 'сергей', 'sync', 'manual', 'active', 1, 0, 1.0, 'x', 'x', 'marker')"""
        )
        memory._conn.commit()
    with NormalizationMemory(db) as memory:
        rows = [r for r in memory.list_rules() if r["marker"] == "Сергей 2"]
        assert [r["normalized_marker"] for r in rows] == ["сергей 2"]


def test_spaced_strippable_prefixes_cover_all_builtin_and_tech_markers() -> None:
    from stage1.normalization_memory import _SPACED_NUMBER_STRIPPABLE_PREFIXES
    from stage1.normalizer import _BUILTIN_MARKERS, _TECH_MARKERS

    def plain(marker: str) -> str:
        # Same whitespace/case normalisation as normalize_marker(), minus the index strip.
        value = marker.casefold().replace("ё", "е").strip()
        return re.sub(r"[\s.:;,_/\\|\-–—]+", " ", value)

    missing = sorted(
        plain(marker)
        for marker in set(_BUILTIN_MARKERS) | set(_TECH_MARKERS)
        if plain(marker) not in _SPACED_NUMBER_STRIPPABLE_PREFIXES
    )
    assert not missing, f"add to _SPACED_NUMBER_STRIPPABLE_PREFIXES: {missing}"


# --- Re-keying must address concrete rows: a stale row must never clobber or
# delete a row that already has (or is about to get) the correct key. ---


def _raw_insert_suppression(memory: NormalizationMemory, key: str, marker: str) -> None:
    memory._conn.execute(
        "INSERT INTO alias_suppressions(normalized_marker, marker, created_at) VALUES (?, ?, 'x')",
        (key, marker),
    )


def test_legacy_suppression_rekey_keeps_correct_rows_and_chains(tmp_path: Path) -> None:
    db = tmp_path / "memory.sqlite3"
    with NormalizationMemory(db) as memory:
        # Legacy rows: the old normalize_marker() stripped one spaced index.
        _raw_insert_suppression(memory, "сергей", "Сергей 2")  # stale -> 'сергей 2'
        _raw_insert_suppression(memory, "сергей 2", "Сергей 2 2")  # stale -> 'сергей 2 2'
        _raw_insert_suppression(memory, "анна", "Анна")  # correct, must stay
        _raw_insert_suppression(memory, "диктор", "Диктор 3")  # correct, must stay
        memory._conn.commit()
    with NormalizationMemory(db) as memory:
        rows = {
            r["marker"]: r["normalized_marker"]
            for r in memory._conn.execute("SELECT marker, normalized_marker FROM alias_suppressions")
        }
    assert rows == {
        "Сергей 2": "сергей 2",
        "Сергей 2 2": "сергей 2 2",
        "Анна": "анна",
        "Диктор 3": "диктор",
    }


def test_legacy_suppression_rekey_conflict_drops_only_the_stale_row(tmp_path: Path) -> None:
    db = tmp_path / "memory.sqlite3"
    with NormalizationMemory(db) as memory:
        _raw_insert_suppression(memory, "сергей", "Сергей 2")  # stale
        _raw_insert_suppression(memory, "сергей 2", "Сергей 2")  # correct duplicate of the target
        _raw_insert_suppression(memory, "анна", "Анна")
        memory._conn.commit()
    with NormalizationMemory(db) as memory:
        rows = sorted(
            tuple(r)
            for r in memory._conn.execute(
                "SELECT marker, normalized_marker FROM alias_suppressions"
            )
        )
    assert rows == [("Анна", "анна"), ("Сергей 2", "сергей 2")]


def test_legacy_alias_rekey_chain_does_not_lose_rows(tmp_path: Path) -> None:
    db = tmp_path / "memory.sqlite3"
    insert = (
        "INSERT INTO aliases(marker, normalized_marker, target_type, source, status,"
        " confirmations, corrections, confidence, created_at, updated_at, scope)"
        " VALUES (?, ?, ?, 'manual', 'active', 1, 0, 1.0, 'x', 'x', ?)"
    )
    with NormalizationMemory(db) as memory:
        memory._conn.execute(insert, ("Сергей 2", "сергей", "sync", "marker"))  # stale
        memory._conn.execute(insert, ("Сергей 2 2", "сергей 2", "sync", "marker"))  # stale
        memory._conn.execute(insert, ("Сергей", "сергей", "voiceover", "marker"))  # correct
        memory._conn.execute(insert, ("Сергей 2", "сергей", "sync", "video_cue"))  # other scope, stale
        memory._conn.commit()
    with NormalizationMemory(db) as memory:
        rows = sorted(
            tuple(r)
            for r in memory._conn.execute(
                "SELECT marker, normalized_marker, target_type, scope FROM aliases"
            )
        )
    assert rows == [
        ("Сергей", "сергей", "voiceover", "marker"),
        ("Сергей 2", "сергей 2", "sync", "marker"),
        ("Сергей 2", "сергей 2", "sync", "video_cue"),
        ("Сергей 2 2", "сергей 2 2", "sync", "marker"),
    ]
