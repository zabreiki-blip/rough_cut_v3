from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import wave

import pytest

from api_core_routes import _active_revision_input_path
from atomic_io import atomic_write_json
from project_service import ensure_project
from stage4.revision_assets import revision_speaker_alias_dir
from stage4.versioning import (
    RevisionStateConflictError,
    _detect_revision_state,
    _inherit_blocks,
    _restore_exact_auto_classifications,
    activate_revision,
    compare_blocks,
    ensure_baseline,
    import_revision,
    inspect_revision_state,
    reconcile_revision_state,
    repair_active_revision_metadata,
)


def _block(uid: str, text: str, block_type: str = "sync", speaker: str = "Иван") -> dict:
    return {
        "uid": uid,
        "normalized_text": text,
        "original_text": text,
        "type": block_type,
        "speaker": speaker,
        "original_label": speaker,
        "text_for_tts": text if block_type == "voiceover" else "",
    }


def _write_wav(path: Path, marker: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(marker.to_bytes(2, "little", signed=True) * 80)


def test_compare_detects_insert_edit_and_move_without_shifting_every_block() -> None:
    old = [_block("a", "Первый"), _block("b", "Второй"), _block("c", "Третий")]
    new = [
        _block("n1", "Новый"),
        _block("n2", "Первый"),
        _block("n3", "Третий"),
        _block("n4", "Второй расширенный"),
    ]

    result = compare_blocks(old, new)

    by_new = {
        item["new_index"]: item for item in result["changes"] if item["new_index"] is not None
    }
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


def test_exact_text_unknown_in_new_revision_inherits_confirmed_classification() -> None:
    old = [_block("stable", "Неизменённый закадр", "voiceover", "Диктор")]
    new = [_block("imported", "Неизменённый закадр", "unknown", "")]
    new[0].update({"source": "llm", "text_for_tts": "", "role": ""})

    _restore_exact_auto_classifications(old, new)
    diff = compare_blocks(old, new)

    assert new[0]["type"] == "voiceover"
    assert new[0]["text_for_tts"] == "Неизменённый закадр"
    assert diff["summary"] == {"unchanged": 1}


def test_exact_text_llm_metadata_drift_inherits_confirmed_classification() -> None:
    old = [_block("stable", "Неизменённый закадр", "voiceover", "unknown")]
    old[0].update({"original_label": "", "role": "narrator", "source": "llm"})
    new = [_block("imported", "Неизменённый закадр", "voiceover", "narrator")]
    new[0].update({"original_label": "", "role": "narrator", "source": "llm"})

    _restore_exact_auto_classifications(old, new)
    diff = compare_blocks(old, new)

    assert new[0]["speaker"] == "unknown"
    assert diff["summary"] == {"unchanged": 1}


def test_speaker_title_spelling_variant_is_not_a_speaker_change() -> None:
    old = [_block("stable", "Текст синхрона", "sync", "ПРОТОИЕРЕЙ ИВАН ИВАНОВ")]
    new = [_block("imported", "Текст синхрона", "sync", "ПРОТОИЕРЕЙ ИВАН ИВАНОВ")]

    diff = compare_blocks(old, new)

    assert diff["summary"] == {"unchanged": 1}
    assert diff["changes"][0]["downstream_changed_fields"] == []


def test_import_snapshots_baseline_and_inherits_stable_uid(tmp_path: Path) -> None:
    paths = ensure_project("demo", tmp_path / "projects")
    source_v1 = paths["input"] / "scenario.docx"
    source_v2 = paths["input"] / "v002.docx"
    source_v1.write_bytes(b"v1")
    source_v2.write_bytes(b"v2")
    old = {
        "source": str(source_v1),
        "source_hash": hashlib.sha256(b"v1").hexdigest(),
        "status": "confirmed",
        "blocks": [_block("stable-a", "Одинаковый")],
    }
    atomic_write_json(paths["normalized"], old)
    atomic_write_json(
        paths["sync_matches"],
        {
            "schema_version": 1,
            "matches": [{"segment_id": "stable-a", "metadata": {"parent_block_uid": "stable-a"}}],
        },
    )

    def fake_generate(input_path, output_path, **_kwargs):
        value = {
            "source": str(input_path),
            "source_hash": hashlib.sha256(b"v2").hexdigest(),
            "status": "draft",
            "blocks": [_block("import-a", "Одинаковый")],
        }
        atomic_write_json(output_path, value)
        return value

    result = import_revision(
        "demo",
        paths,
        generate_normalization=fake_generate,
        memory_path=tmp_path / "memory.sqlite3",
        input_path=source_v2,
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
    atomic_write_json(
        paths["scenario_versions"] / "rev_0002.json",
        {
            "revision_id": "rev_0002",
            "source_file": "v002_scenario.docx",
        },
    )
    atomic_write_json(
        paths["work"] / "active_revision.json",
        {
            "revision_id": "rev_0002",
        },
    )

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
    atomic_write_json(
        paths["scenario_versions"] / "rev_0002.json",
        {
            "revision_id": "rev_0002",
            "parent_revision_id": "rev_0001",
            "normalized_snapshot": "rev_0002.normalized.json",
        },
    )
    atomic_write_json(paths["work"] / "active_revision.json", {"revision_id": "rev_0002"})
    atomic_write_json(paths["normalized"], current)

    result = repair_active_revision_metadata(paths)

    repaired = __import__("json").loads(paths["normalized"].read_text(encoding="utf-8"))
    assert result == {"repaired": True, "revision_id": "rev_0002", "blocks": 2}
    assert repaired["status"] == "confirmed"
    assert repaired["scenario_revision_id"] == "rev_0002"
    assert repaired["parent_revision_id"] == "rev_0001"
    assert [block["uid"] for block in repaired["blocks"]] == [
        "regenerated-a",
        "regenerated-b",
    ]
    assert [block["versioning"]["change_status"] for block in repaired["blocks"]] == [
        "unchanged",
        "added",
    ]


def test_import_preserves_new_speaker_and_type_semantics(tmp_path: Path) -> None:
    paths = ensure_project("demo", tmp_path / "projects")
    source_v1 = paths["input"] / "v1.docx"
    source_v2 = paths["input"] / "v2.docx"
    source_v1.write_bytes(b"v1")
    source_v2.write_bytes(b"v2")
    atomic_write_json(
        paths["normalized"],
        {
            "source": str(source_v1),
            "source_hash": hashlib.sha256(b"v1").hexdigest(),
            "status": "confirmed",
            "blocks": [
                _block("speaker", "Та же реплика", speaker="Иван"),
                _block("kind", "Тот же текст", block_type="sync"),
            ],
        },
    )

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
        "demo",
        paths,
        generate_normalization=fake_generate,
        memory_path=tmp_path / "memory.sqlite3",
        input_path=source_v2,
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
    atomic_write_json(
        paths["normalized"],
        {
            "source": str(source_v1),
            "source_hash": hashlib.sha256(b"v1").hexdigest(),
            "status": "confirmed",
            "blocks": [_block("one", "Первая")],
        },
    )

    def import_file(name: str, payload: bytes, text: str) -> dict:
        source = paths["input"] / name
        source.write_bytes(payload)

        def generate(input_path, output_path, **_kwargs):
            value = {
                "source": str(input_path),
                "source_hash": hashlib.sha256(payload).hexdigest(),
                "status": "draft",
                "blocks": [_block(name, text)],
            }
            atomic_write_json(output_path, value)
            return value

        return import_revision(
            "demo",
            paths,
            generate_normalization=generate,
            memory_path=tmp_path / "memory.sqlite3",
            input_path=source,
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
        "source": str(source),
        "source_hash": hashlib.sha256(b"v1").hexdigest(),
        "status": "confirmed",
        "blocks": [_block("one", "Первая")],
    }
    atomic_write_json(paths["normalized"], original)
    ensure_baseline("demo", paths)
    snapshot = paths["scenario_versions"] / "rev_0001.normalized.json"
    before = snapshot.read_bytes()
    atomic_write_json(
        paths["normalized"],
        {
            "scenario_revision_id": "rev_9999",
            "blocks": [_block("other", "Чужой")],
        },
    )

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
    atomic_write_json(
        paths["normalized"],
        {
            "source": str(source),
            "source_hash": hashlib.sha256(b"v1").hexdigest(),
            "status": "confirmed",
            "revision": 2,
            "blocks": [_block("one", "Первая")],
        },
    )
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
    assert (
        __import__("json").loads(backup.read_text(encoding="utf-8"))["blocks"][0]["uid"] == "other"
    )
    restored = __import__("json").loads(paths["normalized"].read_text(encoding="utf-8"))
    assert restored["scenario_revision_id"] == "rev_0001"
    assert restored["blocks"][0]["uid"] == "one"


def test_activation_interrupted_between_files_is_detected_and_recoverable(
    tmp_path: Path,
) -> None:
    paths = ensure_project("demo", tmp_path / "projects")
    source_v1 = paths["input"] / "v1.docx"
    source_v1.write_bytes(b"v1")
    atomic_write_json(
        paths["normalized"],
        {
            "source": str(source_v1),
            "source_hash": hashlib.sha256(b"v1").hexdigest(),
            "status": "confirmed",
            "blocks": [_block("one", "Первая")],
        },
    )
    ensure_baseline("demo", paths)

    source_v2 = paths["input"] / "v2.docx"
    source_v2.write_bytes(b"v2")

    def fake_generate(input_path, output_path, **_kwargs):
        value = {
            "source": str(input_path),
            "source_hash": hashlib.sha256(b"v2").hexdigest(),
            "status": "draft",
            "blocks": [_block("two", "Вторая")],
        }
        atomic_write_json(output_path, value)
        return value

    import_revision(
        "demo",
        paths,
        generate_normalization=fake_generate,
        memory_path=tmp_path / "memory.sqlite3",
        input_path=source_v2,
    )

    real_atomic_write_json = atomic_write_json
    calls = {"count": 0}

    def flaky_atomic_write_json(path, payload, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("simulated crash")
        return real_atomic_write_json(path, payload, *args, **kwargs)

    import pytest
    from unittest.mock import patch

    with patch("stage4.versioning.atomic_write_json", side_effect=flaky_atomic_write_json):
        with pytest.raises(RuntimeError, match="simulated crash"):
            activate_revision("rev_0002", paths)

    normalized_after_crash = __import__("json").loads(
        paths["normalized"].read_text(encoding="utf-8")
    )
    active_after_crash = __import__("json").loads(
        (paths["work"] / "active_revision.json").read_text(encoding="utf-8")
    )
    assert normalized_after_crash["scenario_revision_id"] == "rev_0002"
    assert active_after_crash["revision_id"] == "rev_0001"

    with pytest.raises(RevisionStateConflictError, match="does not match"):
        activate_revision("rev_0001", paths)

    # The journal survives the crash, so a status check would self-heal.  This
    # test covers the manual reconcile path for a project with no journal
    # (hand-edited / journal lost), so drop it to emulate that.
    intent = paths["work"] / "activation_intent.json"
    assert intent.is_file()
    intent.unlink()

    result = reconcile_revision_state("rev_0002", paths)

    assert result["reconciled"] is True
    backup = paths["root"] / result["recovery_backup"] / "work" / "scenario_normalized.json"
    backed_up = __import__("json").loads(backup.read_text(encoding="utf-8"))
    assert backed_up["scenario_revision_id"] == "rev_0002"

    normalized_after = __import__("json").loads(paths["normalized"].read_text(encoding="utf-8"))
    active_after = __import__("json").loads(
        (paths["work"] / "active_revision.json").read_text(encoding="utf-8")
    )
    assert normalized_after["scenario_revision_id"] == "rev_0002"
    assert active_after["revision_id"] == "rev_0002"


def test_activation_interrupted_mid_asset_rename_is_detected(
    tmp_path: Path,
) -> None:
    """A crash between the two commits of restore_revision_assets and the
    per-alias rename loop must leave a detectable, blocking mismatch, even
    though active_revision.json and scenario_normalized.json still agree on
    the old revision.
    """
    paths = ensure_project("demo", tmp_path / "projects")
    source_v1 = paths["input"] / "v1.docx"
    source_v1.write_bytes(b"v1")
    atomic_write_json(
        paths["normalized"],
        {
            "source": str(source_v1),
            "source_hash": hashlib.sha256(b"v1").hexdigest(),
            "status": "confirmed",
            "blocks": [
                _block("one", "Первая", "voiceover"),
                _block("two", "Вторая", "voiceover"),
            ],
        },
    )
    _write_wav(paths["speaker"] / "zk1.wav", 1)
    _write_wav(paths["speaker"] / "zk2.wav", 2)
    ensure_baseline("demo", paths)

    source_v2 = paths["input"] / "v2.docx"
    source_v2.write_bytes(b"v2")

    def fake_generate(input_path, output_path, **_kwargs):
        value = {
            "source": str(input_path),
            "source_hash": hashlib.sha256(b"v2").hexdigest(),
            "status": "draft",
            "blocks": [
                _block("one", "Первая", "voiceover"),
                _block("two", "Вторая", "voiceover"),
            ],
        }
        atomic_write_json(output_path, value)
        return value

    import_revision(
        "demo",
        paths,
        generate_normalization=fake_generate,
        memory_path=tmp_path / "memory.sqlite3",
        input_path=source_v2,
    )

    import pytest
    from unittest.mock import patch

    speaker_dir = revision_speaker_alias_dir(paths, "rev_0002")
    real_replace = Path.replace
    rename_calls = {"count": 0}

    def flaky_replace(self, target):
        target_path = Path(target)
        # Only interrupt the commit loop that renames staged files onto the
        # live speaker/zkN.wav aliases; ignore staging copies and unrelated
        # snapshot copies so this test stays robust to unrelated Path.replace
        # calls elsewhere in the activation path.
        if target_path.parent == speaker_dir and re.fullmatch(r"zk\d+\.wav", target_path.name):
            rename_calls["count"] += 1
            if rename_calls["count"] == 2:
                raise RuntimeError("simulated crash mid asset rename")
        return real_replace(self, target)

    with patch.object(Path, "replace", flaky_replace):
        with pytest.raises(RuntimeError, match="simulated crash mid asset rename"):
            activate_revision("rev_0002", paths)

    assert rename_calls["count"] == 2  # the loop was genuinely interrupted mid-way

    active_after_crash = __import__("json").loads(
        (paths["work"] / "active_revision.json").read_text(encoding="utf-8")
    )
    normalized_after_crash = __import__("json").loads(
        paths["normalized"].read_text(encoding="utf-8")
    )
    prepared_after_crash = __import__("json").loads(paths["prepared"].read_text(encoding="utf-8"))
    manifest_after_crash = __import__("json").loads(
        paths["active_voiceover_manifest"].read_text(encoding="utf-8")
    )

    # The active pointer and the working normalization still agree on the
    # old revision...
    assert active_after_crash["revision_id"] == "rev_0001"
    assert normalized_after_crash.get("scenario_revision_id") == "rev_0001"
    # ...yet prepared.json and the voiceover manifest were already committed
    # to the new revision before the rename loop was interrupted, and one of
    # the two aliases never got renamed into place.
    assert prepared_after_crash["scenario_revision_id"] == "rev_0002"
    assert manifest_after_crash["scenario_revision_id"] == "rev_0002"
    assert (speaker_dir / ".zk2.revision.tmp").is_file()

    # Fix the fact of the interruption first, via the pure detector that
    # never triggers recovery, before invoking any self-healing behaviour.
    result = _detect_revision_state(paths)
    assert result["status"] == "active_assets_mismatch"
    assert result["blocking"] is True
    assert "prepared" in result["mismatched_assets"]
    assert "voiceover_manifest" in result["mismatched_assets"]

    # inspect_revision_state's write-ahead recovery pass finds the intent
    # journal restore_revision_assets left behind, resumes the interrupted
    # activation to completion, and reports the project as consistent again.
    recovered = inspect_revision_state(paths)
    assert recovered["status"] == "ok"
    assert recovered["blocking"] is False
    assert recovered["active_revision_id"] == "rev_0002"

    active_after_recovery = __import__("json").loads(
        (paths["work"] / "active_revision.json").read_text(encoding="utf-8")
    )
    normalized_after_recovery = __import__("json").loads(
        paths["normalized"].read_text(encoding="utf-8")
    )
    prepared_after_recovery = __import__("json").loads(
        paths["prepared"].read_text(encoding="utf-8")
    )
    manifest_after_recovery = __import__("json").loads(
        paths["active_voiceover_manifest"].read_text(encoding="utf-8")
    )
    assert active_after_recovery["revision_id"] == "rev_0002"
    assert normalized_after_recovery["scenario_revision_id"] == "rev_0002"
    assert prepared_after_recovery["scenario_revision_id"] == "rev_0002"
    assert manifest_after_recovery["scenario_revision_id"] == "rev_0002"
    assert not (speaker_dir / ".zk2.revision.tmp").is_file()
    assert (speaker_dir / "zk2.wav").is_file()
    assert not (paths["work"] / "activation_intent.json").is_file()


def test_activation_recovery_reports_when_it_cannot_finish(tmp_path: Path) -> None:
    """If the underlying voiceover asset an interrupted activation staged
    from is gone by the time recovery runs, resuming is impossible. This
    must be reported clearly, not silently ignored or left to crash an
    unrelated caller of inspect_revision_state.
    """
    paths = ensure_project("demo", tmp_path / "projects")
    source_v1 = paths["input"] / "v1.docx"
    source_v1.write_bytes(b"v1")
    atomic_write_json(
        paths["normalized"],
        {
            "source": str(source_v1),
            "source_hash": hashlib.sha256(b"v1").hexdigest(),
            "status": "confirmed",
            "blocks": [
                _block("one", "Первая", "voiceover"),
                _block("two", "Вторая", "voiceover"),
            ],
        },
    )
    _write_wav(paths["speaker"] / "zk1.wav", 1)
    _write_wav(paths["speaker"] / "zk2.wav", 2)
    ensure_baseline("demo", paths)

    source_v2 = paths["input"] / "v2.docx"
    source_v2.write_bytes(b"v2")

    def fake_generate(input_path, output_path, **_kwargs):
        value = {
            "source": str(input_path),
            "source_hash": hashlib.sha256(b"v2").hexdigest(),
            "status": "draft",
            "blocks": [
                _block("one", "Первая", "voiceover"),
                _block("two", "Вторая", "voiceover"),
            ],
        }
        atomic_write_json(output_path, value)
        return value

    import_revision(
        "demo",
        paths,
        generate_normalization=fake_generate,
        memory_path=tmp_path / "memory.sqlite3",
        input_path=source_v2,
    )

    import pytest
    from unittest.mock import patch

    speaker_dir = revision_speaker_alias_dir(paths, "rev_0002")
    real_replace = Path.replace
    rename_calls = {"count": 0}

    def flaky_replace(self, target):
        target_path = Path(target)
        if target_path.parent == speaker_dir and re.fullmatch(r"zk\d+\.wav", target_path.name):
            rename_calls["count"] += 1
            if rename_calls["count"] == 2:
                raise RuntimeError("simulated crash mid asset rename")
        return real_replace(self, target)

    with patch.object(Path, "replace", flaky_replace):
        with pytest.raises(RuntimeError, match="simulated crash mid asset rename"):
            activate_revision("rev_0002", paths)

    # Something outside this crash (a hand edit, a cleanup script, disk
    # corruption...) removed the underlying content-addressed asset the
    # interrupted activation staged its still-pending rename from.
    for asset in paths["voiceover_assets"].rglob("*.wav"):
        asset.unlink()

    result = inspect_revision_state(paths)

    assert result["status"] == "activation_recovery_failed"
    assert result["blocking"] is True
    assert result["active_revision_id"] == "rev_0002"
    assert "rev_0002" in result["message"]
    # The failed recovery attempt must not have thrown away the journal: a
    # later retry (once the missing asset is restored) still needs it.
    assert (paths["work"] / "activation_intent.json").is_file()


def test_recover_interrupted_activation_runs_activation_exactly_once(
    tmp_path: Path, monkeypatch
) -> None:
    """Two concurrent status checks (as ordinary GET routes issue) must not
    both decide to resume the same interrupted activation: the journal
    presence check and the resume must be atomic with respect to each other,
    not just internally locked inside ``activate_revision`` itself.
    """
    import threading
    import time

    from stage4 import versioning

    paths = ensure_project("demo", tmp_path / "projects")
    journal_path = paths["work"] / "activation_intent.json"
    atomic_write_json(journal_path, {"revision_id": "rev_0001"})

    calls: list[str] = []
    calls_lock = threading.Lock()
    start_barrier = threading.Barrier(2)

    def fake_activate(revision_id, project_paths, *, _resume=False):
        with calls_lock:
            calls.append(revision_id)
        # Widen the window during which a second, concurrent caller could
        # wrongly decide to resume too, mirroring how long the real
        # ``activate_revision`` keeps the journal around while it commits.
        time.sleep(0.05)
        journal_path.unlink(missing_ok=True)
        return {"revision": {}, "reused_sync_matches": 0}

    monkeypatch.setattr(versioning, "activate_revision", fake_activate)

    results: list[dict | None] = []
    results_lock = threading.Lock()

    def worker() -> None:
        start_barrier.wait()
        outcome = versioning.recover_interrupted_activation(paths)
        with results_lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(threads) == 2 and all(not thread.is_alive() for thread in threads)
    assert len(calls) == 1, f"activate_revision ran {len(calls)} times, expected exactly once"
    assert len(results) == 2
    assert sum(1 for result in results if result is not None and result["recovered"]) == 1
    assert not journal_path.is_file()


_ACTIVATION_CRASH_POINTS = [
    "after_restore_assets",
    "before_normalized",
    "after_normalized",
    "before_sync_matches",
    "after_sync_matches",
    "before_timeline",
    "after_timeline",
    "before_mapping",
    "after_mapping",
    "before_active_revision",
    "after_active_revision",
]


def _prepare_two_revisions_with_projections(tmp_path: Path) -> dict[str, Path]:
    """rev_0001 active; rev_0002 imported and given timeline/mapping snapshots."""
    import json

    paths = ensure_project("demo", tmp_path / "projects")
    source_v1 = paths["input"] / "v1.docx"
    source_v1.write_bytes(b"v1")
    blocks = [_block("one", "Первая", "voiceover"), _block("two", "Вторая", "voiceover")]
    atomic_write_json(
        paths["normalized"],
        {
            "source": str(source_v1),
            "source_hash": hashlib.sha256(b"v1").hexdigest(),
            "status": "confirmed",
            "blocks": blocks,
        },
    )
    _write_wav(paths["speaker"] / "zk1.wav", 1)
    _write_wav(paths["speaker"] / "zk2.wav", 2)
    atomic_write_json(paths["timeline"], {"marker": "timeline_v1"})
    atomic_write_json(paths["work"] / "stage3_source_mapping.json", {"marker": "mapping_v1"})
    ensure_baseline("demo", paths)

    source_v2 = paths["input"] / "v2.docx"
    source_v2.write_bytes(b"v2")

    def fake_generate(input_path, output_path, **_kwargs):
        value = {
            "source": str(input_path),
            "source_hash": hashlib.sha256(b"v2").hexdigest(),
            "status": "draft",
            "blocks": blocks,
        }
        atomic_write_json(output_path, value)
        return value

    import_revision(
        "demo",
        paths,
        generate_normalization=fake_generate,
        memory_path=tmp_path / "memory.sqlite3",
        input_path=source_v2,
    )
    timelines = paths["scenario_versions"].parent / "timelines"
    atomic_write_json(timelines / "rev_0002.full_timeline.json", {"marker": "timeline_v2"})
    atomic_write_json(timelines / "rev_0002.stage3_mapping.json", {"marker": "mapping_v2"})
    manifest_path = paths["scenario_versions"] / "rev_0002.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["timeline_snapshot"] = "rev_0002.full_timeline.json"
    manifest["stage3_mapping_snapshot"] = "rev_0002.stage3_mapping.json"
    atomic_write_json(manifest_path, manifest)
    return paths


def _inject_activation_crash(monkeypatch, paths: dict[str, Path], point: str) -> dict[str, bool]:
    from stage4 import versioning

    when, _, step = point.partition("_")
    targets = {
        "normalized": paths["normalized"],
        "sync_matches": paths["sync_matches"],
        "timeline": paths["timeline"],
        "mapping": paths["work"] / "stage3_source_mapping.json",
        "active_revision": paths["work"] / "active_revision.json",
    }
    state = {"fired": False}

    def maybe_crash(label: str, phase: str) -> None:
        if not state["fired"] and label == step and phase == when:
            state["fired"] = True
            raise RuntimeError(f"simulated crash {point}")

    real_restore = versioning.restore_revision_assets
    real_write = versioning.atomic_write_json
    real_snapshot = versioning._snapshot_file

    def restore(*args, **kwargs):
        real_restore(*args, **kwargs)
        maybe_crash("restore_assets", "after")

    def label_for(path: Path) -> str | None:
        for label, target in targets.items():
            if Path(path) == target:
                return label
        return None

    def write(path, payload, *args, **kwargs):
        label = label_for(path)
        if label:
            maybe_crash(label, "before")
        real_write(path, payload, *args, **kwargs)
        if label:
            maybe_crash(label, "after")

    def snapshot(source, destination):
        label = label_for(destination)
        if label:
            maybe_crash(label, "before")
        real_snapshot(source, destination)
        if label:
            maybe_crash(label, "after")

    monkeypatch.setattr(versioning, "restore_revision_assets", restore)
    monkeypatch.setattr(versioning, "atomic_write_json", write)
    monkeypatch.setattr(versioning, "_snapshot_file", snapshot)
    return state


def test_activation_crash_after_restore_assets_keeps_intent_journal(
    tmp_path: Path, monkeypatch
) -> None:
    """Audit item 5: the journal must outlive restore_revision_assets."""
    import pytest

    paths = _prepare_two_revisions_with_projections(tmp_path)
    _inject_activation_crash(monkeypatch, paths, "after_restore_assets")
    with pytest.raises(RuntimeError, match="simulated crash"):
        activate_revision("rev_0002", paths)
    monkeypatch.undo()

    assert (paths["work"] / "activation_intent.json").is_file()
    assert inspect_revision_state(paths)["status"] == "ok"
    assert not (paths["work"] / "activation_intent.json").is_file()


@pytest.mark.parametrize("point", _ACTIVATION_CRASH_POINTS)
def test_activation_crash_at_every_boundary_is_recoverable(
    tmp_path: Path, monkeypatch, point: str
) -> None:
    import json

    paths = _prepare_two_revisions_with_projections(tmp_path)
    intent = paths["work"] / "activation_intent.json"
    state = _inject_activation_crash(monkeypatch, paths, point)
    with pytest.raises(RuntimeError, match="simulated crash"):
        activate_revision("rev_0002", paths)
    monkeypatch.undo()
    assert state["fired"]

    # Never "journal absent + inconsistent projections".
    if not intent.is_file():
        assert _detect_revision_state(paths)["blocking"] is False

    recovered = inspect_revision_state(paths)
    assert recovered["status"] == "ok"
    assert recovered["blocking"] is False
    assert recovered["active_revision_id"] == "rev_0002"
    assert not intent.is_file()

    def read(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    assert read(paths["work"] / "active_revision.json")["revision_id"] == "rev_0002"
    assert read(paths["normalized"])["scenario_revision_id"] == "rev_0002"
    assert read(paths["prepared"])["scenario_revision_id"] == "rev_0002"
    assert read(paths["timeline"])["marker"] == "timeline_v2"
    assert read(paths["work"] / "stage3_source_mapping.json")["marker"] == "mapping_v2"


def test_successful_activation_closes_intent_journal(tmp_path: Path) -> None:
    paths = _prepare_two_revisions_with_projections(tmp_path)
    activate_revision("rev_0002", paths)
    assert not (paths["work"] / "activation_intent.json").is_file()
    assert inspect_revision_state(paths)["status"] == "ok"


def test_resumed_activation_rejects_working_projection_at_a_third_revision(
    tmp_path: Path,
) -> None:
    paths = ensure_project("demo", tmp_path / "projects")
    source_v1 = paths["input"] / "v1.docx"
    source_v1.write_bytes(b"v1")
    atomic_write_json(
        paths["normalized"],
        {
            "source": str(source_v1),
            "source_hash": hashlib.sha256(b"v1").hexdigest(),
            "status": "confirmed",
            "blocks": [_block("one", "Первая", "voiceover")],
        },
    )
    _write_wav(paths["speaker"] / "zk1.wav", 1)
    ensure_baseline("demo", paths)

    source_v2 = paths["input"] / "v2.docx"
    source_v2.write_bytes(b"v2")

    def fake_generate(input_path, output_path, **_kwargs):
        value = {
            "source": str(input_path),
            "source_hash": hashlib.sha256(b"v2").hexdigest(),
            "status": "draft",
            "blocks": [_block("one", "Первая", "voiceover")],
        }
        atomic_write_json(output_path, value)
        return value

    import_revision(
        "demo",
        paths,
        generate_normalization=fake_generate,
        memory_path=tmp_path / "memory.sqlite3",
        input_path=source_v2,
    )

    # The working projection belongs to an unrelated third revision.
    working = json.loads(paths["normalized"].read_text(encoding="utf-8"))
    working["scenario_revision_id"] = "rev_0099"
    working["blocks"] = [_block("x", "Чужая правка", "voiceover")]
    atomic_write_json(paths["normalized"], working)
    before = paths["normalized"].read_bytes()

    with pytest.raises(RevisionStateConflictError, match="does not match"):
        activate_revision("rev_0002", paths, _resume=True)

    assert paths["normalized"].read_bytes() == before


def _baseline_with_status(tmp_path: Path, blocks: list[dict]) -> dict:
    paths = ensure_project("demo", tmp_path / "projects")
    source = paths["input"] / "v1.docx"
    source.write_bytes(b"v1")
    atomic_write_json(
        paths["normalized"],
        {
            "source": str(source),
            "source_hash": hashlib.sha256(b"v1").hexdigest(),
            "status": "confirmed",
            "blocks": blocks,
        },
    )
    ensure_baseline("demo", paths)
    return paths


def test_restoring_confirmed_revision_failing_current_checks_warns_and_keeps_status(
    tmp_path: Path,
) -> None:
    bad = {**_block("one", "СНХ1 Первая", "voiceover", ""), "order": 1}
    bad["text_for_tts"] = "СНХ1 Первая"
    bad["source"] = "builtin_rule"
    paths = _baseline_with_status(tmp_path, [bad])

    result = activate_revision("rev_0001", paths)

    message = "restored confirmed revision fails current checks: service_label_in_tts"
    assert result["warnings"] == [message]
    restored = json.loads(paths["normalized"].read_text(encoding="utf-8"))
    assert restored["status"] == "confirmed"
    assert message in restored["warnings"]


def test_restoring_confirmed_revision_passing_checks_adds_no_warning(tmp_path: Path) -> None:
    good = {**_block("one", "Первая", "voiceover", ""), "order": 1, "source": "builtin_rule"}
    paths = _baseline_with_status(tmp_path, [good])

    result = activate_revision("rev_0001", paths)

    assert "warnings" not in result
    restored = json.loads(paths["normalized"].read_text(encoding="utf-8"))
    assert restored["status"] == "confirmed" and not restored.get("warnings")
