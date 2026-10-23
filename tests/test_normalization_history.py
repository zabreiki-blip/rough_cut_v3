from __future__ import annotations

import json
from pathlib import Path

import pytest

from stage1.normalization_memory import NormalizationMemory
from stage1.normalizer import (
    SourceChangedError,
    confirm_normalization,
    diff_normalization_payloads,
    diff_normalization_snapshot,
    generate_normalization,
    list_normalization_history,
    restore_normalization_snapshot,
    save_normalization,
)

SOURCE_TEXT = (
    "ЗК Первая история о реке и рыбаках.\n"
    "СНХ Мария Иванова (00:30.00) Рассказ героини про реку.\n"
    "Особый: Реплика без известной метки.\n"
    "ЗК Финальная история о доме.\n"
)


class _Project:
    def __init__(self, root: Path) -> None:
        self.work = root / "work"
        self.work.mkdir()
        self.source = root / "scenario.txt"
        self.source.write_text(SOURCE_TEXT, encoding="utf-8")
        self.normalized = self.work / "scenario_normalized.json"
        self.prepared = self.work / "scenario_prepared.json"
        self.raw = self.work / "scenario_raw.txt"
        self.memory = root / "memory.sqlite3"

    def generate(self) -> dict:
        return generate_normalization(
            self.source, self.normalized, memory_path=self.memory, use_llm=False
        )

    def confirmed(self) -> dict:
        draft = self.generate()
        for block in draft["blocks"]:
            if block["type"] == "unknown":
                block["type"] = "technical"
        saved = save_normalization(self.normalized, draft, expected_revision=draft["revision"])
        return confirm_normalization(
            self.normalized,
            self.prepared,
            self.raw,
            memory_path=self.memory,
            expected_revision=saved["revision"],
            learning_decisions=[],
        )

    def teach_marker(self) -> None:
        with NormalizationMemory(self.memory) as memory:
            memory.record_manual("Особый", "sync")


@pytest.fixture
def project(tmp_path: Path) -> _Project:
    return _Project(tmp_path)


def test_first_generation_creates_no_snapshot(project: _Project) -> None:
    project.generate()
    assert list_normalization_history(project.normalized) == []


def test_regenerating_confirmed_scenario_keeps_snapshot_and_prepared(project: _Project) -> None:
    confirmed = project.confirmed()
    prepared_before = project.prepared.read_bytes()
    raw_before = project.raw.read_bytes()

    draft = project.generate()

    history = list_normalization_history(project.normalized)
    assert len(history) == 1
    assert history[0]["status"] == "confirmed"
    assert history[0]["revision"] == confirmed["revision"]
    assert history[0]["path"].startswith(str(project.work / "normalization_history"))
    snapshot = json.loads(Path(history[0]["path"]).read_text(encoding="utf-8"))
    assert snapshot == json.loads(json.dumps(confirmed))
    assert draft["status"] == "draft"
    assert draft["revision"] == confirmed["revision"] + 1
    assert project.prepared.read_bytes() == prepared_before
    assert project.raw.read_bytes() == raw_before
    assert [b["uid"] for b in draft["blocks"]] == [b["uid"] for b in confirmed["blocks"]]
    assert [b["scene_uid"] for b in draft["blocks"]] == [
        b["scene_uid"] for b in confirmed["blocks"]
    ]


def test_regeneration_keeps_manual_edits_and_diff_shows_parser_change(project: _Project) -> None:
    confirmed = project.confirmed()
    assert confirmed["blocks"][2]["type"] == "technical"
    project.teach_marker()  # the "new parser result" differs from the confirmed one

    draft = project.generate()
    assert draft["blocks"][2]["type"] == "technical"  # manual decision is pinned
    assert draft["blocks"][2]["source"] == "manual"

    snapshot = list_normalization_history(project.normalized)[0]["name"]
    diff = diff_normalization_snapshot(project.normalized, snapshot)
    assert set(diff) >= {"summary", "changes", "snapshot", "current"}
    assert diff["snapshot"]["status"] == "confirmed"
    assert diff["current"]["status"] == "draft"
    assert diff["summary"] == {"unchanged": 3, "edited": 1}
    edited = next(c for c in diff["changes"] if c["status"] == "edited")
    assert edited["old_block_uid"] == draft["blocks"][2]["uid"]
    assert edited["old_text"].startswith("Особый:")  # label now recognised by the new parse
    assert edited["new_type"] == "technical"


def test_diff_payloads_is_pure_and_reports_changes() -> None:
    def block(uid: str, text: str, kind: str = "voiceover") -> dict:
        return {"uid": uid, "type": kind, "normalized_text": text, "original_text": text}

    old = {"revision": 1, "status": "confirmed", "blocks": [block("a", "Один"), block("b", "Два")]}
    new = {
        "revision": 2,
        "status": "draft",
        "blocks": [block("a", "Один"), block("b", "Два", "technical"), block("c", "Три")],
    }
    before = json.dumps([old, new], sort_keys=True)
    diff = diff_normalization_payloads(old, new)
    assert json.dumps([old, new], sort_keys=True) == before
    assert diff["summary"] == {"unchanged": 1, "type_changed": 1, "added": 1}
    assert diff["snapshot"] == {"revision": 1, "status": "confirmed", "blocks": 2}
    assert diff["current"] == {"revision": 2, "status": "draft", "blocks": 3}


