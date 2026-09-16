from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from contextlib import contextmanager
from datetime import UTC, datetime
from difflib import SequenceMatcher
from functools import wraps
import hashlib
import json
from pathlib import Path
import re
import shutil
from threading import Lock, RLock
from typing import Any, Callable
import unicodedata

from atomic_io import atomic_write_json
from stage4.revision_assets import (
    activation_intent_path,
    create_revision_asset_snapshots,
    revision_speaker_alias_dir,
    restore_revision_assets,
    snapshot_revision_assets,
    validate_revision_assets,
)


SUPPORTED_SCENARIOS = {".doc", ".docx", ".md", ".txt"}
_CLASSIFICATION_FIELDS = (
    "type",
    "text_for_tts",
    "original_label",
    "label_confirmed",
    "source",
    "confidence",
    "memory_rule_id",
    "speaker",
    "role",
    "suggested_type",
    "suggested_label",
    "proposed_type",
    "proposed_label",
    "proposed_label_confirmed",
    "proposed_source",
    "proposed_memory_rule_id",
    "continuation_of_type",
)
_DOWNSTREAM_FIELDS = (
    "type",
    "speaker",
    "role",
    "text_for_tts",
    "timecode_start",
    "timecode_end",
    "sync_fragments",
)
# Spelling variants of a title do not identify a different interviewee.  Keep
# this deliberately small: broad fuzzy matching of names would risk merging
# genuinely different people.
_SPEAKER_TOKEN_ALIASES = {
    "протоирей": "протоиерей",
}
_PROJECT_LOCKS: dict[str, RLock] = {}
_PROJECT_LOCKS_GUARD = Lock()


class RevisionStateConflictError(ValueError):
    """The active marker and mutable work projection describe different revisions."""


@contextmanager
def _project_lock(project_paths: dict[str, Path]):
    key = str(project_paths["root"].resolve())
    with _PROJECT_LOCKS_GUARD:
        lock = _PROJECT_LOCKS.setdefault(key, RLock())
    with lock:
        yield


def _locked_project_paths(index: int):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            project_paths = args[index] if len(args) > index else kwargs["project_paths"]
            with _project_lock(project_paths):
                return function(*args, **kwargs)

        return wrapped

    return decorate


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
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
    value = _normalize(block.get("speaker") or block.get("original_label"))
    return " ".join(_SPEAKER_TOKEN_ALIASES.get(token, token) for token in value.split())


def _block_text(block: dict[str, Any]) -> str:
    return _normalize(block.get("normalized_text") or block.get("original_text"))


def _block_uid(block: dict[str, Any]) -> str:
    return str(block.get("uid") or block.get("block_uid") or "").strip()


def _downstream_changes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    changed = []
    for field in _DOWNSTREAM_FIELDS:
        if field == "sync_fragments":
            old_value = old.get(field) if isinstance(old.get(field), list) else []
            new_value = new.get(field) if isinstance(new.get(field), list) else []
        elif field == "speaker":
            old_value = _speaker(old)
            new_value = _speaker(new)
        else:
            old_value = str(old.get(field) or "")
            new_value = str(new.get(field) or "")
        if old_value != new_value:
            changed.append(field)
    return changed


def _restore_exact_auto_classifications(old_blocks: list[dict], new_blocks: list[dict]) -> None:
    """Keep a confirmed classification when auto-classification changes it.

    An unmarked paragraph can receive a different LLM label on two runs, or
    be left unresolved after an unavailable/uncertain response.  A new
    revision must not turn an exactly unchanged, already-confirmed block into
    a different speaker/type or silently drop its TTS/sync metadata. Only
    exact one-to-one text matches with no new explicit label qualify; a
    manual classification or textual edit remains authoritative.
    """
    pairs, _, _ = _exact_pairs(old_blocks, new_blocks)
    inherited_fields = (*_CLASSIFICATION_FIELDS, "timecode_start", "timecode_end", "sync_fragments")
    for old_index, new_index, _status in pairs:
        old = old_blocks[old_index]
        new = new_blocks[new_index]
        automatically_classified = str(new.get("source") or "") in {"llm", "unknown"}
        if (
            old.get("type") in {"", "unknown"}
            or not automatically_classified
            or new.get("original_label")
        ):
            continue
        for field in inherited_fields:
            if field in old:
                new[field] = deepcopy(old[field])


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
        if not re.fullmatch(r"rev_\d+\.json", path.name):
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


