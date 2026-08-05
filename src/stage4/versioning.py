from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from difflib import SequenceMatcher
import hashlib
from pathlib import Path
import re
import shutil
from typing import Any, Callable
import unicodedata

from atomic_io import atomic_write_json


SUPPORTED_SCENARIOS = {".doc", ".docx", ".md", ".txt"}
_CLASSIFICATION_FIELDS = (
    "type", "text_for_tts", "original_label", "label_confirmed", "source",
    "confidence", "memory_rule_id", "speaker", "role", "suggested_type",
    "suggested_label", "proposed_type", "proposed_label",
    "proposed_label_confirmed", "proposed_source", "proposed_memory_rule_id",
    "continuation_of_type",
)


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    import json

    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("ё", "е")
    return " ".join(re.findall(r"[\w]+", text, flags=re.UNICODE))


def _speaker(block: dict[str, Any]) -> str:
    return _normalize(block.get("speaker") or block.get("original_label"))


def _block_text(block: dict[str, Any]) -> str:
    return _normalize(block.get("normalized_text") or block.get("original_text"))


def _block_uid(block: dict[str, Any]) -> str:
    return str(block.get("uid") or block.get("block_uid") or "").strip()


def _paths(project_paths: dict[str, Path]) -> dict[str, Path]:
    scenarios = project_paths["scenario_versions"]
    diffs = project_paths["scenario_diffs"]
    timelines = project_paths["timeline_versions"]
    scenarios.mkdir(parents=True, exist_ok=True)
    diffs.mkdir(parents=True, exist_ok=True)
    timelines.mkdir(parents=True, exist_ok=True)
    return {"scenarios": scenarios, "diffs": diffs, "timelines": timelines}


def list_revisions(project_paths: dict[str, Path]) -> list[dict[str, Any]]:
    storage = _paths(project_paths)
    revisions = []
    for path in sorted(storage["scenarios"].glob("rev_*.json")):
        if ".normalized." in path.name:
            continue
        try:
            revisions.append(_read_json(path))
        except (OSError, ValueError):
            continue
    active_path = project_paths["work"] / "active_revision.json"
    active_id = ""
    if active_path.is_file():
        try:
            active_id = str(_read_json(active_path).get("revision_id") or "")
        except (OSError, ValueError):
            pass
    for revision in revisions:
        revision["active"] = revision.get("revision_id") == active_id
    return revisions


def _revision_number(revision_id: str) -> int:
    match = re.fullmatch(r"rev_(\d+)", revision_id)
    return int(match.group(1)) if match else 0


def _find_source(project_paths: dict[str, Path], source_hash: str) -> Path:
    candidates = [
        path for path in project_paths["input"].iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_SCENARIOS
    ]
    for path in candidates:
        if _sha256(path) == source_hash:
            return path
    raise FileNotFoundError("Current normalized scenario source is not present in input/")


def _snapshot_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def ensure_baseline(project: str, project_paths: dict[str, Path]) -> dict[str, Any]:
    existing = list_revisions(project_paths)
    if existing:
        return existing[0]
    normalized_path = project_paths["normalized"]
    if not normalized_path.is_file():
        raise FileNotFoundError("scenario_normalized.json not found")
    normalized = _read_json(normalized_path)
    source_hash = str(normalized.get("source_hash") or "")
    source = _find_source(project_paths, source_hash)
    storage = _paths(project_paths)
    revision_id = "rev_0001"
    normalized_snapshot = storage["scenarios"] / f"{revision_id}.normalized.json"
    source_snapshot = storage["scenarios"] / f"{revision_id}.source{source.suffix.lower()}"
    atomic_write_json(normalized_snapshot, normalized)
    _snapshot_file(source, source_snapshot)

    timeline_snapshot = ""
    if project_paths["timeline"].is_file():
        destination = storage["timelines"] / f"{revision_id}.full_timeline.json"
        _snapshot_file(project_paths["timeline"], destination)
        timeline_snapshot = destination.name
    matches_snapshot = ""
    if project_paths["sync_matches"].is_file():
        destination = storage["timelines"] / f"{revision_id}.sync_matches.json"
        _snapshot_file(project_paths["sync_matches"], destination)
        matches_snapshot = destination.name

    manifest = {
        "schema_version": 1,
        "revision_id": revision_id,
        "parent_revision_id": "",
        "project": project,
        "source_file": source.name,
        "source_snapshot": source_snapshot.name,
        "source_sha256": source_hash,
        "normalized_snapshot": normalized_snapshot.name,
        "timeline_snapshot": timeline_snapshot,
        "sync_matches_snapshot": matches_snapshot,
        "created_at": _utcnow(),
        "status": "confirmed" if normalized.get("status") == "confirmed" else "draft",
        "block_count": len(normalized.get("blocks", [])),
    }
    atomic_write_json(storage["scenarios"] / f"{revision_id}.json", manifest)
    atomic_write_json(project_paths["work"] / "active_revision.json", {
        "revision_id": revision_id, "activated_at": _utcnow()
    })
    return manifest


