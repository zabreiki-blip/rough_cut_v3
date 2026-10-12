from __future__ import annotations

import hashlib
import json
from pathlib import Path
import wave

import pytest

from atomic_io import atomic_write_json
from project_service import ensure_project
from stage4.revision_assets import (
    CorruptStateError,
    _atomic_copy,
    capture_legacy_voiceover_manifest,
    merge_voiceover_manifest_item,
    revision_speaker_alias_dir,
    restore_revision_assets,
    save_tts_manifest,
    snapshot_revision_assets,
)
from stage4.versioning import (
    activate_revision,
    ensure_baseline,
    import_revision,
    inspect_revision_state,
)


def _block(uid: str, text: str) -> dict:
    return {
        "uid": uid,
        "normalized_text": text,
        "original_text": text,
        "type": "voiceover",
        "speaker": "Закадровый голос",
        "original_label": "ЗК",
        "text_for_tts": text,
    }


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_wav(path: Path, marker: int) -> bytes:
    """Write a valid, distinguishable WAV and return its immutable contents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(marker.to_bytes(2, "little", signed=True) * 80)
    return path.read_bytes()


def _manifest_entries(manifest: dict) -> list[dict]:
    """Keep assertions focused on the manifest's public mapping semantics."""
    for key in ("items", "voiceovers", "mappings"):
        value = manifest.get(key)
        if isinstance(value, list):
            return value
    raise AssertionError("voiceover manifest must contain an item mapping list")


def _entry_for_uid(manifest: dict, block_uid: str) -> dict:
    for entry in _manifest_entries(manifest):
        if entry.get("block_uid") == block_uid:
            return entry
    raise AssertionError(f"voiceover manifest has no entry for {block_uid}")


def _asset_path(entry: dict, project_root: Path) -> Path:
    raw = entry.get("asset") or entry.get("asset_path") or entry.get("path")
    assert raw, "voiceover mapping must reference its canonical asset"
    path = Path(str(raw))
    return path if path.is_absolute() else project_root / path


def _prepare_legacy_baseline(tmp_path: Path) -> tuple[dict[str, Path], Path, bytes]:
    paths = ensure_project("demo", tmp_path / "projects")
    source = paths["input"] / "scenario.docx"
    source.write_bytes(b"revision one")
    atomic_write_json(
        paths["normalized"],
        {
            "source": str(source),
            "source_hash": hashlib.sha256(b"revision one").hexdigest(),
            "status": "confirmed",
            "revision": 1,
            "blocks": [_block("voice-stable", "Старый закадровый текст")],
        },
    )
    # Deliberately legacy: old projections know only the positional zkN id.
    atomic_write_json(
        paths["prepared"],
        {
            "source": str(source),
            "normalization_revision": 1,
            "items": [
                {
                    "id": "zk1",
                    "text_original": "Старый закадровый текст",
                    "text_for_tts": "Старый закадровый текст",
                }
            ],
        },
    )
    old_audio = _write_wav(paths["speaker"] / "zk1.wav", 101)
    (paths["speaker"] / "zk1.wav.meta").write_text(
        json.dumps({"fingerprint": "legacy-fingerprint"}), encoding="utf-8"
    )
    return paths, source, old_audio


def _import_with_voiceover_insert(tmp_path: Path, paths: dict[str, Path]) -> dict:
    source_v2 = paths["input"] / "scenario_v2.docx"
    source_v2.write_bytes(b"revision two")

    def fake_generate(input_path: Path, output_path: Path, **_kwargs) -> dict:
        value = {
            "source": str(input_path),
            "source_hash": hashlib.sha256(b"revision two").hexdigest(),
            "status": "draft",
            "revision": 2,
            "blocks": [
                _block("new-import-uid", "Новый первый закадровый текст"),
                _block("old-import-uid", "Старый закадровый текст"),
            ],
        }
        atomic_write_json(output_path, value)
        return value

    return import_revision(
        "demo",
        paths,
        generate_normalization=fake_generate,
        memory_path=tmp_path / "memory.sqlite3",
        input_path=source_v2,
    )