def _asset_projection_mismatches(project_paths: dict[str, Path], active_id: str) -> list[str]:
    """Report Stage 4 asset projections that disagree with the active revision.

    ``restore_revision_assets`` commits ``prepared.json`` and the active
    voiceover manifest before it renames any ``speaker/*.wav`` alias into
    place.  A crash partway through that rename loop leaves those two
    projections fully describing the new revision while
    ``active_revision.json``/``scenario_normalized.json`` still describe the
    old one, and some aliases still hold the old revision's audio.  Neither
    projection is covered by the ``scenario_normalized.json`` comparison
    above, so check them independently here.
    """

    try:
        prepared = _read_json(project_paths["prepared"])
    except (OSError, ValueError):
        prepared = {}
    mismatches: list[str] = []
    prepared_id = str(prepared.get("scenario_revision_id") or "")
    if prepared_id and prepared_id != active_id:
        mismatches.append("prepared")

    try:
        manifest = _read_json(project_paths["active_voiceover_manifest"])
    except (OSError, ValueError):
        manifest = {}
    manifest_id = str(manifest.get("scenario_revision_id") or "")
    if manifest_id and manifest_id != active_id:
        mismatches.append("voiceover_manifest")
        # The manifest itself is stale; the assets it lists cannot be
        # meaningfully checked against it.
        return mismatches

    items = manifest.get("items")
    speaker = revision_speaker_alias_dir(project_paths, active_id)
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        block_id = str(item.get("id") or "")
        if not re.fullmatch(r"zk\d+", block_id):
            continue
        alias = speaker / f"{block_id}.wav"
        if not alias.is_file():
            mismatches.append(f"voiceover_asset:{block_id}")
            continue
        fingerprint = str(item.get("fingerprint") or "")
        if not fingerprint:
            continue
        meta_path = alias.with_suffix(".wav.meta")
        if not meta_path.is_file():
            # No sidecar means no recorded fingerprint to compare against;
            # this is expected for projects predating the sidecar and must
            # not be treated as a mismatch.
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        recorded = str(meta.get("fingerprint") or "") if isinstance(meta, dict) else ""
        if recorded and recorded != fingerprint:
            mismatches.append(f"voiceover_asset:{block_id}")
    return mismatches


def recover_interrupted_activation(project_paths: dict[str, Path]) -> dict[str, Any] | None:
    """Finish an activation that crashed mid-commit, if one was left in flight.

    ``restore_revision_assets`` writes a write-ahead intent journal (see
    ``stage4.revision_assets.activation_intent_path``) before it starts
    committing ``prepared.json``, the active voiceover manifest and the
    ``speaker/*.wav`` alias renames for a new revision, and removes it once
    every alias is in place. Its presence therefore means a previous
    ``activate_revision`` call was interrupted partway through that commit.
    Every step it performs is either already-durable, content-addressed data
    (the ``voiceover_assets`` sources it stages from) or an idempotent
    ``atomic_write_json``, so resuming ``activate_revision`` (which skips
    re-snapshotting the outgoing revision -- see the ``_resume`` parameter --
    since a journal proves that already happened before the interrupted
    attempt started committing) finishes exactly what was interrupted,
    including the projections ``restore_revision_assets`` itself does not
    own (``scenario_normalized.json``, ``sync_matches.json``, the timeline
    and ``active_revision.json``).

    Returns ``None`` when there is nothing to recover. Otherwise returns a
    result describing the outcome and never raises: read-only integrity
    checks (``inspect_revision_state`` and everything that calls it) must be
    able to report a clear diagnosis instead of crashing on an interrupted
    activation they did not cause.

    Concurrent callers (every ``inspect_revision_state`` caller, including
    plain GET status routes, can run at the same time) must not both decide
    to resume the same interrupted activation: a second caller that only
    acquires the lock after the first has already finished would otherwise
    replay ``activate_revision`` against a revision that is already active.
    Check for the journal twice -- once unlocked, so the overwhelming
    majority of calls (no interrupted activation in flight) never pay for
    the project lock, and once again after acquiring ``_project_lock``,
    which is what actually rules out that race. ``_project_lock`` is
    reentrant, so resuming into ``activate_revision`` (itself
    ``@_locked_project_paths``) from within this lock does not deadlock.
    """
    journal_path = activation_intent_path(project_paths)
    if not journal_path.is_file():
        return None
    with _project_lock(project_paths):
        if not journal_path.is_file():
            # Another thread already resumed and finished this activation
            # while we were waiting for the lock; nothing left to do.
            return None
        try:
            intent = _read_json(journal_path)
        except (OSError, ValueError):
            return {
                "recovered": False,
                "revision_id": "",
                "error": "журнал незавершённой активации повреждён и не может быть прочитан",
            }
        revision_id = str(intent.get("revision_id") or "")
        if not re.fullmatch(r"rev_\d+", revision_id):
            return {
                "recovered": False,
                "revision_id": revision_id,
                "error": "журнал незавершённой активации не содержит корректный id редакции",
            }
        try:
            activate_revision(revision_id, project_paths, _resume=True)
        except Exception as exc:  # noqa: BLE001 - report, never crash a read-only integrity check
            return {
                "recovered": False,
                "revision_id": revision_id,
                "error": f"не удалось автоматически завершить активацию {revision_id}: {exc}",
            }
    return {"recovered": True, "revision_id": revision_id, "error": None}