def _exact_pairs(old_blocks: list[dict], new_blocks: list[dict]) -> tuple[list[tuple[int, int, str]], set[int], set[int]]:
    old_texts = [_block_text(block) for block in old_blocks]
    new_texts = [_block_text(block) for block in new_blocks]
    pairs: list[tuple[int, int, str]] = []
    used_old: set[int] = set()
    used_new: set[int] = set()
    matcher = SequenceMatcher(a=old_texts, b=new_texts, autojunk=False)
    for match in matcher.get_matching_blocks():
        for offset in range(match.size):
            old_index = match.a + offset
            new_index = match.b + offset
            if old_texts[old_index]:
                pairs.append((old_index, new_index, "unchanged"))
                used_old.add(old_index)
                used_new.add(new_index)

    old_by_text: dict[str, list[int]] = defaultdict(list)
    for index, text in enumerate(old_texts):
        if index not in used_old and text:
            old_by_text[text].append(index)
    for new_index, text in enumerate(new_texts):
        if new_index in used_new or not old_by_text.get(text):
            continue
        old_index = min(old_by_text[text], key=lambda candidate: abs(candidate - new_index))
        old_by_text[text].remove(old_index)
        pairs.append((old_index, new_index, "moved"))
        used_old.add(old_index)
        used_new.add(new_index)
    return pairs, used_old, used_new


def compare_blocks(old_blocks: list[dict], new_blocks: list[dict]) -> dict[str, Any]:
    pairs, used_old, used_new = _exact_pairs(old_blocks, new_blocks)
    candidates: list[tuple[float, int, int]] = []
    for old_index, old in enumerate(old_blocks):
        if old_index in used_old:
            continue
        old_text = _block_text(old)
        for new_index, new in enumerate(new_blocks):
            if new_index in used_new:
                continue
            similarity = SequenceMatcher(
                None, old_text, _block_text(new), autojunk=False
            ).ratio()
            score = similarity
            if old.get("type") == new.get("type"):
                score += 0.06
            if _speaker(old) and _speaker(old) == _speaker(new):
                score += 0.04
            if score >= 0.58:
                candidates.append((score, old_index, new_index))
    for score, old_index, new_index in sorted(candidates, reverse=True):
        if old_index in used_old or new_index in used_new:
            continue
        old = old_blocks[old_index]
        new = new_blocks[new_index]
        if new.get("type") not in {"", "unknown", old.get("type")}:
            status = "type_changed"
        elif _speaker(new) and _speaker(old) != _speaker(new):
            status = "speaker_changed"
        else:
            status = "edited"
        pairs.append((old_index, new_index, status))
        used_old.add(old_index)
        used_new.add(new_index)

    changes = []
    for old_index, new_index, status in sorted(pairs, key=lambda item: item[1]):
        old = old_blocks[old_index]
        new = new_blocks[new_index]
        if status in {"unchanged", "moved"}:
            if new.get("type") not in {"", "unknown", old.get("type")}:
                status = "type_changed"
            elif _speaker(new) and _speaker(old) != _speaker(new):
                status = "speaker_changed"
        changes.append({
            "status": status,
            "old_index": old_index,
            "new_index": new_index,
            "old_block_uid": _block_uid(old),
            "new_import_uid": _block_uid(new),
            "text_similarity": round(
                SequenceMatcher(None, _block_text(old), _block_text(new), autojunk=False).ratio(), 4
            ),
            "old_type": old.get("type", ""),
            "new_type": new.get("type", ""),
            "old_speaker": old.get("speaker", ""),
            "new_speaker": new.get("speaker", ""),
            "old_text": old.get("normalized_text") or old.get("original_text") or "",
            "new_text": new.get("normalized_text") or new.get("original_text") or "",
        })
    for new_index, new in enumerate(new_blocks):
        if new_index not in used_new:
            changes.append({
                "status": "added", "old_index": None, "new_index": new_index,
                "old_block_uid": "", "new_import_uid": _block_uid(new),
                "text_similarity": 0.0, "old_type": "", "new_type": new.get("type", ""),
                "old_speaker": "", "new_speaker": new.get("speaker", ""),
                "old_text": "", "new_text": new.get("normalized_text") or new.get("original_text") or "",
            })
    for old_index, old in enumerate(old_blocks):
        if old_index not in used_old:
            changes.append({
                "status": "removed", "old_index": old_index, "new_index": None,
                "old_block_uid": _block_uid(old), "new_import_uid": "",
                "text_similarity": 0.0, "old_type": old.get("type", ""), "new_type": "",
                "old_speaker": old.get("speaker", ""), "new_speaker": "",
                "old_text": old.get("normalized_text") or old.get("original_text") or "", "new_text": "",
            })
    changes.sort(key=lambda item: (
        item["new_index"] is None,
        item["new_index"] if item["new_index"] is not None else item["old_index"],
    ))
    summary: dict[str, int] = defaultdict(int)
    for change in changes:
        summary[change["status"]] += 1
    return {"summary": dict(summary), "changes": changes}