def test_inserting_voiceover_before_existing_keeps_old_audio_by_block_uid(
    tmp_path: Path,
) -> None:
    paths, _source, old_audio = _prepare_legacy_baseline(tmp_path)

    imported = _import_with_voiceover_insert(tmp_path, paths)
    revision = imported["revision"]

    assert revision["prepared_snapshot"] == "rev_0002.prepared.json"
    assert revision["voiceover_manifest_snapshot"] == "rev_0002.voiceover.json"
    prepared = _read_json(paths["scenario_versions"] / revision["prepared_snapshot"])
    assert [(item["id"], item["block_uid"]) for item in prepared["items"]] == [
        ("zk1", "new-import-uid"),
        ("zk2", "voice-stable"),
    ]

    activate_revision("rev_0002", paths)

    active_manifest = _read_json(paths["active_voiceover_manifest"])
    old_mapping = _entry_for_uid(active_manifest, "voice-stable")
    assert old_mapping["id"] == "zk2"
    assert _asset_path(old_mapping, paths["root"]).read_bytes() == old_audio
    assert (revision_speaker_alias_dir(paths, "rev_0002") / "zk2.wav").read_bytes() == old_audio


def test_activating_old_revision_restores_prepared_and_audio_mapping(
    tmp_path: Path,
) -> None:
    paths, _source, old_audio = _prepare_legacy_baseline(tmp_path)
    _import_with_voiceover_insert(tmp_path, paths)
    activate_revision("rev_0002", paths)

    # Simulate TTS completion for the newly inserted block while rev_0002 is
    # active. Switching away must first snapshot this outgoing active state.
    new_asset = paths["voiceover_assets"] / "new-import-uid" / "new-fingerprint.wav"
    new_audio = _write_wav(new_asset, 202)
    active_manifest = _read_json(paths["active_voiceover_manifest"])
    _manifest_entries(active_manifest).append(
        {
            "id": "zk1",
            "block_uid": "new-import-uid",
            "fingerprint": "new-fingerprint",
            "asset": str(new_asset.relative_to(paths["root"])),
        }
    )
    atomic_write_json(paths["active_voiceover_manifest"], active_manifest)
    (revision_speaker_alias_dir(paths, "rev_0002") / "zk1.wav").write_bytes(new_audio)
    atomic_write_json(paths["timeline"], {"revision": "rev_0002", "segments": []})
    atomic_write_json(
        paths["work"] / "stage3_source_mapping.json",
        {"scenario_revision_id": "rev_0002", "nested_timeline_filename": "demo_V2.xml"},
    )
    atomic_write_json(
        paths["sync_matches"],
        {
            "schema_version": 1,
            "matches": [{"segment_id": "new-import-uid", "status": "confirmed"}],
        },
    )

    activate_revision("rev_0001", paths)

    restored = _read_json(paths["prepared"])
    assert [(item["id"], item["block_uid"]) for item in restored["items"]] == [
        ("zk1", "voice-stable")
    ]
    restored_manifest = _read_json(paths["active_voiceover_manifest"])
    assert [entry["block_uid"] for entry in _manifest_entries(restored_manifest)] == [
        "voice-stable"
    ]
    assert (revision_speaker_alias_dir(paths, "rev_0001") / "zk1.wav").read_bytes() == old_audio

    rev2 = _read_json(paths["scenario_versions"] / "rev_0002.json")
    saved_rev2_manifest = _read_json(
        paths["scenario_versions"] / rev2["voiceover_manifest_snapshot"]
    )
    inserted = _entry_for_uid(saved_rev2_manifest, "new-import-uid")
    assert _asset_path(inserted, paths["root"]).read_bytes() == new_audio

    activate_revision("rev_0002", paths)
    assert _read_json(paths["timeline"])["revision"] == "rev_0002"
    assert _read_json(paths["sync_matches"])["matches"][0]["segment_id"] == "new-import-uid"
    assert (
        _read_json(paths["work"] / "stage3_source_mapping.json")["scenario_revision_id"]
        == "rev_0002"
    )


