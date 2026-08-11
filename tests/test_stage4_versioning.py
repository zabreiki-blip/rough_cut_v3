from __future__ import annotations

import hashlib
from pathlib import Path

from api_core_routes import _active_revision_input_path
from atomic_io import atomic_write_json
from project_service import ensure_project
from stage4.versioning import (
    RevisionStateConflictError,
    _inherit_blocks,
    activate_revision,
    compare_blocks,
    ensure_baseline,
    import_revision,
    reconcile_revision_state,
    repair_active_revision_metadata,
)


def _block(uid: str, text: str, block_type: str = "sync", speaker: str = "Иван") -> dict:
    return {
        "uid": uid, "normalized_text": text, "original_text": text,
        "type": block_type, "speaker": speaker, "original_label": speaker,
        "text_for_tts": text if block_type == "voiceover" else "",
    }


def test_compare_detects_insert_edit_and_move_without_shifting_every_block() -> None:
    old = [_block("a", "Первый"), _block("b", "Второй"), _block("c", "Третий")]
    new = [
        _block("n1", "Новый"), _block("n2", "Первый"),
        _block("n3", "Третий"), _block("n4", "Второй расширенный"),
    ]

    result = compare_blocks(old, new)

    by_new = {item["new_index"]: item for item in result["changes"] if item["new_index"] is not None}
    assert by_new[0]["status"] == "added"
    assert by_new[1]["status"] == "unchanged"
    assert by_new[2]["status"] == "unchanged"
    assert by_new[3]["status"] == "edited"


def test_exact_text_downstream_change_is_not_reused_or_overwritten() -> None:
    old = [_block("stable", "Одинаковый", "voiceover", "Диктор")]
    old[0]["text_for_tts"] = "Старое произношение"
    new = [_block("imported", "Одинаковый", "voiceover", "Диктор")]
    new[0]["text_for_tts"] = "Новое произношение"

    diff = compare_blocks(old, new)
    _inherit_blocks(old, new, diff)

    assert diff["changes"][0]["status"] == "edited"
    assert diff["changes"][0]["downstream_changed_fields"] == ["text_for_tts"]
    assert new[0]["text_for_tts"] == "Новое произношение"


def test_import_snapshots_baseline_and_inherits_stable_uid(tmp_path: Path) -> None:
    paths = ensure_project("demo", tmp_path / "projects")
    source_v1 = paths["input"] / "scenario.docx"
    source_v2 = paths["input"] / "v002.docx"
    source_v1.write_bytes(b"v1")
    source_v2.write_bytes(b"v2")
    old = {
        "source": str(source_v1),
        "source_hash": hashlib.sha256(b"v1").hexdigest(),
        "status": "confirmed", "blocks": [_block("stable-a", "Одинаковый")],
    }
    atomic_write_json(paths["normalized"], old)
    atomic_write_json(paths["sync_matches"], {
        "schema_version": 1,
        "matches": [{"segment_id": "stable-a", "metadata": {"parent_block_uid": "stable-a"}}],
    })

    def fake_generate(input_path, output_path, **_kwargs):
        value = {
            "source": str(input_path), "source_hash": hashlib.sha256(b"v2").hexdigest(),
            "status": "draft", "blocks": [_block("import-a", "Одинаковый")],
        }
        atomic_write_json(output_path, value)
        return value

    result = import_revision(
        "demo", paths, generate_normalization=fake_generate,
        memory_path=tmp_path / "memory.sqlite3", input_path=source_v2,
    )

    assert result["revision"]["revision_id"] == "rev_0002"
    assert result["diff"]["summary"] == {"unchanged": 1}
    normalized = __import__("json").loads(
        (paths["scenario_versions"] / "rev_0002.normalized.json").read_text(encoding="utf-8")
    )
    assert normalized["blocks"][0]["uid"] == "stable-a"
    assert normalized["source"] == str(
        (paths["scenario_versions"] / "rev_0002.source.docx").resolve()
    )

    activated = activate_revision("rev_0002", paths)
    assert activated["reused_sync_matches"] == 1
    assert (paths["scenario_versions"] / "rev_0001.source.docx").read_bytes() == b"v1"

    paths["sync_matches"].write_text('{"schema_version": 1, "matches": []}\n', encoding="utf-8")
    rolled_back = activate_revision("rev_0001", paths)
    assert rolled_back["reused_sync_matches"] == 1


