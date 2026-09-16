from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import threading
from typing import Any

from atomic_io import atomic_write_json


_VOICEOVER_ALIAS_RE = re.compile(r"^zk\d+\.wav$")
_ACTIVATION_INTENT_FILENAME = "activation_intent.json"

# merge_voiceover_manifest_item() does a non-atomic read-modify-write of the
# active voiceover manifest. Stage 1 TTS calls it once per finished block
# from several ThreadPoolExecutor worker threads (see synthesize_tts), so
# two blocks finishing at the same moment can both read the same manifest
# version and one write can clobber the other, silently dropping a block.
# Serialize updates with one lock per manifest path (not a single
# process-wide lock) so concurrent projects never block each other. Mirrors
# the ``_path_lock`` cache-by-resolved-path pattern in stage1/normalizer.py.
_MANIFEST_LOCKS: dict[str, threading.Lock] = {}
_MANIFEST_LOCKS_GUARD = threading.Lock()


def _manifest_lock(path: Path) -> threading.Lock:
    key = str(Path(path).resolve())
    with _MANIFEST_LOCKS_GUARD:
        return _MANIFEST_LOCKS.setdefault(key, threading.Lock())


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    shutil.copy2(source, temporary)
    temporary.replace(destination)


def _safe_uid(value: str) -> str:
    raw = str(value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", raw):
        return raw
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _project_asset_path(project_paths: dict[str, Path], value: str) -> Path | None:
    raw = Path(str(value or ""))
    if not value or raw.is_absolute():
        return None
    root = project_paths["root"].resolve()
    candidate = (root / raw).resolve()
    return candidate if candidate.is_relative_to(root) else None


def activation_intent_path(project_paths: dict[str, Path]) -> Path:
    """Where restore_revision_assets records its write-ahead activation intent.

    Kept directly under ``work/`` alongside the rest of the mutable
    projection state (``project_paths["work"]`` in project_service.py) so a
    crash recovery pass (see stage4.versioning.inspect_revision_state) only
    has to look in one place every project already keeps its live state.
    """
    return project_paths["work"] / _ACTIVATION_INTENT_FILENAME


def revision_speaker_alias_dir(project_paths: dict[str, Path], revision_id: str) -> Path:
    """Return the mutable speaker alias projection for one revision.

    Canonical assets remain under ``speaker/assets``.  Positional ``zkN``
    names are deliberately revision-local so activating or synthesizing V2
    cannot overwrite the aliases kept for V1 (or any later revision).
    ``capture_legacy_voiceover_manifest`` continues to read the historical
    top-level ``speaker/zkN.wav`` aliases while migrating pre-versioning
    projects.
    """
    if not re.fullmatch(r"rev_\d+", revision_id):
        raise ValueError("invalid revision id")
    return project_paths["speaker"] / "revisions" / revision_id


def prepared_from_normalized(normalized: dict[str, Any]) -> dict[str, Any]:
    blocks = [
        block
        for block in normalized.get("blocks", [])
        if isinstance(block, dict) and block.get("type") == "voiceover"
    ]
    return {
        "source": normalized.get("source", ""),
        "used_llm": any(block.get("source") == "llm" for block in blocks),
        "manual_markers_used": any(block.get("source") == "manual" for block in blocks),
        "extraction_method": "normalization",
        "normalization_revision": normalized.get("revision", 1),
        "scenario_revision_id": normalized.get("scenario_revision_id", ""),
        "parent_revision_id": normalized.get("parent_revision_id", ""),
        "items": [
            {
                "id": f"zk{index}",
                "block_uid": str(block.get("uid") or ""),
                "text_original": block.get("normalized_text") or block.get("original_text") or "",
                "text_for_tts": block.get("text_for_tts") or block.get("normalized_text") or "",
                "pause_ms": 300,
                "notes": block.get("notes") or "",
            }
            for index, block in enumerate(blocks, start=1)
        ],
    }


def _with_block_uids(prepared: dict[str, Any], normalized: dict[str, Any]) -> dict[str, Any]:
    result = dict(prepared)
    result["source"] = normalized.get("source", result.get("source", ""))
    result["normalization_revision"] = normalized.get(
        "revision", result.get("normalization_revision", 1)
    )
    result["scenario_revision_id"] = (
        normalized.get("scenario_revision_id") or result.get("scenario_revision_id") or ""
    )
    result["parent_revision_id"] = normalized.get(
        "parent_revision_id", result.get("parent_revision_id", "")
    )
    voiceovers = [
        block
        for block in normalized.get("blocks", [])
        if isinstance(block, dict) and block.get("type") == "voiceover"
    ]
    items = []
    for index, original in enumerate(prepared.get("items", [])):
        if not isinstance(original, dict):
            continue
        item = dict(original)
        if not item.get("block_uid") and index < len(voiceovers):
            item["block_uid"] = str(voiceovers[index].get("uid") or "")
        items.append(item)
    result["items"] = items
    return result


def capture_legacy_voiceover_manifest(project_paths: dict[str, Path]) -> dict[str, Any]:
    prepared = _read_json(project_paths["prepared"])
    normalized = _read_json(project_paths["normalized"])
    if not prepared:
        prepared = prepared_from_normalized(normalized)
    prepared = _with_block_uids(prepared, normalized)
    atomic_write_json(project_paths["prepared"], prepared)

    current = _read_json(project_paths["active_voiceover_manifest"])
    by_id = {
        str(item.get("id") or ""): item
        for item in current.get("items", [])
        if isinstance(item, dict)
    }
    manifest_items: list[dict[str, Any]] = []
    for item in prepared.get("items", []):
        block_id = str(item.get("id") or "").strip()
        block_uid = str(item.get("block_uid") or block_id).strip()
        existing = by_id.get(block_id, {})
        if existing and str(existing.get("block_uid") or "") != block_uid:
            # A positional alias may belong to another revision. Never bind it
            # to a different stable block identity.
            continue
        asset = str(existing.get("asset") or "")
        asset_path = _project_asset_path(project_paths, asset)
        if asset_path is not None and asset_path.is_file():
            manifest_items.append(dict(existing))
            continue
        alias = project_paths["speaker"] / f"{block_id}.wav"
        if not alias.is_file():
            continue
        audio_hash = hashlib.sha256(alias.read_bytes()).hexdigest()
        fingerprint = f"legacy-{audio_hash}"
        destination = (
            project_paths["voiceover_assets"] / _safe_uid(block_uid) / f"legacy-{audio_hash}.wav"
        )
        if not destination.is_file():
            _atomic_copy(alias, destination)
        # Stamp the sidecar with the fingerprint just computed from this
        # exact alias content, so later consistency checks (see
        # stage4.versioning.inspect_revision_state) have an accurate record
        # to compare against instead of a stale or absent one.
        atomic_write_json(alias.with_suffix(".wav.meta"), {"fingerprint": fingerprint})
        manifest_items.append(
            {
                "id": block_id,
                "block_uid": block_uid,
                "fingerprint": fingerprint,
                "asset": destination.relative_to(project_paths["root"]).as_posix(),
            }
        )
    manifest = {
        "schema_version": 1,
        "scenario_revision_id": prepared.get("scenario_revision_id") or "",
        "items": manifest_items,
    }
    atomic_write_json(project_paths["active_voiceover_manifest"], manifest)
    return manifest


def snapshot_revision_assets(revision_id: str, project_paths: dict[str, Path]) -> dict[str, Any]:
    manifest_path = project_paths["scenario_versions"] / f"{revision_id}.json"
    revision = _read_json(manifest_path)
    if not revision:
        return {}
    normalized = _read_json(project_paths["normalized"])
    if str(normalized.get("scenario_revision_id") or "") != revision_id:
        # The work projection may already belong to a newly normalized or
        # otherwise different scenario.  Never overwrite the active revision's
        # historical snapshot with that unrelated payload.
        return {}
    source_snapshot_name = str(revision.get("source_snapshot") or "")
    expected_source_snapshot = (
        f"{revision_id}.source{Path(str(revision.get('source_file') or '')).suffix.lower()}"
    )
    source_snapshot = project_paths["scenario_versions"] / source_snapshot_name
    if source_snapshot_name == expected_source_snapshot and source_snapshot.is_file():
        normalized["source"] = str(source_snapshot.resolve())
        atomic_write_json(project_paths["normalized"], normalized)
    voiceover_manifest = capture_legacy_voiceover_manifest(project_paths)
    normalized_snapshot = project_paths["scenario_versions"] / f"{revision_id}.normalized.json"
    prepared_snapshot = project_paths["scenario_versions"] / f"{revision_id}.prepared.json"
    voiceover_snapshot = project_paths["scenario_versions"] / f"{revision_id}.voiceover.json"
    if project_paths["normalized"].is_file():
        _atomic_copy(project_paths["normalized"], normalized_snapshot)
        revision["normalized_snapshot"] = normalized_snapshot.name
        revision["status"] = "confirmed" if normalized.get("status") == "confirmed" else "draft"
        blocks = normalized.get("blocks")
        revision["block_count"] = len(blocks) if isinstance(blocks, list) else 0
    if project_paths["prepared"].is_file():
        _atomic_copy(project_paths["prepared"], prepared_snapshot)
        revision["prepared_snapshot"] = prepared_snapshot.name
    atomic_write_json(voiceover_snapshot, voiceover_manifest)
    revision["voiceover_manifest_snapshot"] = voiceover_snapshot.name
    if project_paths["timeline"].is_file():
        timeline_snapshot = project_paths["timeline_versions"] / f"{revision_id}.full_timeline.json"
        _atomic_copy(project_paths["timeline"], timeline_snapshot)
        revision["timeline_snapshot"] = timeline_snapshot.name
    if project_paths["sync_matches"].is_file():
        matches_snapshot = project_paths["timeline_versions"] / f"{revision_id}.sync_matches.json"
        _atomic_copy(project_paths["sync_matches"], matches_snapshot)
        revision["sync_matches_snapshot"] = matches_snapshot.name
    stage3_mapping = project_paths["work"] / "stage3_source_mapping.json"
    if stage3_mapping.is_file():
        mapping_snapshot = project_paths["timeline_versions"] / f"{revision_id}.stage3_mapping.json"
        _atomic_copy(stage3_mapping, mapping_snapshot)
        revision["stage3_mapping_snapshot"] = mapping_snapshot.name
    atomic_write_json(manifest_path, revision)
    return revision


def validate_revision_assets(
    revision_id: str,
    revision: dict[str, Any],
    normalized: dict[str, Any],
    project_paths: dict[str, Path],
) -> None:
    """Validate every Stage 4 asset used by activation without mutating work/."""
    source_snapshot = str(revision.get("source_snapshot") or "")
    expected_source_snapshot = (
        f"{revision_id}.source{Path(str(revision.get('source_file') or '')).suffix.lower()}"
    )
    if source_snapshot != expected_source_snapshot:
        raise ValueError("invalid source snapshot")
    source_path = project_paths["scenario_versions"] / source_snapshot
    if not source_path.is_file():
        raise FileNotFoundError(f"Source snapshot not found: {source_snapshot}")
    expected_hash = str(revision.get("source_sha256") or "")
    if expected_hash:
        actual_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            raise ValueError("source snapshot hash mismatch")

    prepared_snapshot = str(revision.get("prepared_snapshot") or "")
    if prepared_snapshot and prepared_snapshot != f"{revision_id}.prepared.json":
        raise ValueError("invalid prepared snapshot")
    if prepared_snapshot:
        prepared_source = project_paths["scenario_versions"] / prepared_snapshot
        if not prepared_source.is_file() or not _read_json(prepared_source):
            raise ValueError("invalid prepared snapshot")

    manifest_snapshot = str(revision.get("voiceover_manifest_snapshot") or "")
    if manifest_snapshot and manifest_snapshot != f"{revision_id}.voiceover.json":
        raise ValueError("invalid voiceover manifest snapshot")
    manifest = {}
    if manifest_snapshot:
        manifest_source = project_paths["scenario_versions"] / manifest_snapshot
        if not manifest_source.is_file():
            raise FileNotFoundError("voiceover manifest snapshot not found")
        manifest = _read_json(manifest_source)
        if not manifest:
            raise ValueError("invalid voiceover manifest snapshot")
    for item in manifest.get("items", []):
        if not isinstance(item, dict):
            raise ValueError("invalid voiceover manifest item")
        block_id = str(item.get("id") or "")
        asset = str(item.get("asset") or "")
        if not re.fullmatch(r"zk\d+", block_id) or not asset:
            raise ValueError("invalid voiceover manifest item")
        source = _project_asset_path(project_paths, asset)
        if source is None or not source.is_file():
            raise FileNotFoundError(f"Voiceover asset not found for {block_id}")

    if not isinstance(normalized.get("blocks"), list):
        raise ValueError("invalid normalized snapshot")


def create_revision_asset_snapshots(
    revision_id: str,
    normalized: dict[str, Any],
    project_paths: dict[str, Path],
) -> tuple[str, str]:
    """Create a new revision's projections while reusing immutable audio by UID."""
    prepared = prepared_from_normalized(normalized)
    prepared_snapshot = project_paths["scenario_versions"] / f"{revision_id}.prepared.json"
    atomic_write_json(prepared_snapshot, prepared)

    active = _read_json(project_paths["active_voiceover_manifest"])
    parent_id = str(normalized.get("parent_revision_id") or "")
    parent_revision = (
        _read_json(project_paths["scenario_versions"] / f"{parent_id}.json") if parent_id else {}
    )
    parent_snapshot = str(parent_revision.get("voiceover_manifest_snapshot") or "")
    if parent_snapshot:
        parent_manifest = _read_json(project_paths["scenario_versions"] / parent_snapshot)
        if parent_manifest:
            active = parent_manifest
    by_uid = {
        str(item.get("block_uid") or ""): item
        for item in active.get("items", [])
        if isinstance(item, dict) and item.get("block_uid")
    }
    reusable_uids = {
        str(block.get("uid") or "")
        for block in normalized.get("blocks", [])
        if isinstance(block, dict)
        and isinstance(block.get("versioning"), dict)
        and block["versioning"].get("change_status") in {"unchanged", "moved"}
    }
    inherited = []
    for prepared_item in prepared.get("items", []):
        block_uid = str(prepared_item.get("block_uid") or "")
        if block_uid not in reusable_uids:
            continue
        source = by_uid.get(block_uid)
        if not source:
            continue
        item = dict(source)
        item["id"] = prepared_item["id"]
        item["block_uid"] = block_uid
        inherited.append(item)
    voiceover_snapshot = project_paths["scenario_versions"] / f"{revision_id}.voiceover.json"
    atomic_write_json(
        voiceover_snapshot,
        {
            "schema_version": 1,
            "scenario_revision_id": revision_id,
            "items": inherited,
        },
    )
    return prepared_snapshot.name, voiceover_snapshot.name


def save_tts_manifest(project_paths: dict[str, Path], result: dict[str, Any]) -> dict[str, Any]:
    items = []
    for raw in result.get("voiceover_assets", []):
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        asset_path = Path(str(item.get("asset") or ""))
        try:
            item["asset"] = (
                asset_path.resolve().relative_to(project_paths["root"].resolve()).as_posix()
            )
        except (OSError, ValueError):
            continue
        items.append(item)
    normalized = _read_json(project_paths["normalized"])
    active_revision_id = str(normalized.get("scenario_revision_id") or "")
    revision_id = str(result.get("scenario_revision_id") or active_revision_id)
    manifest = {
        "schema_version": 1,
        "scenario_revision_id": revision_id,
        "items": items,
    }
    if revision_id and active_revision_id and revision_id != active_revision_id:
        snapshot = project_paths["scenario_versions"] / f"{revision_id}.voiceover.json"
        atomic_write_json(snapshot, manifest)
        revision_path = project_paths["scenario_versions"] / f"{revision_id}.json"
        revision = _read_json(revision_path)
        if revision:
            revision["voiceover_manifest_snapshot"] = snapshot.name
            atomic_write_json(revision_path, revision)
        return manifest
    atomic_write_json(project_paths["active_voiceover_manifest"], manifest)
    if revision_id:
        snapshot_revision_assets(revision_id, project_paths)
    return manifest


def merge_voiceover_manifest_item(
    project_paths: dict[str, Path],
    *,
    revision_id: str,
    block_id: str,
    block_uid: str,
    fingerprint: str,
    speaker_dir: Path,
) -> None:
    """Record one freshly synthesized block in the active voiceover manifest.

    Stage 1 TTS updates ``speaker/<id>.wav`` and its sidecar fingerprint as
    soon as each block finishes, well before the whole batch — which may run
    for many items and can be cancelled or fail partway through — completes.
    ``save_tts_manifest`` only overwrites ``active_voiceover_manifest.json``
    once, after the entire batch returns; if the batch never returns, every
    already-resynthesized block is left recorded with a stale fingerprint in
    the manifest even though nothing on disk is actually broken. Merging
    each successful item here keeps the manifest a faithful, incrementally
    updated record of what is actually on disk, so
    ``stage4.versioning.inspect_revision_state`` does not mistake a
    legitimate, in-progress or partially cancelled resynthesis for project
    corruption.
    """
    if not re.fullmatch(r"zk\d+", block_id) or not fingerprint:
        return
    normalized = _read_json(project_paths["normalized"])
    active_revision_id = str(normalized.get("scenario_revision_id") or "")
    if revision_id and active_revision_id and revision_id != active_revision_id:
        # This synthesis run targets a revision other than the active one;
        # its manifest belongs in that revision's own snapshot (written by
        # save_tts_manifest once the batch completes), not in the active
        # projection. Never cross-contaminate the active manifest.
        return
    asset = speaker_dir / "assets" / _safe_uid(block_uid) / f"{fingerprint}.wav"
    if not asset.is_file():
        return
    try:
        asset_relative = asset.resolve().relative_to(project_paths["root"].resolve()).as_posix()
    except (OSError, ValueError):
        return
    manifest_path = project_paths["active_voiceover_manifest"]
    with _manifest_lock(manifest_path):
        manifest = _read_json(manifest_path)
        items = [
            item
            for item in manifest.get("items", [])
            if isinstance(item, dict) and str(item.get("id") or "") != block_id
        ]
        items.append(
            {
                "id": block_id,
                "block_uid": block_uid,
                "fingerprint": fingerprint,
                "asset": asset_relative,
            }
        )
        updated = {
            "schema_version": 1,
            "scenario_revision_id": revision_id or str(manifest.get("scenario_revision_id") or ""),
            "items": items,
        }
        atomic_write_json(manifest_path, updated)


def restore_revision_assets(
    revision_id: str,
    revision: dict[str, Any],
    normalized: dict[str, Any],
    project_paths: dict[str, Path],
) -> None:
    prepared_snapshot = str(revision.get("prepared_snapshot") or "")
    if prepared_snapshot and prepared_snapshot != f"{revision_id}.prepared.json":
        raise ValueError("invalid prepared snapshot")
    prepared_source = project_paths["scenario_versions"] / prepared_snapshot
    if prepared_snapshot and prepared_source.is_file():
        prepared_payload = _with_block_uids(_read_json(prepared_source), normalized)
    else:
        prepared_payload = prepared_from_normalized(normalized)

    manifest_snapshot = str(revision.get("voiceover_manifest_snapshot") or "")
    if manifest_snapshot and manifest_snapshot != f"{revision_id}.voiceover.json":
        raise ValueError("invalid voiceover manifest snapshot")
    manifest_source = project_paths["scenario_versions"] / manifest_snapshot
    manifest = _read_json(manifest_source) if manifest_snapshot else {}
    if not manifest:
        manifest = {"schema_version": 1, "scenario_revision_id": revision_id, "items": []}
    desired: dict[str, tuple[Path, str]] = {}
    for item in manifest.get("items", []):
        if not isinstance(item, dict):
            continue
        block_id = str(item.get("id") or "")
        asset = str(item.get("asset") or "")
        if not re.fullmatch(r"zk\d+", block_id) or not asset:
            continue
        source = _project_asset_path(project_paths, asset)
        if source is None or not source.is_file():
            raise FileNotFoundError(f"Voiceover asset not found for {block_id}")
        desired[block_id] = (source, str(item.get("fingerprint") or ""))

    # Validate and stage every target before changing this revision's alias
    # projection.  Never publish positional aliases into speaker/ itself:
    # that legacy shared directory made opening V2 overwrite V1's zkN.wav.
    alias_dir = revision_speaker_alias_dir(project_paths, revision_id)
    alias_dir.mkdir(parents=True, exist_ok=True)
    staged: dict[str, Path] = {}
    for block_id, (source, _fingerprint) in desired.items():
        destination = alias_dir / f".{block_id}.revision.tmp"
        _atomic_copy(source, destination)
        staged[block_id] = destination

    # Write a write-ahead intent journal before starting the actual commit
    # (prepared.json, the manifest, then N per-alias renames). A crash
    # partway through that commit leaves prepared.json/the manifest already
    # describing ``revision_id`` while some speaker/*.wav aliases still hold
    # the previous revision's audio. inspect_revision_state's recovery pass
    # uses the presence of this journal to know an activation was
    # interrupted and must be finished rather than mistaken for a project
    # someone else corrupted by hand.
    root = project_paths["root"].resolve()
    intent_path = activation_intent_path(project_paths)
    atomic_write_json(
        intent_path,
        {
            "schema_version": 1,
            "revision_id": revision_id,
            "created_at": datetime.now(UTC).isoformat(),
            "operations": [
                {
                    "block_id": block_id,
                    "staged": staged_path.resolve().relative_to(root).as_posix(),
                    "alias": (alias_dir / f"{block_id}.wav").resolve().relative_to(root).as_posix(),
                    "fingerprint": desired[block_id][1],
                }
                for block_id, staged_path in staged.items()
            ],
        },
    )

    atomic_write_json(project_paths["prepared"], prepared_payload)
    atomic_write_json(project_paths["active_voiceover_manifest"], manifest)
    for block_id, staged_path in staged.items():
        alias = alias_dir / f"{block_id}.wav"
        staged_path.replace(alias)
        fingerprint = desired[block_id][1]
        if fingerprint:
            # Record the fingerprint for every alias, including legacy
            # content-hash fingerprints, so downstream consistency checks
            # (see stage4.versioning.inspect_revision_state) can verify the
            # asset on disk without re-hashing potentially large audio
            # files. Legacy fingerprints are prefixed with "legacy-" and can
            # never collide with a real synthesis fingerprint, so recording
            # them here does not affect TTS cache invalidation.
            atomic_write_json(alias.with_suffix(".wav.meta"), {"fingerprint": fingerprint})
        else:
            alias.with_suffix(".wav.meta").unlink(missing_ok=True)
    for alias in alias_dir.iterdir():
        if (
            alias.is_file()
            and _VOICEOVER_ALIAS_RE.fullmatch(alias.name)
            and alias.stem not in desired
        ):
            alias.unlink()
            alias.with_suffix(".wav.meta").unlink(missing_ok=True)
    # Every alias now matches revision_id; the intent recorded above is done.
    intent_path.unlink(missing_ok=True)