def test_restore_snapshot_returns_old_blocks_as_draft_and_is_reversible(project: _Project) -> None:
    confirmed = project.confirmed()
    prepared_before = project.prepared.read_bytes()
    draft = project.generate()
    draft["blocks"][0]["normalized_text"] = "Правка после повторного разбора."
    edited = save_normalization(project.normalized, draft, expected_revision=draft["revision"])
    name = list_normalization_history(project.normalized)[-1]["name"]  # oldest = confirmed

    restored = restore_normalization_snapshot(
        project.normalized, name, expected_revision=edited["revision"]
    )

    assert restored["revision"] == edited["revision"] + 1
    assert restored["status"] == "draft"
    assert restored["confirmed_at"] == ""
    assert [b["normalized_text"] for b in restored["blocks"]] == [
        b["normalized_text"] for b in confirmed["blocks"]
    ]
    assert [b["uid"] for b in restored["blocks"]] == [b["uid"] for b in confirmed["blocks"]]
    assert json.loads(project.normalized.read_text(encoding="utf-8")) == restored
    assert project.prepared.read_bytes() == prepared_before
    # the replaced draft (with the user edit) is itself kept
    texts = {
        json.loads(Path(item["path"]).read_text(encoding="utf-8"))["blocks"][0]["normalized_text"]
        for item in list_normalization_history(project.normalized)
    }
    assert "Правка после повторного разбора." in texts


def test_restore_rejects_bad_names_conflicts_and_other_source(project: _Project) -> None:
    project.confirmed()
    draft = project.generate()
    name = list_normalization_history(project.normalized)[0]["name"]

    for bad in ("../scenario_normalized.json", "/etc/passwd", "missing.json", ""):
        with pytest.raises((ValueError, FileNotFoundError)):
            restore_normalization_snapshot(project.normalized, bad)
    with pytest.raises(Exception, match="Expected revision"):
        restore_normalization_snapshot(project.normalized, name, expected_revision=999)

    project.source.write_text(SOURCE_TEXT + "Новый абзац.\n", encoding="utf-8")
    with pytest.raises(SourceChangedError):
        restore_normalization_snapshot(project.normalized, name)
    assert (
        json.loads(project.normalized.read_text(encoding="utf-8"))["revision"] == draft["revision"]
    )


def test_old_project_without_new_fields_is_readable(project: _Project) -> None:
    confirmed = project.confirmed()
    legacy = json.loads(project.normalized.read_text(encoding="utf-8"))
    for block in legacy["blocks"]:
        for field in ("scene_uid", "manual_fields", "reviewed_warnings", "manually_edited"):
            block.pop(field, None)
    project.normalized.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    draft = project.generate()
    assert [b["uid"] for b in draft["blocks"]] == [b["uid"] for b in confirmed["blocks"]]
    name = list_normalization_history(project.normalized)[0]["name"]
    diff = diff_normalization_snapshot(project.normalized, name)
    assert sum(diff["summary"].values()) == len(confirmed["blocks"])
    restored = restore_normalization_snapshot(project.normalized, name)
    assert restored["status"] == "draft"
    assert all("manual_fields" in b for b in restored["blocks"])


def test_history_is_capped_to_the_newest_snapshots(project: _Project, monkeypatch) -> None:
    import stage1.normalizer as normalizer

    monkeypatch.setattr(normalizer, "MAX_NORMALIZATION_HISTORY", 3)
    project.generate()
    for _ in range(6):
        project.generate()
    history = list_normalization_history(project.normalized)
    assert len(history) == 3
    assert [item["revision"] for item in history] == [6, 5, 4]


def test_history_name_with_trailing_newline_is_rejected(project: _Project) -> None:
    project.generate()
    project.generate()
    (name,) = [item["name"] for item in list_normalization_history(project.normalized)]
    with pytest.raises(ValueError):
        diff_normalization_snapshot(project.normalized, name + "\n")
    with pytest.raises(ValueError):
        restore_normalization_snapshot(project.normalized, name + "\n")


def test_reparse_keeps_valid_absorbed_link_and_drops_dangling_one(project: _Project) -> None:
    draft = project.generate()
    first, _second, _third, fourth = draft["blocks"]
    first["absorbed_by_uid"] = fourth["uid"]
    first["manual_fields"] = ["absorbed_by_uid"]
    fourth["absorbed_by_uid"] = "block-gone-00000000"
    fourth["manual_fields"] = ["absorbed_by_uid"]
    project.normalized.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")

    regenerated = project.generate()
    first, *_middle, fourth = regenerated["blocks"]
    assert first["absorbed_by_uid"] == fourth["uid"]
    assert fourth["absorbed_by_uid"] == ""
    assert any("Absorbed-by link dropped" in w for w in fourth["warnings"])
    assert not any("Absorbed-by link dropped" in w for w in first["warnings"])