def test_active_revision_selects_its_scenario_instead_of_first_input(tmp_path: Path) -> None:
    paths = ensure_project("demo", tmp_path / "projects")
    input_files = [
        "projects/demo/input/scenario.doc",
        "projects/demo/input/v002_scenario.docx",
    ]
    atomic_write_json(paths["scenario_versions"] / "rev_0002.json", {
        "revision_id": "rev_0002",
        "source_file": "v002_scenario.docx",
    })
    atomic_write_json(paths["work"] / "active_revision.json", {
        "revision_id": "rev_0002",
    })

    selected = _active_revision_input_path(paths, input_files)

    assert selected == "projects/demo/input/v002_scenario.docx"


def test_repair_restores_revision_status_after_legacy_confirmation_roundtrip(
    tmp_path: Path,
) -> None:
    paths = ensure_project("demo", tmp_path / "projects")
    snapshot = {
        "status": "draft",
        "scenario_revision_id": "rev_0002",
        "parent_revision_id": "rev_0001",
        "blocks": [
            {**_block("old-a", "Без изменений"), "versioning": {"change_status": "unchanged"}},
            {**_block("old-b", "Новая реплика"), "versioning": {"change_status": "added"}},
        ],
    }
    current = {
        "status": "confirmed",
        "revision": 2,
        "blocks": [
            _block("regenerated-a", "Без изменений"),
            _block("regenerated-b", "Новая реплика"),
        ],
    }
    atomic_write_json(paths["scenario_versions"] / "rev_0002.normalized.json", snapshot)
    atomic_write_json(paths["scenario_versions"] / "rev_0002.json", {
        "revision_id": "rev_0002",
        "parent_revision_id": "rev_0001",
        "normalized_snapshot": "rev_0002.normalized.json",
    })
    atomic_write_json(paths["work"] / "active_revision.json", {"revision_id": "rev_0002"})
    atomic_write_json(paths["normalized"], current)

    result = repair_active_revision_metadata(paths)

    repaired = __import__("json").loads(paths["normalized"].read_text(encoding="utf-8"))
    assert result == {"repaired": True, "revision_id": "rev_0002", "blocks": 2}
    assert repaired["status"] == "confirmed"
    assert repaired["scenario_revision_id"] == "rev_0002"
    assert repaired["parent_revision_id"] == "rev_0001"
    assert [block["uid"] for block in repaired["blocks"]] == [
        "regenerated-a", "regenerated-b",
    ]
    assert [block["versioning"]["change_status"] for block in repaired["blocks"]] == [
        "unchanged", "added",
    ]


def test_import_preserves_new_speaker_and_type_semantics(tmp_path: Path) -> None:
    paths = ensure_project("demo", tmp_path / "projects")
    source_v1 = paths["input"] / "v1.docx"
    source_v2 = paths["input"] / "v2.docx"
    source_v1.write_bytes(b"v1")
    source_v2.write_bytes(b"v2")
    atomic_write_json(paths["normalized"], {
        "source": str(source_v1),
        "source_hash": hashlib.sha256(b"v1").hexdigest(),
        "status": "confirmed",
        "blocks": [
            _block("speaker", "Та же реплика", speaker="Иван"),
            _block("kind", "Тот же текст", block_type="sync"),
        ],
    })

    def fake_generate(input_path, output_path, **_kwargs):
        value = {
            "source": str(input_path),
            "source_hash": hashlib.sha256(b"v2").hexdigest(),
            "status": "draft",
            "blocks": [
                _block("new-speaker", "Та же реплика", speaker="Пётр"),
                _block("new-kind", "Тот же текст", block_type="voiceover"),
            ],
        }
        atomic_write_json(output_path, value)
        return value

    imported = import_revision(
        "demo", paths, generate_normalization=fake_generate,
        memory_path=tmp_path / "memory.sqlite3", input_path=source_v2,
    )
    normalized = __import__("json").loads(
        (paths["scenario_versions"] / "rev_0002.normalized.json").read_text()
    )

    assert imported["diff"]["summary"] == {"speaker_changed": 1, "type_changed": 1}
    assert normalized["blocks"][0]["speaker"] == "Пётр"
    assert normalized["blocks"][1]["type"] == "voiceover"