def _inherit_blocks(old_blocks: list[dict], new_blocks: list[dict], diff: dict[str, Any]) -> None:
    for change in diff["changes"]:
        if change["old_index"] is None or change["new_index"] is None:
            continue
        old = old_blocks[change["old_index"]]
        new = new_blocks[change["new_index"]]
        import_uid = _block_uid(new)
        new["uid"] = _block_uid(old)
        new["versioning"] = {
            "import_uid": import_uid,
            "parent_block_uid": _block_uid(old),
            "change_status": change["status"],
        }
        explicit_change = (
            new.get("type") not in {"", "unknown"}
            and bool(new.get("original_label"))
            and new.get("type") != old.get("type")
        )
        if not explicit_change and (
            change["text_similarity"] == 1.0 or new.get("type") in {"", "unknown"}
        ):
            for field in _CLASSIFICATION_FIELDS:
                if field in old:
                    new[field] = old[field]
            if change["text_similarity"] != 1.0 and new.get("type") == "voiceover":
                new["text_for_tts"] = new.get("normalized_text", "")
    for change in diff["changes"]:
        if change["status"] != "added" or change["new_index"] is None:
            continue
        new = new_blocks[change["new_index"]]
        new["versioning"] = {"import_uid": _block_uid(new), "change_status": "added"}
    for index, block in enumerate(new_blocks):
        if block.get("type") != "unknown":
            continue
        text = str(block.get("normalized_text") or block.get("original_text") or "").strip()
        previous_type = str(new_blocks[index - 1].get("type") or "") if index else ""
        if _normalize(text) == "титры" or (
            text.startswith((",", ";")) and previous_type == "technical"
        ):
            block.update({
                "type": "technical", "source": "builtin_rule", "confidence": 1.0,
                "proposed_type": "technical", "proposed_source": "builtin_rule",
                "continuation_of_type": previous_type if text.startswith((",", ";")) else "",
            })


def _execution_plan(diff: dict[str, Any], old_blocks: list[dict], new_blocks: list[dict]) -> dict[str, Any]:
    reusable = {"unchanged", "moved"}
    reuse_uids = []
    reuse_tts = 0
    reuse_matches = 0
    new_tts = 0
    rematch = 0
    old_speakers = {_speaker(block) for block in old_blocks if block.get("type") == "sync"}
    new_speakers: set[str] = set()
    for change in diff["changes"]:
        if change["new_index"] is None:
            continue
        block = new_blocks[change["new_index"]]
        status = change["status"]
        if status in reusable:
            reuse_uids.append(_block_uid(block))
            reuse_tts += int(block.get("type") == "voiceover")
            reuse_matches += int(block.get("type") == "sync")
        elif block.get("type") == "voiceover":
            new_tts += 1
        elif block.get("type") == "sync":
            rematch += 1
            speaker = _speaker(block)
            if speaker and speaker not in old_speakers:
                new_speakers.add(str(block.get("speaker") or block.get("original_label") or ""))
    return {
        "reuse": {
            "blocks": len(reuse_uids), "tts": reuse_tts,
            "sync_matches": reuse_matches, "whisper_transcripts": "all_existing",
        },
        "run": {
            "tts": new_tts, "sync_match": rematch,
            "new_speakers": sorted(new_speakers), "timeline_xml": 1,
        },
        "reusable_block_uids": reuse_uids,
    }