def test_legacy_zk_audio_is_migrated_once_and_reused_as_immutable_asset(
    tmp_path: Path,
) -> None:
    paths, _source, old_audio = _prepare_legacy_baseline(tmp_path)

    baseline = ensure_baseline("demo", paths)

    assert baseline["prepared_snapshot"] == "rev_0001.prepared.json"
    assert baseline["voiceover_manifest_snapshot"] == "rev_0001.voiceover.json"
    prepared = _read_json(paths["scenario_versions"] / baseline["prepared_snapshot"])
    assert prepared["items"][0]["block_uid"] == "voice-stable"
    manifest = _read_json(paths["active_voiceover_manifest"])
    mapping = _entry_for_uid(manifest, "voice-stable")
    canonical = _asset_path(mapping, paths["root"])
    assert canonical.parent == paths["voiceover_assets"] / "voice-stable"
    assert canonical.name == f"{mapping['fingerprint']}.wav"
    assert canonical.read_bytes() == old_audio

    # A later positional alias may be replaced, but the migrated content-addressed
    # asset belonging to the old revision must not be overwritten.
    _write_wav(paths["speaker"] / "zk1.wav", 303)
    same_baseline = ensure_baseline("demo", paths)
    assert same_baseline["revision_id"] == "rev_0001"
    assert canonical.read_bytes() == old_audio


def test_baseline_accepts_scenario_from_friendly_inbox(tmp_path: Path) -> None:
    paths = ensure_project("demo", tmp_path / "projects")
    source = paths["inbox_scenarios"] / "Первая редакция.docx"
    source.write_bytes(b"friendly scenario")
    atomic_write_json(
        paths["normalized"],
        {
            "source": str(source),
            "source_hash": hashlib.sha256(b"friendly scenario").hexdigest(),
            "status": "confirmed",
            "revision": 1,
            "blocks": [_block("voice-friendly", "Текст")],
        },
    )

    baseline = ensure_baseline("demo", paths)

    assert baseline["source_file"] == "Первая редакция.docx"
    assert (paths["scenario_versions"] / baseline["source_snapshot"]).is_file()


def test_snapshot_syncs_manifest_metadata_and_uses_canonical_source(
    tmp_path: Path,
) -> None:
    paths, _source, _audio = _prepare_legacy_baseline(tmp_path)
    baseline = ensure_baseline("demo", paths)
    normalized = _read_json(paths["normalized"])
    normalized["status"] = "draft"
    normalized["blocks"].append(_block("voice-two", "Вторая реплика"))
    normalized["source"] = "/temporary/import/location.docx"
    atomic_write_json(paths["normalized"], normalized)

    updated = snapshot_revision_assets("rev_0001", paths)

    canonical = str((paths["scenario_versions"] / baseline["source_snapshot"]).resolve())
    assert updated["status"] == "draft"
    assert updated["block_count"] == 2
    assert _read_json(paths["normalized"])["source"] == canonical
    assert (
        _read_json(paths["scenario_versions"] / updated["normalized_snapshot"])["source"]
        == canonical
    )
    assert (
        _read_json(paths["scenario_versions"] / updated["prepared_snapshot"])["source"] == canonical
    )


