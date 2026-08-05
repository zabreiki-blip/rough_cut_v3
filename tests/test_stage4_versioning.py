from __future__ import annotations

import hashlib
from pathlib import Path

from atomic_io import atomic_write_json
from project_service import ensure_project
from stage4.versioning import activate_revision, compare_blocks, import_revision


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
    assert normalized["source"] == str(source_v2)

    activated = activate_revision("rev_0002", paths)
    assert activated["reused_sync_matches"] == 1
    assert (paths["scenario_versions"] / "rev_0001.source.docx").read_bytes() == b"v1"

    paths["sync_matches"].write_text('{"schema_version": 1, "matches": []}\n', encoding="utf-8")
    rolled_back = activate_revision("rev_0001", paths)
    assert rolled_back["reused_sync_matches"] == 1