def inspect_revision_state(project_paths: dict[str, Path]) -> dict[str, Any]:
    """Describe whether the mutable work projection belongs to the active revision.

    Every call first gives ``recover_interrupted_activation`` a chance to
    finish an activation a previous crash left mid-commit, so switching,
    importing and every status display that already goes through this
    function (see ``import_revision``, ``reconcile_revision_state``, and the
    ``api*.py`` routes that call it) self-heals instead of staying blocked
    on ``active_assets_mismatch`` forever. Use ``_detect_revision_state`` to
    inspect the raw, pre-recovery state without triggering that side effect.
    """

    recovery = recover_interrupted_activation(project_paths)
    if recovery is not None and not recovery["recovered"]:
        return {
            "status": "activation_recovery_failed",
            "blocking": True,
            "active_revision_id": str(recovery.get("revision_id") or ""),
            "message": (
                "Активация редакции была прервана крэшем и не может быть завершена "
                f"автоматически: {recovery['error']}. "
                "Переключение и импорт заблокированы до восстановления проекта."
            ),
        }
    return _detect_revision_state(project_paths)


def _detect_revision_state(project_paths: dict[str, Path]) -> dict[str, Any]:
    """Pure, read-only revision/asset consistency check (no recovery)."""

    revisions = list_revisions(project_paths)
    if not revisions:
        return {"status": "no_history", "blocking": False, "message": ""}
    active_path = project_paths["work"] / "active_revision.json"
    if not active_path.is_file():
        return {
            "status": "missing_active_revision",
            "blocking": True,
            "message": "История существует, но активная редакция не определена.",
        }
    try:
        active_id = str(_read_json(active_path).get("revision_id") or "")
    except (OSError, ValueError):
        active_id = ""
    known_ids = {str(item.get("revision_id") or "") for item in revisions}
    if active_id not in known_ids:
        return {
            "status": "invalid_active_revision",
            "blocking": True,
            "active_revision_id": active_id,
            "message": "Указатель активной редакции не соответствует истории.",
        }
    try:
        working_id = str(_read_json(project_paths["normalized"]).get("scenario_revision_id") or "")
    except (OSError, ValueError):
        working_id = ""
    if working_id != active_id:
        return {
            "status": "active_work_mismatch",
            "blocking": True,
            "active_revision_id": active_id,
            "working_revision_id": working_id,
            "message": (
                "Активная редакция и рабочий сценарий расходятся. "
                "Переключение и импорт заблокированы до восстановления проекта."
            ),
        }
    mismatched_assets = _asset_projection_mismatches(project_paths, active_id)
    if mismatched_assets:
        return {
            "status": "active_assets_mismatch",
            "blocking": True,
            "active_revision_id": active_id,
            "working_revision_id": working_id,
            "mismatched_assets": mismatched_assets,
            "message": (
                "Подготовленная озвучка (prepared.json, манифест или файлы speaker/*.wav) "
                "расходится с активной редакцией. Переключение и импорт заблокированы "
                "до восстановления проекта."
            ),
        }
    return {
        "status": "ok",
        "blocking": False,
        "active_revision_id": active_id,
        "working_revision_id": working_id,
        "message": "",
    }


def _revision_number(revision_id: str) -> int:
    match = re.fullmatch(r"rev_(\d+)", revision_id)
    return int(match.group(1)) if match else 0


def _find_source(project_paths: dict[str, Path], source_hash: str) -> Path:
    candidates: list[Path] = []
    for directory in (
        project_paths["input"],
        project_paths.get("inbox_scenarios", project_paths["input"]),
    ):
        candidates.extend(
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_SCENARIOS
        )
    for path in candidates:
        if _sha256(path) == source_hash:
            return path
    raise FileNotFoundError("Current normalized scenario source is not present in input/")


