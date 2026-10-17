#!/usr/bin/env python3
"""Apply the reviewed BRIK normalization corrections without regenerating audio.

Run inside the application container so the normalized source's /app path is
available. The default mode prints a plan; --apply backs up runtime artifacts
and atomically republishes the active revision's voiceover projection.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

from atomic_io import atomic_write_json
from project_service import project_paths
from stage1.normalizer import (
    confirm_normalization,
    generate_normalization,
    save_normalization,
    validate_for_confirmation,
)
from stage4.revision_assets import restore_revision_assets, snapshot_revision_assets
from stage4.versioning import inspect_revision_state


REPARSE_SYNC_UIDS = {
    "block-24-0aaccea2",  # Vera Storozheva credit parsed as "То"
    "block-126-261389aa",  # 50-60ые years parsed as a timecode
    "block-239-33d91684",  # splice punctuation
    "block-281-87104254",  # splice punctuation
    "block-307-3536223e",  # splice punctuation
}
FILM_FRAGMENT_UID = "block-231-3e90997e"
REPLACED_FIELDS = (
    "type", "normalized_text", "text_for_tts", "original_label", "label_confirmed",
    "source", "confidence", "warnings", "speaker", "role", "source_description",
    "timecode_start", "timecode_end", "sync_fragments", "notes", "continuation_of_type",
)
BACKUP_FILES = (
    "work/scenario_normalized.json",
    "work/scenario_prepared.json",
    "work/voiceover_manifest.json",
    "work/sync_sources.json",
    "work/sync_matches.json",
    "work/sync_job_status.json",
    "work/tts_job_status.json",
    "versions/scenarios/rev_0005.json",
    "versions/scenarios/rev_0005.normalized.json",
    "versions/scenarios/rev_0005.prepared.json",
    "versions/scenarios/rev_0005.voiceover.json",
)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def plan(root: Path) -> tuple[dict, list[str]]:
    paths = project_paths("BRIK", root.parent)
    current = read_json(paths["normalized"])
    if root.name != "BRIK" or current.get("scenario_revision_id") != "rev_0005":
        raise ValueError("Expected active BRIK revision rev_0005")
    if current.get("status") != "confirmed":
        raise ValueError("Expected confirmed normalization")
    if int(current.get("revision") or 0) != 8:
        raise ValueError("BRIK normalization changed since the audited revision 8")
    for path in root.glob("work/*job_status.json"):
        if read_json(path).get("status") in {"queued", "running", "cancelling"}:
            raise RuntimeError(f"Active job prevents repair: {path.name}")

    source = Path(current["source"])
    if not source.is_file():
        source = root / "versions/scenarios/rev_0005.source.docx"
    with TemporaryDirectory() as temporary:
        temp = Path(temporary)
        generated = generate_normalization(
            source, temp / "normalized.json", memory_path=temp / "memory.sqlite3", use_llm=False
        )
    old_blocks = current["blocks"]
    new_blocks = generated["blocks"]
    if len(old_blocks) != len(new_blocks):
        raise ValueError("Source block count changed")

    changed: list[str] = []
    for old, new in zip(old_blocks, new_blocks):
        if old["order"] != new["order"] or old["original_text"] != new["original_text"]:
            raise ValueError(f"Source changed at block {old['order']}")
        update = (
            (old["type"] == "voiceover" and new["type"] in {"sync", "technical"})
            or (old["type"] == "sync" and new["type"] == "technical")
            or old["uid"] in REPARSE_SYNC_UIDS
        )
        if update:
            for field in REPLACED_FIELDS:
                old[field] = new[field]
            changed.append(f"{old['order']}: {old['uid']} -> {old['type']}")
        if old["uid"] == FILM_FRAGMENT_UID:
            old.update(
                type="life",
                normalized_text=old["original_text"],
                text_for_tts="",
                original_label="ФРАГМЕНТ ФИЛЬМА",
                speaker="",
                role="",
                source_description="Фрагмент фильма «Стеклянный глаз»",
                timecode_start="38:42",
                timecode_end="38:55",
                sync_fragments=[],
                source="manual",
                confidence=1.0,
                warnings=[],
            )
            changed.append(f"{old['order']}: {old['uid']} -> life")

    errors = validate_for_confirmation(current)
    if errors:
        raise ValueError("Corrections still fail confirmation: " + "; ".join(errors))
    return current, changed


def backup(root: Path) -> Path:
    destination = root / "work" / f"repair_backup_{datetime.now(UTC):%Y%m%d_%H%M%S}"
    destination.mkdir(parents=True)
    for relative in BACKUP_FILES:
        source = root / relative
        if source.is_file():
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    shutil.copytree(root / "speaker/revisions/rev_0005", destination / "rev_0005")
    return destination


def apply(root: Path, candidate: dict) -> dict:
    paths = project_paths("BRIK", root.parent)
    old_manifest = read_json(paths["active_voiceover_manifest"])
    assets_by_uid = {item["block_uid"]: item for item in old_manifest["items"]}
    saved = save_normalization(
        paths["normalized"], candidate, expected_revision=int(candidate["revision"])
    )
    confirmed = confirm_normalization(
        paths["normalized"], paths["prepared"], paths["raw"],
        expected_revision=int(saved["revision"]),
    )
    prepared = read_json(paths["prepared"])
    items = []
    for block in prepared["items"]:
        source = assets_by_uid.get(block["block_uid"])
        if source is None:
            raise ValueError(f"No existing WAV asset for {block['block_uid']}")
        items.append({**source, "id": block["id"]})
    manifest = {
        "schema_version": 1,
        "scenario_revision_id": "rev_0005",
        "items": items,
    }
    scenario_versions = paths["scenario_versions"]
    atomic_write_json(scenario_versions / "rev_0005.normalized.json", confirmed)
    atomic_write_json(scenario_versions / "rev_0005.prepared.json", prepared)
    atomic_write_json(scenario_versions / "rev_0005.voiceover.json", manifest)
    revision = read_json(scenario_versions / "rev_0005.json")
    restore_revision_assets("rev_0005", revision, confirmed, paths)
    snapshot_revision_assets("rev_0005", paths)

    sources = read_json(paths["sync_sources"])
    for source in sources["sources"]:
        if source["id"] == "media_3f4896a800e6a2afce09":
            source["speakers"] = list(dict.fromkeys([
                *source.get("speakers", []), "Луэлла", "Лоэлла"
            ]))
    atomic_write_json(paths["sync_sources"], sources)

    tts_job = read_json(paths["tts_job_status"])
    tts_job.update(
        total=len(items), done=len(items), generated=0, skipped=len(items),
        message=f"Existing WAV assets remapped to {len(items)} active voiceover blocks",
    )
    tts_job["result"] = {
        **tts_job.get("result", {}),
        "items_total": len(items), "files_count": 0, "skipped_count": len(items),
        "scenario_revision_id": "rev_0005",
    }
    atomic_write_json(paths["tts_job_status"], tts_job)

    integrity = inspect_revision_state(paths)
    if integrity.get("blocking"):
        raise RuntimeError(f"Repair left a blocking integrity error: {integrity}")
    return {"normalization_revision": confirmed["revision"], "voiceovers": len(items),
            "integrity": integrity["status"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path("/app/projects/BRIK"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = args.project_root.resolve()
    candidate, changes = plan(root)
    print("\n".join(changes))
    print(f"Planned corrections: {len(changes)}")
    if args.apply:
        location = backup(root)
        print(f"Backup: {location}")
        print(apply(root, candidate))


if __name__ == "__main__":
    main()
