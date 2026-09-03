from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from stage1.normalization_memory import NormalizationMemory


def _create_legacy_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                marker TEXT NOT NULL,
                normalized_marker TEXT NOT NULL,
                target_type TEXT NOT NULL,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                confirmations INTEGER NOT NULL DEFAULT 0,
                corrections INTEGER NOT NULL DEFAULT 0,
                confidence REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(normalized_marker, target_type, source)
            );
            CREATE INDEX aliases_marker_idx ON aliases(normalized_marker);
            CREATE TABLE alias_confirmations (
                alias_id INTEGER NOT NULL REFERENCES aliases(id) ON DELETE CASCADE,
                source_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(alias_id, source_hash)
            );
            CREATE TABLE alias_suppressions (
                normalized_marker TEXT PRIMARY KEY,
                marker TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.executemany(
            """INSERT INTO aliases
               (id, marker, normalized_marker, target_type, source, status,
                confirmations, corrections, confidence, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (11, "Голос «Лили»", "голос «лили»", "voiceover", "llm", "candidate",
                 1, 0, 0.7, "2026-07-21T12:27:22.391874+00:00", "2026-07-21T12:27:22.391874+00:00"),
                (25, "ЗК.", "зк", "technical", "correction", "candidate",
                 1, 0, 0.7, "2026-08-12T09:33:45.808622+00:00", "2026-08-12T09:33:45.808622+00:00"),
            ],
        )
        conn.executemany(
            "INSERT INTO alias_confirmations(alias_id, source_hash, created_at) VALUES (?, ?, ?)",
            [
                (11, "hash-a", "2026-07-21T12:27:22.393418+00:00"),
                (25, "hash-b", "2026-08-12T09:33:45.844594+00:00"),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_migration_preserves_legacy_rows_and_confirmations(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    _create_legacy_db(db_path)

    with NormalizationMemory(db_path) as memory:
        rules = memory.list_rules()
        assert len(rules) == 2
        by_id = {rule["id"]: rule for rule in rules}
        assert by_id[11]["scope"] == "marker"
        assert by_id[11]["marker"] == "Голос «Лили»"
        assert by_id[11]["confirmations"] == 1
        assert by_id[11]["corrections"] == 0
        assert by_id[11]["confidence"] == 0.7
        assert by_id[11]["created_at"] == "2026-07-21T12:27:22.391874+00:00"
        assert by_id[25]["scope"] == "marker"
        assert by_id[25]["source"] == "correction"
        assert by_id[25]["status"] == "candidate"

    raw = sqlite3.connect(str(db_path))
    try:
        confirmations = raw.execute(
            "SELECT alias_id, source_hash FROM alias_confirmations ORDER BY alias_id"
        ).fetchall()
        assert confirmations == [(11, "hash-a"), (25, "hash-b")]
        cols = [row[1] for row in raw.execute("PRAGMA table_info(aliases)")]
        assert "scope" in cols
    finally:
        raw.close()


def test_reopening_migrated_db_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    _create_legacy_db(db_path)

    with NormalizationMemory(db_path) as memory:
        first = memory.list_rules()

    with NormalizationMemory(db_path) as memory:
        second = memory.list_rules()

    assert first == second
    assert len(second) == 2


def test_video_cue_and_marker_rules_coexist_without_conflict(tmp_path: Path) -> None:
    with NormalizationMemory(tmp_path / "memory.sqlite3") as memory:
        marker_rule = memory.record_manual("бюст", "voiceover")
        cue_rule = memory.create_rule(
            "бюст", "technical", source="manual", status="active", scope="video_cue"
        )

        assert marker_rule["scope"] == "marker"
        assert cue_rule["scope"] == "video_cue"
        assert marker_rule["status"] == "active"
        assert cue_rule["status"] == "active"

        marker_resolved = memory.resolve_exact("бюст", scope="marker")
        cue_resolved = memory.resolve_exact("бюст", scope="video_cue")
        assert marker_resolved["conflict"] is False
        assert marker_resolved["rule"]["target_type"] == "voiceover"
        assert cue_resolved["conflict"] is False
        assert cue_resolved["rule"]["target_type"] == "technical"


def test_resolve_exact_and_suggest_are_scoped(tmp_path: Path) -> None:
    with NormalizationMemory(tmp_path / "memory.sqlite3") as memory:
        memory.record_manual("бюст", "voiceover")
        memory.create_rule(
            "бюстгальтер", "technical", source="manual", status="active", scope="video_cue"
        )

        assert memory.resolve_exact("бюстгальтер", scope="marker") is None
        assert memory.resolve_exact("бюст", scope="video_cue") is None

        suggestion_marker_scope = memory.suggest("бюстгальтеры", scope="marker")
        assert suggestion_marker_scope is None

        suggestion_video_scope = memory.suggest("бюстгальтер!", scope="video_cue")
        assert suggestion_video_scope is not None
        assert suggestion_video_scope["rule"]["scope"] == "video_cue"


def test_candidate_to_active_learning_cycle_works_within_video_cue_scope(tmp_path: Path) -> None:
    with NormalizationMemory(tmp_path / "memory.sqlite3") as memory:
        rule = memory.record_llm_confirmation(
            "бюст", "technical", "hash-1", scope="video_cue"
        )
        assert rule["status"] == "candidate"
        assert rule["scope"] == "video_cue"

        rule = memory.record_llm_confirmation(
            "бюст", "technical", "hash-2", scope="video_cue"
        )
        assert rule["status"] == "active"
        assert rule["confirmations"] == 2

        assert memory.resolve_exact("бюст", scope="marker") is None
        resolved = memory.resolve_exact("бюст", scope="video_cue")
        assert resolved["conflict"] is False
        assert resolved["rule"]["target_type"] == "technical"


def test_unknown_scope_raises_value_error(tmp_path: Path) -> None:
    with NormalizationMemory(tmp_path / "memory.sqlite3") as memory:
        with pytest.raises(ValueError):
            memory.create_rule("бюст", "technical", source="manual", scope="unknown_scope")
        with pytest.raises(ValueError):
            memory.resolve_exact("бюст", scope="unknown_scope")
        with pytest.raises(ValueError):
            memory.suggest("бюст", scope="unknown_scope")
        with pytest.raises(ValueError):
            memory.list_rules(scope="unknown_scope")