def _snapshot_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    shutil.copy2(source, temporary)
    temporary.replace(destination)


@_locked_project_paths(1)
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
    normalized["scenario_revision_id"] = revision_id
    normalized["parent_revision_id"] = ""
    atomic_write_json(normalized_path, normalized)
    normalized_snapshot = storage["scenarios"] / f"{revision_id}.normalized.json"
    source_snapshot = storage["scenarios"] / f"{revision_id}.source{source.suffix.lower()}"
    _snapshot_file(source, source_snapshot)
    normalized["source"] = str(source_snapshot.resolve())
    atomic_write_json(normalized_path, normalized)
    atomic_write_json(normalized_snapshot, normalized)

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
    atomic_write_json(
        project_paths["work"] / "active_revision.json",
        {"revision_id": revision_id, "activated_at": _utcnow()},
    )
    snapshotted = snapshot_revision_assets(revision_id, project_paths) or manifest
    # Migrate the baseline's legacy top-level zkN aliases into its own
    # projection immediately.  Future integrity checks and Stage 2 builds
    # must never depend on the shared pre-versioning speaker directory.
    restore_revision_assets(revision_id, snapshotted, normalized, project_paths)
    return snapshotted


def _exact_pairs(
    old_blocks: list[dict], new_blocks: list[dict]
) -> tuple[list[tuple[int, int, str]], set[int], set[int]]:
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
            similarity = SequenceMatcher(None, old_text, _block_text(new), autojunk=False).ratio()
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
        downstream_changes = _downstream_changes(old, new)
        if status in {"unchanged", "moved"}:
            if new.get("type") not in {"", "unknown", old.get("type")}:
                status = "type_changed"
            elif _speaker(new) and _speaker(old) != _speaker(new):
                status = "speaker_changed"
            elif downstream_changes:
                status = "edited"
        changes.append(
            {
                "status": status,
                "old_index": old_index,
                "new_index": new_index,
                "old_block_uid": _block_uid(old),
                "new_import_uid": _block_uid(new),
                "text_similarity": round(
                    SequenceMatcher(
                        None, _block_text(old), _block_text(new), autojunk=False
                    ).ratio(),
                    4,
                ),
                "old_type": old.get("type", ""),
                "new_type": new.get("type", ""),
                "old_speaker": old.get("speaker", ""),
                "new_speaker": new.get("speaker", ""),
                "old_text": old.get("normalized_text") or old.get("original_text") or "",
                "new_text": new.get("normalized_text") or new.get("original_text") or "",
                "downstream_changed_fields": downstream_changes,
            }
        )
    for new_index, new in enumerate(new_blocks):
        if new_index not in used_new:
            changes.append(
                {
                    "status": "added",
                    "old_index": None,
                    "new_index": new_index,
                    "old_block_uid": "",
                    "new_import_uid": _block_uid(new),
                    "text_similarity": 0.0,
                    "old_type": "",
                    "new_type": new.get("type", ""),
                    "old_speaker": "",
                    "new_speaker": new.get("speaker", ""),
                    "old_text": "",
                    "new_text": new.get("normalized_text") or new.get("original_text") or "",
                }
            )
    for old_index, old in enumerate(old_blocks):
        if old_index not in used_old:
            changes.append(
                {
                    "status": "removed",
                    "old_index": old_index,
                    "new_index": None,
                    "old_block_uid": _block_uid(old),
                    "new_import_uid": "",
                    "text_similarity": 0.0,
                    "old_type": old.get("type", ""),
                    "new_type": "",
                    "old_speaker": old.get("speaker", ""),
                    "new_speaker": "",
                    "old_text": old.get("normalized_text") or old.get("original_text") or "",
                    "new_text": "",
                }
            )
    changes.sort(
        key=lambda item: (
            item["new_index"] is None,
            item["new_index"] if item["new_index"] is not None else item["old_index"],
        )
    )
    summary: dict[str, int] = defaultdict(int)
    for change in changes:
        summary[change["status"]] += 1
    return {"summary": dict(summary), "changes": changes}