def import_revision(
    project: str,
    project_paths: dict[str, Path],
    *,
    generate_normalization: Callable[..., dict[str, Any]],
    memory_path: Path,
    input_path: Path | None = None,
    use_llm: bool = False,
    hint: str = "",
) -> dict[str, Any]:
    baseline = ensure_baseline(project, project_paths)
    revisions = list_revisions(project_paths)
    known_hashes = {str(item.get("source_sha256") or "") for item in revisions}
    if input_path is None:
        candidates = [
            path for path in project_paths["input"].iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_SCENARIOS
            and _sha256(path) not in known_hashes
        ]
        if not candidates:
            raise ValueError("No new scenario file found in input/")
        input_path = max(candidates, key=lambda path: path.stat().st_mtime_ns)
    input_path = input_path.resolve()
    if not input_path.is_file() or input_path.suffix.lower() not in SUPPORTED_SCENARIOS:
        raise ValueError("New scenario must be a .doc, .docx, .txt or .md file")
    source_hash = _sha256(input_path)
    if source_hash in known_hashes:
        raise ValueError("This scenario revision has already been imported")

    parent = max(revisions, key=lambda item: _revision_number(item["revision_id"]))
    revision_id = f"rev_{_revision_number(parent['revision_id']) + 1:04d}"
    storage = _paths(project_paths)
    normalized_path = storage["scenarios"] / f"{revision_id}.normalized.json"
    generated = generate_normalization(
        input_path, normalized_path, memory_path=memory_path,
        use_llm=use_llm, hint=hint,
    )
    parent_normalized = _read_json(
        storage["scenarios"] / str(parent["normalized_snapshot"])
    )
    old_blocks = list(parent_normalized.get("blocks", []))
    new_blocks = list(generated.get("blocks", []))
    diff = compare_blocks(old_blocks, new_blocks)
    _inherit_blocks(old_blocks, new_blocks, diff)
    generated["blocks"] = new_blocks
    parent_source = Path(str(parent_normalized.get("source") or input_path))
    generated["source"] = str(parent_source.parent / input_path.name)
    generated["scenario_revision_id"] = revision_id
    generated["parent_revision_id"] = parent["revision_id"]
    atomic_write_json(normalized_path, generated)

    diff_id = f"diff_{parent['revision_id']}_{revision_id}"
    plan = _execution_plan(diff, old_blocks, new_blocks)
    diff_document = {
        "schema_version": 1, "diff_id": diff_id, "project": project,
        "from_revision_id": parent["revision_id"], "to_revision_id": revision_id,
        "created_at": _utcnow(), **diff, "execution_plan": plan,
    }
    diff_path = storage["diffs"] / f"{diff_id}.json"
    atomic_write_json(diff_path, diff_document)
    source_snapshot = storage["scenarios"] / f"{revision_id}.source{input_path.suffix.lower()}"
    _snapshot_file(input_path, source_snapshot)
    manifest = {
        "schema_version": 1, "revision_id": revision_id,
        "parent_revision_id": parent["revision_id"], "project": project,
        "source_file": input_path.name, "source_snapshot": source_snapshot.name,
        "source_sha256": source_hash, "normalized_snapshot": normalized_path.name,
        "diff_id": diff_id, "diff_file": diff_path.name,
        "created_at": _utcnow(), "status": "draft", "block_count": len(new_blocks),
        "summary": diff["summary"], "execution_plan": plan,
    }
    atomic_write_json(storage["scenarios"] / f"{revision_id}.json", manifest)
    return {"revision": manifest, "diff": diff_document, "baseline": baseline}


def activate_revision(
    revision_id: str,
    project_paths: dict[str, Path],
) -> dict[str, Any]:
    storage = _paths(project_paths)
    manifest_path = storage["scenarios"] / f"{revision_id}.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Revision not found: {revision_id}")
    manifest = _read_json(manifest_path)
    normalized = _read_json(storage["scenarios"] / manifest["normalized_snapshot"])
    atomic_write_json(project_paths["normalized"], normalized)

    reusable: set[str] = set()
    source_matches_path = project_paths["sync_matches"]
    if manifest.get("sync_matches_snapshot"):
        source_matches_path = storage["timelines"] / manifest["sync_matches_snapshot"]
    if manifest.get("diff_file"):
        diff = _read_json(storage["diffs"] / manifest["diff_file"])
        reusable = set(diff.get("execution_plan", {}).get("reusable_block_uids", []))
        parent_id = str(manifest.get("parent_revision_id") or "")
        parent_path = storage["scenarios"] / f"{parent_id}.json"
        if parent_path.is_file():
            parent = _read_json(parent_path)
            if parent.get("sync_matches_snapshot"):
                source_matches_path = storage["timelines"] / parent["sync_matches_snapshot"]
    matches = _read_json(source_matches_path) if source_matches_path.is_file() else {"schema_version": 1, "matches": []}
    kept = list(matches.get("matches", []))
    if manifest.get("diff_file"):
        kept = []
        for match in matches.get("matches", []):
            metadata = match.get("metadata") if isinstance(match.get("metadata"), dict) else {}
            parent_uid = str(metadata.get("parent_block_uid") or match.get("segment_id") or "").split("::fragment:", 1)[0]
            if parent_uid in reusable:
                kept.append(match)
    matches["matches"] = kept
    atomic_write_json(project_paths["sync_matches"], matches)
    atomic_write_json(project_paths["work"] / "active_revision.json", {
        "revision_id": revision_id, "activated_at": _utcnow()
    })
    if manifest.get("timeline_snapshot"):
        _snapshot_file(
            storage["timelines"] / manifest["timeline_snapshot"],
            project_paths["timeline"],
        )
    return {"revision": manifest, "reused_sync_matches": len(kept)}