def test_edited_voiceover_does_not_inherit_old_audio_before_resynthesis(
    tmp_path: Path,
) -> None:
    paths, _source, _old_audio = _prepare_legacy_baseline(tmp_path)
    source_v2 = paths["input"] / "edited.docx"
    source_v2.write_bytes(b"edited revision")

    def fake_generate(input_path: Path, output_path: Path, **_kwargs) -> dict:
        value = {
            "source": str(input_path),
            "source_hash": hashlib.sha256(b"edited revision").hexdigest(),
            "status": "draft",
            "revision": 2,
            "blocks": [_block("imported", "Старый закадровый текст изменён")],
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

    manifest = _read_json(
        paths["scenario_versions"] / imported["revision"]["voiceover_manifest_snapshot"]
    )
    assert _manifest_entries(manifest) == []
    activate_revision("rev_0002", paths)
    assert not (revision_speaker_alias_dir(paths, "rev_0002") / "zk1.wav").exists()


def test_inspect_revision_state_detects_stale_voiceover_asset_fingerprint(
    tmp_path: Path,
) -> None:
    """A speaker/*.wav alias whose content drifted from the manifest's
    recorded fingerprint must be flagged even when prepared.json and the
    manifest itself still agree with the active revision.
    """
    paths, _source, _old_audio = _prepare_legacy_baseline(tmp_path)
    ensure_baseline("demo", paths)

    assert inspect_revision_state(paths)["status"] == "ok"

    manifest = _read_json(paths["active_voiceover_manifest"])
    fingerprint = _entry_for_uid(manifest, "voice-stable")["fingerprint"]
    assert fingerprint.startswith("legacy-")
    alias = revision_speaker_alias_dir(paths, "rev_0001") / "zk1.wav"
    recorded = json.loads(alias.with_suffix(".wav.meta").read_text(encoding="utf-8"))
    assert recorded["fingerprint"] == fingerprint
    alias.with_suffix(".wav.meta").write_text(
        json.dumps({"fingerprint": "legacy-tampered"}), encoding="utf-8"
    )

    result = inspect_revision_state(paths)
    assert result["status"] == "active_assets_mismatch"
    assert result["blocking"] is True
    assert result["mismatched_assets"] == ["voiceover_asset:zk1"]


def test_restore_revision_assets_records_sidecar_for_legacy_fingerprint(
    tmp_path: Path,
) -> None:
    """Legacy content-hash fingerprints must also get a wav.meta sidecar so
    inspect_revision_state can verify them without re-hashing audio files.
    """
    paths, _source, old_audio = _prepare_legacy_baseline(tmp_path)
    imported = _import_with_voiceover_insert(tmp_path, paths)
    manifest = _read_json(
        paths["scenario_versions"] / imported["revision"]["voiceover_manifest_snapshot"]
    )
    expected_fingerprint = _entry_for_uid(manifest, "voice-stable")["fingerprint"]
    assert expected_fingerprint.startswith("legacy-")

    activate_revision("rev_0002", paths)

    alias = revision_speaker_alias_dir(paths, "rev_0002") / "zk2.wav"
    meta = json.loads(alias.with_suffix(".wav.meta").read_text(encoding="utf-8"))
    assert meta["fingerprint"] == expected_fingerprint
    assert alias.read_bytes() == old_audio


def test_revision_alias_projections_coexist_and_restore_without_overwriting(tmp_path: Path) -> None:
    paths, _source, old_audio = _prepare_legacy_baseline(tmp_path)
    _import_with_voiceover_insert(tmp_path, paths)
    activate_revision("rev_0002", paths)

    v2_alias_dir = revision_speaker_alias_dir(paths, "rev_0002")
    new_asset = paths["voiceover_assets"] / "new-import-uid" / "new-fingerprint.wav"
    new_audio = _write_wav(new_asset, 202)
    active_manifest = _read_json(paths["active_voiceover_manifest"])
    _manifest_entries(active_manifest).append(
        {
            "id": "zk1",
            "block_uid": "new-import-uid",
            "fingerprint": "new-fingerprint",
            "asset": str(new_asset.relative_to(paths["root"])),
        }
    )
    atomic_write_json(paths["active_voiceover_manifest"], active_manifest)
    (v2_alias_dir / "zk1.wav").write_bytes(new_audio)

    activate_revision("rev_0001", paths)
    v1_alias_dir = revision_speaker_alias_dir(paths, "rev_0001")
    assert (v1_alias_dir / "zk1.wav").read_bytes() == old_audio
    assert (v2_alias_dir / "zk1.wav").read_bytes() == new_audio
    assert (v2_alias_dir / "zk2.wav").read_bytes() == old_audio

    activate_revision("rev_0002", paths)
    assert (v2_alias_dir / "zk1.wav").read_bytes() == new_audio
    assert (v1_alias_dir / "zk1.wav").read_bytes() == old_audio


def test_stale_tts_result_is_saved_to_its_revision_not_the_active_one(
    tmp_path: Path,
) -> None:
    paths = ensure_project("demo", tmp_path / "projects")
    asset = paths["voiceover_assets"] / "old" / "fp.wav"
    _write_wav(asset, 404)
    atomic_write_json(paths["normalized"], {"scenario_revision_id": "rev_0002"})
    atomic_write_json(
        paths["active_voiceover_manifest"],
        {
            "schema_version": 1,
            "scenario_revision_id": "rev_0002",
            "items": [{"id": "zk1", "block_uid": "current", "asset": "current.wav"}],
        },
    )
    atomic_write_json(
        paths["scenario_versions"] / "rev_0001.json",
        {
            "revision_id": "rev_0001",
        },
    )

    save_tts_manifest(
        paths,
        {
            "scenario_revision_id": "rev_0001",
            "voiceover_assets": [
                {
                    "id": "zk1",
                    "block_uid": "old",
                    "fingerprint": "fp",
                    "asset": str(asset),
                }
            ],
        },
    )

    assert _read_json(paths["active_voiceover_manifest"])["scenario_revision_id"] == "rev_0002"
    saved = _read_json(paths["scenario_versions"] / "rev_0001.voiceover.json")
    assert _entry_for_uid(saved, "old")["fingerprint"] == "fp"


def test_merge_voiceover_manifest_item_records_resynthesis_without_full_batch(
    tmp_path: Path,
) -> None:
    """A single block resynthesized through Stage 1 TTS must update the
    active manifest immediately, not only once an entire batch completes.
    Otherwise a legitimate, still-in-progress or partially cancelled
    resynthesis looks identical to the asset corruption
    inspect_revision_state is meant to catch (see
    test_activation_interrupted_mid_asset_rename_is_detected).
    """
    paths, _source, _old_audio = _prepare_legacy_baseline(tmp_path)
    ensure_baseline("demo", paths)
    assert inspect_revision_state(paths)["status"] == "ok"

    # Mimic what stage1.gemini_tts does when a block is resynthesized: the
    # content-addressed asset and the revision-local zk1.wav alias (with its
    # sidecar) are updated to a new fingerprint.
    speaker_dir = revision_speaker_alias_dir(paths, "rev_0001")
    new_asset = speaker_dir / "assets" / "voice-stable" / "new-fingerprint.wav"
    new_audio = _write_wav(new_asset, 909)
    (speaker_dir / "zk1.wav").write_bytes(new_audio)
    atomic_write_json(speaker_dir / "zk1.wav.meta", {"fingerprint": "new-fingerprint"})

    merge_voiceover_manifest_item(
        paths,
        revision_id="rev_0001",
        block_id="zk1",
        block_uid="voice-stable",
        fingerprint="new-fingerprint",
        speaker_dir=speaker_dir,
    )

    manifest = _read_json(paths["active_voiceover_manifest"])
    entry = _entry_for_uid(manifest, "voice-stable")
    assert entry["fingerprint"] == "new-fingerprint"
    assert _asset_path(entry, paths["root"]).read_bytes() == new_audio

    result = inspect_revision_state(paths)
    assert result["status"] == "ok"
    assert result["blocking"] is False


def test_merge_voiceover_manifest_item_is_safe_under_concurrent_updates(
    tmp_path: Path,
) -> None:
    """synthesize_tts calls merge_voiceover_manifest_item from several
    ThreadPoolExecutor worker threads as soon as each block finishes (see
    stage1.gemini_tts). Its read-modify-write of the manifest must be
    serialized per project so two blocks finishing at the same moment never
    make one write clobber the other and silently drop a block from the
    manifest.
    """
    from concurrent.futures import ThreadPoolExecutor

    paths, _source, _old_audio = _prepare_legacy_baseline(tmp_path)
    ensure_baseline("demo", paths)

    block_count = 20
    for index in range(2, block_count + 1):
        block_uid = f"voice-{index}"
        asset = paths["speaker"] / "assets" / block_uid / f"fp-{index}.wav"
        _write_wav(asset, index)

    def merge_one(index: int) -> None:
        merge_voiceover_manifest_item(
            paths,
            revision_id="rev_0001",
            block_id=f"zk{index}",
            block_uid=f"voice-{index}",
            fingerprint=f"fp-{index}",
            speaker_dir=paths["speaker"],
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(merge_one, range(2, block_count + 1)))

    manifest = _read_json(paths["active_voiceover_manifest"])
    recorded_uids = {entry.get("block_uid") for entry in _manifest_entries(manifest)}
    expected_uids = {f"voice-{index}" for index in range(2, block_count + 1)}
    assert expected_uids <= recorded_uids


def test_merge_voiceover_manifest_item_skips_manifest_for_other_revision(
    tmp_path: Path,
) -> None:
    """TTS running against a non-active revision's prepared.json must never
    touch the active projection; that revision gets its own snapshot once
    save_tts_manifest runs at the end of the batch."""
    paths, _source, _old_audio = _prepare_legacy_baseline(tmp_path)
    ensure_baseline("demo", paths)
    before = _read_json(paths["active_voiceover_manifest"])

    new_asset = paths["speaker"] / "assets" / "voice-stable" / "other-fingerprint.wav"
    _write_wav(new_asset, 111)

    merge_voiceover_manifest_item(
        paths,
        revision_id="rev_0002",
        block_id="zk1",
        block_uid="voice-stable",
        fingerprint="other-fingerprint",
        speaker_dir=paths["speaker"],
    )

    assert _read_json(paths["active_voiceover_manifest"]) == before


def test_merge_voiceover_manifest_item_ignores_missing_asset(tmp_path: Path) -> None:
    paths, _source, _old_audio = _prepare_legacy_baseline(tmp_path)
    ensure_baseline("demo", paths)
    before = _read_json(paths["active_voiceover_manifest"])

    merge_voiceover_manifest_item(
        paths,
        revision_id="rev_0001",
        block_id="zk1",
        block_uid="voice-stable",
        fingerprint="never-written",
        speaker_dir=paths["speaker"],
    )

    assert _read_json(paths["active_voiceover_manifest"]) == before


def test_snapshot_does_not_overwrite_history_when_revision_manifest_is_corrupt(
    tmp_path: Path,
) -> None:
    paths, _source, _audio = _prepare_legacy_baseline(tmp_path)
    ensure_baseline("demo", paths)
    manifest_path = paths["scenario_versions"] / "rev_0001.json"
    snapshot_path = paths["scenario_versions"] / "rev_0001.normalized.json"
    manifest_path.write_text("{broken", encoding="utf-8")
    snapshot_before = snapshot_path.read_bytes()

    with pytest.raises(ValueError):
        snapshot_revision_assets("rev_0001", paths)

    assert manifest_path.read_text(encoding="utf-8") == "{broken"
    assert snapshot_path.read_bytes() == snapshot_before


def test_snapshot_does_not_overwrite_history_when_work_projection_is_corrupt(
    tmp_path: Path,
) -> None:
    paths, _source, _audio = _prepare_legacy_baseline(tmp_path)
    ensure_baseline("demo", paths)
    prepared_snapshot = paths["scenario_versions"] / "rev_0001.prepared.json"
    voiceover_snapshot = paths["scenario_versions"] / "rev_0001.voiceover.json"
    before = (prepared_snapshot.read_bytes(), voiceover_snapshot.read_bytes())
    paths["prepared"].write_text("not json", encoding="utf-8")

    with pytest.raises(ValueError):
        snapshot_revision_assets("rev_0001", paths)

    assert (prepared_snapshot.read_bytes(), voiceover_snapshot.read_bytes()) == before
    assert paths["prepared"].read_text(encoding="utf-8") == "not json"


def test_snapshot_does_not_replace_corrupt_active_voiceover_manifest(tmp_path: Path) -> None:
    paths, _source, _audio = _prepare_legacy_baseline(tmp_path)
    ensure_baseline("demo", paths)
    voiceover_snapshot = paths["scenario_versions"] / "rev_0001.voiceover.json"
    before = voiceover_snapshot.read_bytes()
    paths["active_voiceover_manifest"].write_text("[1, 2]", encoding="utf-8")

    with pytest.raises(ValueError):
        snapshot_revision_assets("rev_0001", paths)

    assert voiceover_snapshot.read_bytes() == before
    assert paths["active_voiceover_manifest"].read_text(encoding="utf-8") == "[1, 2]"


def test_snapshot_tolerates_missing_files(tmp_path: Path) -> None:
    paths, _source, _audio = _prepare_legacy_baseline(tmp_path)
    ensure_baseline("demo", paths)
    paths["active_voiceover_manifest"].unlink()

    assert snapshot_revision_assets("rev_0001", paths)["revision_id"] == "rev_0001"
    assert snapshot_revision_assets("rev_9999", paths) == {}


def test_merge_voiceover_manifest_item_keeps_corrupt_manifest(tmp_path: Path) -> None:
    paths, _source, _audio = _prepare_legacy_baseline(tmp_path)
    ensure_baseline("demo", paths)
    speaker_dir = revision_speaker_alias_dir(paths, "rev_0001")
    _write_wav(speaker_dir / "assets" / "voice-stable" / "fp-new.wav", 77)
    paths["active_voiceover_manifest"].write_text("{broken", encoding="utf-8")

    merge_voiceover_manifest_item(
        paths,
        revision_id="rev_0001",
        block_id="zk1",
        block_uid="voice-stable",
        fingerprint="fp-new",
        speaker_dir=speaker_dir,
    )

    assert paths["active_voiceover_manifest"].read_text(encoding="utf-8") == "{broken"


def test_restore_revision_assets_rejects_corrupt_snapshot_without_touching_work(
    tmp_path: Path,
) -> None:
    paths, _source, _audio = _prepare_legacy_baseline(tmp_path)
    revision = ensure_baseline("demo", paths)
    normalized = _read_json(paths["normalized"])
    prepared_before = paths["prepared"].read_bytes()
    manifest_before = paths["active_voiceover_manifest"].read_bytes()
    (paths["scenario_versions"] / revision["voiceover_manifest_snapshot"]).write_text(
        "{broken", encoding="utf-8"
    )

    with pytest.raises(ValueError):
        restore_revision_assets("rev_0001", revision, normalized, paths)

    assert paths["prepared"].read_bytes() == prepared_before
    assert paths["active_voiceover_manifest"].read_bytes() == manifest_before


def test_atomic_copy_removes_temporary_file_on_failure(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"data")
    destination = tmp_path / "out" / "dest.bin"

    def failing_replace(self: Path, target: Path) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", failing_replace)

    with pytest.raises(OSError):
        _atomic_copy(source, destination)

    assert list(destination.parent.iterdir()) == []


def test_atomic_copy_uses_unique_temporary_names(tmp_path: Path, monkeypatch) -> None:
    import shutil

    source = tmp_path / "source.bin"
    source.write_bytes(b"data")
    destination = tmp_path / "out" / "dest.bin"
    temporaries: list[Path] = []
    real_copy2 = shutil.copy2

    def recording_copy2(src, dst, *args, **kwargs):
        temporaries.append(Path(dst))
        return real_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(shutil, "copy2", recording_copy2)

    _atomic_copy(source, destination)
    _atomic_copy(source, destination)

    assert len(temporaries) == 2
    assert temporaries[0] != temporaries[1]
    assert destination.read_bytes() == b"data"
    assert [item.name for item in destination.parent.iterdir()] == ["dest.bin"]


def test_capture_legacy_manifest_corrupt_input_leaves_work_files_untouched(
    tmp_path: Path,
) -> None:
    paths, _source, _audio = _prepare_legacy_baseline(tmp_path)
    paths["active_voiceover_manifest"].write_text("{broken", encoding="utf-8")
    prepared_before = paths["prepared"].read_bytes()
    normalized_before = paths["normalized"].read_bytes()

    with pytest.raises(ValueError):
        capture_legacy_voiceover_manifest(paths)

    assert paths["prepared"].read_bytes() == prepared_before
    assert paths["normalized"].read_bytes() == normalized_before
    assert paths["active_voiceover_manifest"].read_text(encoding="utf-8") == "{broken"


def test_snapshot_revision_assets_corrupt_input_leaves_work_files_untouched(
    tmp_path: Path,
) -> None:
    paths, _source, _audio = _prepare_legacy_baseline(tmp_path)
    ensure_baseline("demo", paths)
    # Different path string so normalized["source"] would be rewritten.
    normalized = _read_json(paths["normalized"])
    normalized["source"] = "somewhere/else.docx"
    atomic_write_json(paths["normalized"], normalized)
    paths["prepared"].write_text("{broken", encoding="utf-8")
    normalized_before = paths["normalized"].read_bytes()
    manifest_before = paths["active_voiceover_manifest"].read_bytes()

    with pytest.raises(ValueError):
        snapshot_revision_assets("rev_0001", paths)

    assert paths["normalized"].read_bytes() == normalized_before
    assert paths["prepared"].read_text(encoding="utf-8") == "{broken"
    assert paths["active_voiceover_manifest"].read_bytes() == manifest_before


def test_save_tts_manifest_corrupt_normalized_raises_corrupt_state_error(
    tmp_path: Path,
) -> None:
    paths, _source, _audio = _prepare_legacy_baseline(tmp_path)
    ensure_baseline("demo", paths)
    paths["normalized"].write_text("{broken", encoding="utf-8")

    with pytest.raises(CorruptStateError) as excinfo:
        save_tts_manifest(paths, {"voiceover_assets": []})

    assert isinstance(excinfo.value, ValueError)
    assert "повреждено" in str(excinfo.value)