def repair_active_revision_metadata(
    project_paths: dict[str, Path],
) -> dict[str, Any]:
    """Restore Stage 4 metadata lost by older normalization round-trips.

    Early Stage 4 builds did not preserve ``scenario_revision_id`` and block
    ``versioning`` during confirmation.  Match the active revision snapshot to
    the confirmed working copy by content and restore only that metadata,
    leaving the user's confirmed classifications and text untouched.
    """
    active_path = project_paths["work"] / "active_revision.json"
    if not active_path.is_file():
        return {"repaired": False, "reason": "no_active_revision"}
    active = _read_json(active_path)
    revision_id = str(active.get("revision_id") or "")
    if not re.fullmatch(r"rev_\d+", revision_id):
        return {"repaired": False, "reason": "no_active_revision"}
    current = _read_json(project_paths["normalized"])
    blocks = current.get("blocks")
    if not isinstance(blocks, list):
        return {"repaired": False, "reason": "normalization_missing"}
    has_revision_id = str(current.get("scenario_revision_id") or "") == revision_id
    has_block_metadata = (
        all(
            isinstance(block, dict) and isinstance(block.get("versioning"), dict)
            for block in blocks
        )
        if blocks
        else True
    )
    if has_revision_id and has_block_metadata:
        return {"repaired": False, "reason": "metadata_present"}

    manifest = _read_json(project_paths["scenario_versions"] / f"{revision_id}.json")
    snapshot_name = str(manifest.get("normalized_snapshot") or "")
    if snapshot_name != f"{revision_id}.normalized.json":
        return {"repaired": False, "reason": "snapshot_missing"}
    snapshot = _read_json(project_paths["scenario_versions"] / snapshot_name)
    snapshot_blocks = snapshot.get("blocks")
    if not isinstance(snapshot_blocks, list):
        return {"repaired": False, "reason": "snapshot_invalid"}

    alignment = compare_blocks(snapshot_blocks, blocks)
    paired = [
        change
        for change in alignment["changes"]
        if change["old_index"] is not None and change["new_index"] is not None
    ]
    if len(paired) != len(snapshot_blocks) or len(paired) != len(blocks):
        return {"repaired": False, "reason": "scenario_diverged"}

    restored = 0
    for change in paired:
        old = snapshot_blocks[change["old_index"]]
        new = blocks[change["new_index"]]
        metadata = old.get("versioning")
        if isinstance(metadata, dict):
            new["versioning"] = dict(metadata)
            restored += 1
    current["scenario_revision_id"] = revision_id
    current["parent_revision_id"] = str(manifest.get("parent_revision_id") or "")
    source_snapshot_name = str(manifest.get("source_snapshot") or "")
    source_snapshot = project_paths["scenario_versions"] / source_snapshot_name
    if source_snapshot_name and source_snapshot.is_file():
        expected_hash = str(manifest.get("source_sha256") or "")
        if not expected_hash or _sha256(source_snapshot) == expected_hash:
            current["source"] = str(source_snapshot.resolve())
            current["source_hash"] = expected_hash or _sha256(source_snapshot)
    atomic_write_json(project_paths["normalized"], current)
    return {
        "repaired": True,
        "revision_id": revision_id,
        "blocks": restored,
    }


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
        if change["status"] not in {"type_changed", "speaker_changed"} and (
            change["text_similarity"] == 1.0 or new.get("type") in {"", "unknown"}
        ):
            changed_fields = set(change.get("downstream_changed_fields") or [])
            for field in _CLASSIFICATION_FIELDS:
                if field in old and field not in changed_fields:
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
            block.update(
                {
                    "type": "technical",
                    "source": "builtin_rule",
                    "confidence": 1.0,
                    "proposed_type": "technical",
                    "proposed_source": "builtin_rule",
                    "continuation_of_type": previous_type if text.startswith((",", ";")) else "",
                }
            )


def _execution_plan(
    diff: dict[str, Any], old_blocks: list[dict], new_blocks: list[dict]
) -> dict[str, Any]:
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
            "blocks": len(reuse_uids),
            "tts": reuse_tts,
            "sync_matches": reuse_matches,
            "whisper_transcripts": "all_existing",
        },
        "run": {
            "tts": new_tts,
            "sync_match": rematch,
            "new_speakers": sorted(new_speakers),
            "timeline_xml": 1,
        },
        "reusable_block_uids": reuse_uids,
    }