def test_import_uses_valid_active_revision_as_parent_but_next_free_id(
    tmp_path: Path,
) -> None:
    paths = ensure_project("demo", tmp_path / "projects")
    source_v1 = paths["input"] / "v1.docx"
    source_v1.write_bytes(b"v1")
    atomic_write_json(paths["normalized"], {
        "source": str(source_v1), "source_hash": hashlib.sha256(b"v1").hexdigest(),
        "status": "confirmed", "blocks": [_block("one", "Первая")],
    })

    def import_file(name: str, payload: bytes, text: str) -> dict:
        source = paths["input"] / name
        source.write_bytes(payload)
        def generate(input_path, output_path, **_kwargs):
            value = {
                "source": str(input_path), "source_hash": hashlib.sha256(payload).hexdigest(),
                "status": "draft", "blocks": [_block(name, text)],
            }
            atomic_write_json(output_path, value)
            return value
        return import_revision(
            "demo", paths, generate_normalization=generate,
            memory_path=tmp_path / "memory.sqlite3", input_path=source,
        )

    import_file("v2.docx", b"v2", "Вторая")
    activate_revision("rev_0002", paths)
    import_file("v3.docx", b"v3", "Третья")
    activate_revision("rev_0001", paths)
    imported = import_file("v4.docx", b"v4", "Первая новая")

    assert imported["revision"]["revision_id"] == "rev_0004"
    assert imported["revision"]["parent_revision_id"] == "rev_0001"
    assert imported["diff"]["from_revision_id"] == "rev_0001"


def test_activation_refuses_mismatched_work_without_overwriting_snapshot(
    tmp_path: Path,
) -> None:
    paths = ensure_project("demo", tmp_path / "projects")
    source = paths["input"] / "v1.docx"
    source.write_bytes(b"v1")
    original = {
        "source": str(source), "source_hash": hashlib.sha256(b"v1").hexdigest(),
        "status": "confirmed", "blocks": [_block("one", "Первая")],
    }
    atomic_write_json(paths["normalized"], original)
    ensure_baseline("demo", paths)
    snapshot = paths["scenario_versions"] / "rev_0001.normalized.json"
    before = snapshot.read_bytes()
    atomic_write_json(paths["normalized"], {
        "scenario_revision_id": "rev_9999", "blocks": [_block("other", "Чужой")],
    })

    import pytest
    with pytest.raises(RevisionStateConflictError, match="does not match"):
        activate_revision("rev_0001", paths)

    assert snapshot.read_bytes() == before


def test_reconcile_backs_up_divergent_work_and_restores_selected_revision(
    tmp_path: Path,
) -> None:
    paths = ensure_project("demo", tmp_path / "projects")
    source = paths["input"] / "v1.docx"
    source.write_bytes(b"v1")
    atomic_write_json(paths["normalized"], {
        "source": str(source),
        "source_hash": hashlib.sha256(b"v1").hexdigest(),
        "status": "confirmed",
        "revision": 2,
        "blocks": [_block("one", "Первая")],
    })
    ensure_baseline("demo", paths)
    divergent = {
        "scenario_revision_id": "",
        "status": "draft",
        "revision": 1,
        "blocks": [_block("other", "Несохранённый черновик")],
    }
    atomic_write_json(paths["normalized"], divergent)

    result = reconcile_revision_state("rev_0001", paths)

    backup = paths["root"] / result["recovery_backup"] / "work" / "scenario_normalized.json"
    assert result["reconciled"] is True
    assert __import__("json").loads(backup.read_text(encoding="utf-8"))["blocks"][0]["uid"] == "other"
    restored = __import__("json").loads(paths["normalized"].read_text(encoding="utf-8"))
    assert restored["scenario_revision_id"] == "rev_0001"
    assert restored["blocks"][0]["uid"] == "one"
