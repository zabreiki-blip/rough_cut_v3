from __future__ import annotations

from pathlib import Path
import sqlite3
import threading

import pytest

from stage1.normalization_memory import (
    NormalizationMemory,
    project_memory_path,
    resolve_memory_path,
)

from test_normalization_memory_scope import _create_legacy_db


def _seed_global(path: Path) -> None:
    with NormalizationMemory(path) as memory:
        memory.record_manual("бюст", "voiceover")
        memory.record_manual("лайф", "technical", scope="video_cue")
        memory.record_llm_confirmation("ЗК.", "technical", "h1")
        memory.record_llm_confirmation("ЗК.", "technical", "h2")  # active learned rule
        memory.record_correction_confirmation("foo", "voiceover", "h3")  # candidate
        memory.suppress_marker("шум")


def test_project_memory_path_is_inside_work_dir(tmp_path: Path) -> None:
    work = tmp_path / "proj" / "work"
    assert project_memory_path(work) == work / "normalization_memory.sqlite3"


def test_default_mode_isolates_projects_and_does_not_touch_global(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NORMALIZATION_MEMORY_SCOPE", raising=False)
    global_path = tmp_path / "data" / "normalization_memory.sqlite3"
    _seed_global(global_path)
    path_a = resolve_memory_path(tmp_path / "a" / "work", global_path=global_path)
    path_b = resolve_memory_path(tmp_path / "b" / "work", global_path=global_path)
    assert path_a != path_b != global_path

    with NormalizationMemory(path_a) as mem:
        mem.record_manual("только_a", "voiceover")
        mem.record_correction_confirmation("a_learned", "technical", "x1")

    with NormalizationMemory(path_b) as mem:
        markers = {r["normalized_marker"] for r in mem.list_rules()}
        assert "только a" not in markers and "a learned" not in markers

    with NormalizationMemory(global_path) as mem:
        markers = {r["normalized_marker"] for r in mem.list_rules()}
        assert "только a" not in markers and "a learned" not in markers


def test_first_open_seeds_only_manual_rules_and_suppressions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NORMALIZATION_MEMORY_SCOPE", raising=False)
    global_path = tmp_path / "data" / "normalization_memory.sqlite3"
    _seed_global(global_path)
    path = resolve_memory_path(tmp_path / "p" / "work", global_path=global_path)

    with NormalizationMemory(path) as mem:
        rules = mem.list_rules()
        assert {r["source"] for r in rules} == {"manual"}
        assert {(r["normalized_marker"], r["scope"]) for r in rules} == {
            ("бюст", "marker"),
            ("лайф", "video_cue"),
        }
        assert mem.resolve_exact("бюст")["rule"]["target_type"] == "voiceover"
        assert mem.is_suppressed("шум")
        assert mem.resolve_exact("зк") is None  # learned rule did not leak

    # global source is untouched
    with NormalizationMemory(global_path) as mem:
        assert len(mem.list_rules()) == 4


def test_seed_happens_once_and_later_global_changes_do_not_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NORMALIZATION_MEMORY_SCOPE", raising=False)
    global_path = tmp_path / "g.sqlite3"
    _seed_global(global_path)
    work = tmp_path / "p" / "work"
    path = resolve_memory_path(work, global_path=global_path)
    with NormalizationMemory(path) as mem:
        mem.delete_rule(mem.resolve_exact("бюст")["rule"]["id"])
    with NormalizationMemory(global_path) as mem:
        mem.record_manual("новое", "voiceover")
    assert resolve_memory_path(work, global_path=global_path) == path
    with NormalizationMemory(path) as mem:
        assert mem.resolve_exact("бюст") is None  # not re-seeded
        assert mem.resolve_exact("новое") is None


def test_seed_from_legacy_schema_without_scope_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NORMALIZATION_MEMORY_SCOPE", raising=False)
    legacy = tmp_path / "legacy.sqlite3"
    _create_legacy_db(legacy)
    raw = sqlite3.connect(str(legacy))
    raw.execute(
        """INSERT INTO aliases (marker, normalized_marker, target_type, source, status,
           confirmations, corrections, confidence, created_at, updated_at)
           VALUES ('Диктор', 'диктор', 'voiceover', 'manual', 'active', 1, 0, 1.0, 'x', 'x')"""
    )
    raw.commit()
    raw.close()
    path = resolve_memory_path(tmp_path / "p" / "work", global_path=legacy)
    with NormalizationMemory(path) as mem:
        assert [r["normalized_marker"] for r in mem.list_rules()] == ["диктор"]


def test_missing_global_db_yields_empty_project_db_and_not_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NORMALIZATION_MEMORY_SCOPE", raising=False)
    global_path = tmp_path / "data" / "none.sqlite3"
    path = resolve_memory_path(tmp_path / "p" / "work", global_path=global_path)
    assert path.is_file()
    assert not global_path.exists()
    with NormalizationMemory(path) as mem:
        assert mem.list_rules() == []


def test_global_mode_env_returns_legacy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NORMALIZATION_MEMORY_SCOPE", "global")
    global_path = tmp_path / "g.sqlite3"
    assert resolve_memory_path(tmp_path / "p" / "work", global_path=global_path) == global_path
    assert not (tmp_path / "p" / "work").exists()


def test_invalid_scope_env_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NORMALIZATION_MEMORY_SCOPE", "bogus")
    with pytest.raises(ValueError):
        resolve_memory_path(tmp_path / "w", global_path=tmp_path / "g.sqlite3")


def _make_wal_legacy(path: Path) -> None:
    _create_legacy_db(path)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.close()


def test_migration_takes_write_lock_before_reading_row_count(tmp_path: Path) -> None:
    """A row committed by another writer while the migration waits for the lock
    must be migrated, not trigger a row-count mismatch (needs BEGIN IMMEDIATE)."""
    db_path = tmp_path / "legacy.sqlite3"
    _make_wal_legacy(db_path)

    holder = sqlite3.connect(str(db_path), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    errors: list[BaseException] = []
    result: dict[str, int] = {}

    def migrate() -> None:
        try:
            with NormalizationMemory(db_path) as memory:
                result["rules"] = len(memory.list_rules())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=migrate)
    thread.start()
    thread.join(timeout=0.5)
    assert thread.is_alive(), "migration should be waiting on the writer lock"
    holder.execute(
        """INSERT INTO aliases (marker, normalized_marker, target_type, source, status,
           confirmations, corrections, confidence, created_at, updated_at)
           VALUES ('late', 'late', 'voiceover', 'manual', 'active', 1, 0, 1.0, 'x', 'x')"""
    )
    holder.execute("COMMIT")
    holder.close()
    thread.join(timeout=10)

    assert not errors, errors
    assert result["rules"] == 3


def test_two_concurrent_migrations_do_not_conflict(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    _make_wal_legacy(db_path)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    counts: list[int] = []

    def migrate() -> None:
        try:
            barrier.wait(timeout=5)
            with NormalizationMemory(db_path) as memory:
                counts.append(len(memory.list_rules()))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=migrate) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, errors
    assert counts == [2, 2]