@_locked_project_paths(1)
def import_revision(
    project: str,
    project_paths: dict[str, Path],
    *,
    generate_normalization: Callable[..., dict[str, Any]],
    memory_path: Path,
    input_path: Path | None = None,
    use_llm: bool = False,
    hint: str = "",
    usage_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    baseline = ensure_baseline(project, project_paths)
    integrity = inspect_revision_state(project_paths)
    if integrity.get("blocking"):
        raise RevisionStateConflictError(str(integrity.get("message") or "revision state conflict"))
    revisions = list_revisions(project_paths)
    known_hashes = {str(item.get("source_sha256") or "") for item in revisions}
    if input_path is None:
        candidates = [
            path
            for path in project_paths["input"].iterdir()
            if path.is_file()
            and path.suffix.lower() in SUPPORTED_SCENARIOS
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

    latest = max(revisions, key=lambda item: _revision_number(item["revision_id"]))
    parent = latest
    active_path = project_paths["work"] / "active_revision.json"
    if active_path.is_file():
        active_id = str(_read_json(active_path).get("revision_id") or "")
        active = next(
            (item for item in revisions if item.get("revision_id") == active_id),
            None,
        )
        if active is not None:
            active_snapshot = str(active.get("normalized_snapshot") or "")
            if active_snapshot == f"{active_id}.normalized.json":
                try:
                    _read_json(_paths(project_paths)["scenarios"] / active_snapshot)
                except (OSError, ValueError):
                    pass
                else:
                    parent = active
    revision_id = f"rev_{_revision_number(latest['revision_id']) + 1:04d}"
    storage = _paths(project_paths)
    normalized_path = storage["scenarios"] / f"{revision_id}.normalized.json"
    generated = generate_normalization(
        input_path,
        normalized_path,
        memory_path=memory_path,
        use_llm=use_llm,
        hint=hint,
        usage_callback=usage_callback,
    )
    parent_normalized = _read_json(storage["scenarios"] / str(parent["normalized_snapshot"]))
    old_blocks = list(parent_normalized.get("blocks", []))
    new_blocks = list(generated.get("blocks", []))
    _restore_exact_auto_classifications(old_blocks, new_blocks)
    diff = compare_blocks(old_blocks, new_blocks)
    _inherit_blocks(old_blocks, new_blocks, diff)
    generated["blocks"] = new_blocks
    generated["source"] = str(input_path)
    generated["scenario_revision_id"] = revision_id
    generated["parent_revision_id"] = parent["revision_id"]
    source_snapshot = storage["scenarios"] / f"{revision_id}.source{input_path.suffix.lower()}"
    _snapshot_file(input_path, source_snapshot)
    generated["source"] = str(source_snapshot.resolve())
    atomic_write_json(normalized_path, generated)

    prepared_snapshot, voiceover_manifest_snapshot = create_revision_asset_snapshots(
        revision_id, generated, project_paths
    )

    diff_id = f"diff_{parent['revision_id']}_{revision_id}"
    plan = _execution_plan(diff, old_blocks, new_blocks)
    diff_document = {
        "schema_version": 1,
        "diff_id": diff_id,
        "project": project,
        "from_revision_id": parent["revision_id"],
        "to_revision_id": revision_id,
        "created_at": _utcnow(),
        **diff,
        "execution_plan": plan,
    }
    diff_path = storage["diffs"] / f"{diff_id}.json"
    atomic_write_json(diff_path, diff_document)
    manifest = {
        "schema_version": 1,
        "revision_id": revision_id,
        "parent_revision_id": parent["revision_id"],
        "project": project,
        "source_file": input_path.name,
        "source_snapshot": source_snapshot.name,
        "source_sha256": source_hash,
        "normalized_snapshot": normalized_path.name,
        "diff_id": diff_id,
        "diff_file": diff_path.name,
        "created_at": _utcnow(),
        "status": "draft",
        "block_count": len(new_blocks),
        "prepared_snapshot": prepared_snapshot,
        "voiceover_manifest_snapshot": voiceover_manifest_snapshot,
        "summary": diff["summary"],
        "execution_plan": plan,
    }
    atomic_write_json(storage["scenarios"] / f"{revision_id}.json", manifest)
    return {"revision": manifest, "diff": diff_document, "baseline": baseline}


def _activation_matches(
    revision_id: str,
    manifest: dict[str, Any],
    diff: dict[str, Any],
    project_paths: dict[str, Path],
    storage: dict[str, Path],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    reusable: set[str] = set()
    source_matches_path = project_paths["sync_matches"]
    has_own_snapshot = bool(manifest.get("sync_matches_snapshot"))
    if has_own_snapshot:
        snapshot_name = str(manifest["sync_matches_snapshot"])
        if snapshot_name != f"{revision_id}.sync_matches.json":
            raise ValueError("invalid sync matches snapshot")
        source_matches_path = storage["timelines"] / snapshot_name
        if not source_matches_path.is_file():
            raise FileNotFoundError(f"Sync matches snapshot not found: {snapshot_name}")
    elif manifest.get("diff_file"):
        reusable = set(diff.get("execution_plan", {}).get("reusable_block_uids", []))
        parent_id = str(manifest.get("parent_revision_id") or "")
        parent_path = storage["scenarios"] / f"{parent_id}.json"
        if parent_path.is_file():
            parent = _read_json(parent_path)
            if parent.get("sync_matches_snapshot"):
                parent_snapshot = str(parent["sync_matches_snapshot"])
                if parent_snapshot != f"{parent_id}.sync_matches.json":
                    raise ValueError("invalid parent sync matches snapshot")
                source_matches_path = storage["timelines"] / parent_snapshot
                if not source_matches_path.is_file():
                    raise FileNotFoundError(
                        f"Parent sync matches snapshot not found: {parent_snapshot}"
                    )
    matches = (
        _read_json(source_matches_path)
        if source_matches_path.is_file()
        else {"schema_version": 1, "matches": []}
    )
    raw_matches = matches.get("matches", [])
    if not isinstance(raw_matches, list):
        raise ValueError("invalid sync matches snapshot")
    kept = list(raw_matches)
    if manifest.get("diff_file") and not has_own_snapshot:
        kept = []
        for match in raw_matches:
            if not isinstance(match, dict):
                raise ValueError("invalid sync match")
            raw_metadata = match.get("metadata")
            metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
            parent_uid = str(
                metadata.get("parent_block_uid") or match.get("segment_id") or ""
            ).split("::fragment:", 1)[0]
            if parent_uid in reusable:
                kept.append(match)
    result = dict(matches)
    result["matches"] = kept
    return result, kept


@_locked_project_paths(1)
def activate_revision(
    revision_id: str,
    project_paths: dict[str, Path],
    *,
    _resume: bool = False,
) -> dict[str, Any]:
    """Switch the project's mutable work projection to ``revision_id``.

    ``_resume`` is for internal use by ``recover_interrupted_activation``
    only. The intent journal ``restore_revision_assets`` writes before it
    starts committing is only created after the outgoing revision has
    already been snapshotted below, so a journal's presence proves that step
    already completed for the interrupted attempt being resumed. Redoing it
    against a ``prepared.json``/manifest pair that an interrupted attempt may
    have already overwritten with the *incoming* revision's data would
    snapshot the outgoing revision incorrectly, so resuming must skip it.
    """
    storage = _paths(project_paths)
    manifest_path = storage["scenarios"] / f"{revision_id}.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Revision not found: {revision_id}")
    active_path = project_paths["work"] / "active_revision.json"
    if active_path.is_file():
        active_id = str(_read_json(active_path).get("revision_id") or "")
        working = _read_json(project_paths["normalized"])
        working_id = str(working.get("scenario_revision_id") or "")
        if active_id and working_id != active_id:
            raise RevisionStateConflictError(
                "active revision does not match the working scenario; "
                "repair or reconcile the project before switching revisions"
            )
    manifest = _read_json(manifest_path)
    if str(manifest.get("revision_id") or "") != revision_id:
        raise ValueError("revision manifest id mismatch")
    normalized_snapshot = str(manifest.get("normalized_snapshot") or "")
    if normalized_snapshot != f"{revision_id}.normalized.json":
        raise ValueError("invalid normalized snapshot")
    normalized_path = storage["scenarios"] / normalized_snapshot
    if not normalized_path.is_file():
        raise FileNotFoundError(f"Normalized snapshot not found: {normalized_snapshot}")
    normalized = _read_json(normalized_path)
    normalized["scenario_revision_id"] = revision_id
    normalized["parent_revision_id"] = str(manifest.get("parent_revision_id") or "")
    source_snapshot = storage["scenarios"] / str(manifest.get("source_snapshot") or "")
    normalized["source"] = str(source_snapshot.resolve())
    validate_revision_assets(revision_id, manifest, normalized, project_paths)

    # Validate the remaining projections before snapshotting or changing any
    # outgoing state.  After this point activation only performs staged/atomic
    # copies of already checked inputs.
    timeline_snapshot = str(manifest.get("timeline_snapshot") or "")
    if timeline_snapshot:
        if timeline_snapshot != f"{revision_id}.full_timeline.json":
            raise ValueError("invalid timeline snapshot")
        _read_json(storage["timelines"] / timeline_snapshot)
    stage3_mapping_snapshot = str(manifest.get("stage3_mapping_snapshot") or "")
    if stage3_mapping_snapshot:
        if stage3_mapping_snapshot != f"{revision_id}.stage3_mapping.json":
            raise ValueError("invalid Stage 3 mapping snapshot")
        _read_json(storage["timelines"] / stage3_mapping_snapshot)
    diff: dict[str, Any] = {}
    diff_file = str(manifest.get("diff_file") or "")
    if diff_file:
        expected_diff = f"diff_{manifest.get('parent_revision_id')}_{revision_id}.json"
        if diff_file != expected_diff:
            raise ValueError("invalid revision diff")
        diff = _read_json(storage["diffs"] / diff_file)
    matches, kept = _activation_matches(revision_id, manifest, diff, project_paths, storage)
    if active_path.is_file() and not _resume:
        active_id = str(_read_json(active_path).get("revision_id") or "")
        if active_id and active_id != revision_id:
            snapshot_revision_assets(active_id, project_paths)
            manifest = _read_json(manifest_path)
    restore_revision_assets(revision_id, manifest, normalized, project_paths)
    atomic_write_json(project_paths["normalized"], normalized)
    atomic_write_json(project_paths["sync_matches"], matches)
    if manifest.get("timeline_snapshot"):
        _snapshot_file(
            storage["timelines"] / timeline_snapshot,
            project_paths["timeline"],
        )
    else:
        project_paths["timeline"].unlink(missing_ok=True)
    stage3_mapping_snapshot = str(manifest.get("stage3_mapping_snapshot") or "")
    if stage3_mapping_snapshot:
        expected_mapping_snapshot = f"{revision_id}.stage3_mapping.json"
        if stage3_mapping_snapshot != expected_mapping_snapshot:
            raise ValueError("invalid Stage 3 mapping snapshot")
        _snapshot_file(
            storage["timelines"] / stage3_mapping_snapshot,
            project_paths["work"] / "stage3_source_mapping.json",
        )
    else:
        # A mapping from another revision could point export at its XML even
        # though the active timeline was restored correctly.
        (project_paths["work"] / "stage3_source_mapping.json").unlink(missing_ok=True)
    atomic_write_json(
        project_paths["work"] / "active_revision.json",
        {"revision_id": revision_id, "activated_at": _utcnow()},
    )
    return {"revision": manifest, "reused_sync_matches": len(kept)}


@_locked_project_paths(1)
def reconcile_revision_state(
    revision_id: str,
    project_paths: dict[str, Path],
) -> dict[str, Any]:
    """Back up a divergent work projection and restore a chosen known revision."""

    manifest_path = project_paths["scenario_versions"] / f"{revision_id}.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Revision not found: {revision_id}")
    integrity = inspect_revision_state(project_paths)
    if not integrity.get("blocking"):
        return activate_revision(revision_id, project_paths)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_root = project_paths["versions"] / "recovery" / stamp
    work_backup = backup_root / "work"
    speaker_backup = backup_root / "speaker"
    work_backup.mkdir(parents=True, exist_ok=False)
    speaker_backup.mkdir(parents=True, exist_ok=True)
    backup_files = (
        project_paths["normalized"],
        project_paths["prepared"],
        project_paths["sync_matches"],
        project_paths["timeline"],
        project_paths["work"] / "stage3_source_mapping.json",
        project_paths["active_voiceover_manifest"],
        project_paths["work"] / "active_revision.json",
    )
    for source in backup_files:
        if source.is_file():
            _snapshot_file(source, work_backup / source.name)
    for pattern in ("zk*.wav", "zk*.wav.meta"):
        for source in project_paths["speaker"].glob(pattern):
            if source.is_file():
                _snapshot_file(source, speaker_backup / source.name)

    active_path = project_paths["work"] / "active_revision.json"
    previous_active = _read_json(active_path) if active_path.is_file() else None
    atomic_write_json(
        active_path,
        {
            "revision_id": "",
            "recovery_backup": str(backup_root.relative_to(project_paths["root"])),
        },
    )
    try:
        result = activate_revision(revision_id, project_paths)
    except Exception:
        if previous_active is None:
            active_path.unlink(missing_ok=True)
        else:
            atomic_write_json(active_path, previous_active)
        raise
    result["recovery_backup"] = str(backup_root.relative_to(project_paths["root"]))
    result["reconciled"] = True
    return result
