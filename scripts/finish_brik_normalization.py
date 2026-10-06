#!/usr/bin/env python3
"""Finish BRIK's spoken-text cleanup while retaining reviewed sync matches.

Run with PYTHONPATH=src:scripts. The default prints a plan; --apply backs up
the active revision, regenerates changed speech, and republishes its assets.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from atomic_io import atomic_write_json
from project_service import project_paths
from stage1.gemini_tts import synthesize_tts
from stage1.normalizer import (
    clean_voiceover_tts_text,
    confirm_normalization,
    save_normalization,
    validate_for_confirmation,
)
from stage4.revision_assets import (
    prepared_from_normalized,
    restore_revision_assets,
    save_tts_manifest,
)
from stage4.versioning import inspect_revision_state

from finish_brik_tts import MODEL, STYLE, _set_type, update_tts_status
from repair_brik_revision import backup, read_json


FEMALE_ORDERS = {36, 194, 196, 284}
TECHNICAL_ORDERS = {103, 233, 291, 292, 293}


def plan(root: Path) -> tuple[dict, list[str], dict[str, str]]:
    paths = project_paths("BRIK", root.parent)
    current = read_json(paths["normalized"])
    if root.name != "BRIK" or current.get("scenario_revision_id") != "rev_0005":
        raise ValueError("Expected BRIK scenario rev_0005")
    if current.get("status") != "confirmed" or current.get("revision") != 12:
        raise ValueError("Expected confirmed normalization revision 12")
    for path in root.glob("work/*job_status.json"):
        if read_json(path).get("status") in {"queued", "running", "cancelling"}:
            raise RuntimeError(f"Active job prevents repair: {path.name}")

    blocks = {block["order"]: block for block in current["blocks"]}
    changes: list[str] = []
    regenerate: dict[str, str] = {}
    for order, block in blocks.items():
        if block["type"] != "voiceover" or order in TECHNICAL_ORDERS:
            continue
        cleaned = clean_voiceover_tts_text(block["text_for_tts"])
        if cleaned != block["text_for_tts"]:
            block["text_for_tts"] = cleaned
            regenerate[block["uid"]] = "female" if order in FEMALE_ORDERS else "male"
            changes.append(f"{order}: strip speech direction from TTS")

    diary = blocks[98]
    diary["text_for_tts"] = (
        f"{diary['text_for_tts'].rstrip()}\n{blocks[103]['original_text'].strip()}"
    )
    diary["normalized_text"] = diary["text_for_tts"]
    regenerate[diary["uid"]] = "male"
    changes.append("98+103: merge last diary line into one reading")

    poem = "\n".join(blocks[order]["original_text"].strip() for order in range(290, 294))
    poem = re.sub(r"^Черкасов\s+читает\s*:\s*", "", poem, flags=re.I).strip()
    poem = poem.replace("« ", "«").replace("« Партия", "«Партия")
    _set_type(blocks[290], "voiceover", poem)
    regenerate[blocks[290]["uid"]] = "male"
    changes.append("290-293: merge Mayakovsky poem into one TTS reading")

    for order in sorted(TECHNICAL_ORDERS):
        _set_type(blocks[order], "technical")
        changes.append(f"{order}: spoken continuation/editorial cue -> technical")

    errors = validate_for_confirmation(current)
    if errors:
        raise ValueError("Corrections fail confirmation: " + "; ".join(errors))
    return current, changes, regenerate


def _preserve_matches(paths: dict[str, Path], confirmed: dict) -> dict[str, int]:
    # Import lazily: api initializes the Flask app and its configured project root.
    from api import _sync_alignment_units

    payload = read_json(paths["sync_matches"])
    old_revision = 12
    current = [
        item
        for item in payload["matches"]
        if item.get("metadata", {}).get("normalization_revision") == old_revision
        and item.get("metadata", {}).get("scenario_revision_id") == "rev_0005"
    ]
    confirmed_uids = {item["segment_id"] for item in current if item["status"] == "confirmed"}
    units = _sync_alignment_units(
        [block for block in confirmed["blocks"] if block["type"] in {"sync", "host"}],
        preserve_whole_uids=confirmed_uids,
    )
    valid_ids = {str(unit["segment_id"]) for unit in units}
    retained = [item for item in current if item["segment_id"] in valid_ids]
    if len(retained) != len(valid_ids):
        missing = valid_ids - {item["segment_id"] for item in retained}
        raise ValueError(f"Missing existing match records: {sorted(missing)[:8]}")
    for item in retained:
        item["metadata"]["normalization_revision"] = confirmed["revision"]
    atomic_write_json(paths["sync_matches"], {"schema_version": 1, "matches": retained})

    counts = Counter(item["status"] for item in retained)
    job = read_json(paths["sync_job_status"])
    job.update(
        total_segments=len(retained),
        total=len(retained),
        done=len(retained),
        matched=counts["auto_matched"] + counts["confirmed"],
        review=counts["needs_review"],
        unmatched=counts["not_found"],
        auto_matched=counts["auto_matched"],
        confirmed=counts["confirmed"],
        needs_review=counts["needs_review"],
        not_found=counts["not_found"],
        normalization_revision=confirmed["revision"],
        message="BRIK: reviewed sync ranges retained after spoken-text cleanup",
    )
    atomic_write_json(paths["sync_job_status"], job)
    return dict(counts)


def apply(root: Path, candidate: dict, regenerate: dict[str, str]) -> dict:
    paths = project_paths("BRIK", root.parent)
    old_manifest = read_json(paths["active_voiceover_manifest"])
    old_by_uid = {item["block_uid"]: item for item in old_manifest["items"]}
    planned_prepared = prepared_from_normalized(candidate)
    speaker_dir = root / "speaker/revisions/rev_0005"
    generated_assets: dict[str, dict] = {}
    with TemporaryDirectory() as temporary:
        temp = Path(temporary)
        for gender in ("male", "female"):
            subset = [
                item
                for item in planned_prepared["items"]
                if regenerate.get(item["block_uid"]) == gender
            ]
            if not subset:
                continue
            subset_path = temp / f"{gender}.json"
            atomic_write_json(subset_path, {"scenario_revision_id": "rev_0005", "items": subset})
            result = synthesize_tts(
                subset_path,
                speaker_dir,
                provider="openrouter",
                gender=gender,
                tts_model=MODEL,
                tts_style=STYLE,
            )
            if result["files_count"] + result["skipped_count"] != len(subset):
                raise RuntimeError(f"Incomplete {gender} TTS generation: {result}")
            for item in result["voiceover_assets"]:
                generated_assets[item["block_uid"]] = {
                    **item,
                    "asset": Path(item["asset"]).relative_to(root).as_posix(),
                }

    saved = save_normalization(paths["normalized"], candidate, expected_revision=12)
    source_override = None
    if not Path(saved["source"]).is_file():
        source_override = paths["scenario_versions"] / "rev_0005.source.docx"
    confirmed = confirm_normalization(
        paths["normalized"],
        paths["prepared"],
        paths["raw"],
        expected_revision=saved["revision"],
        source_path_override=source_override,
    )
    prepared = read_json(paths["prepared"])
    if [item["block_uid"] for item in prepared["items"]] != [
        item["block_uid"] for item in planned_prepared["items"]
    ]:
        raise RuntimeError("Prepared voiceover order changed after TTS generation")

    items = []
    for item in prepared["items"]:
        uid = item["block_uid"]
        if uid in regenerate:
            asset = generated_assets[uid]
        else:
            asset = old_by_uid.get(uid)
            if asset is None:
                raise ValueError(f"No reusable WAV for {uid}")
        items.append({**asset, "id": item["id"]})
    manifest = {"schema_version": 1, "scenario_revision_id": "rev_0005", "items": items}
    atomic_write_json(paths["active_voiceover_manifest"], manifest)
    scenario_versions = paths["scenario_versions"]
    atomic_write_json(scenario_versions / "rev_0005.normalized.json", confirmed)
    atomic_write_json(scenario_versions / "rev_0005.prepared.json", prepared)
    atomic_write_json(scenario_versions / "rev_0005.voiceover.json", manifest)
    revision = read_json(scenario_versions / "rev_0005.json")
    restore_revision_assets("rev_0005", revision, confirmed, paths)
    save_tts_manifest(
        paths,
        {
            "scenario_revision_id": "rev_0005",
            "voiceover_assets": [{**item, "asset": str(root / item["asset"])} for item in items],
        },
    )
    update_tts_status(root, manifest, len(generated_assets))
    sync_counts = _preserve_matches(paths, confirmed)
    integrity = inspect_revision_state(paths)
    if integrity.get("blocking"):
        raise RuntimeError(f"BRIK revision integrity is blocked: {integrity}")
    return {
        "normalization_revision": confirmed["revision"],
        "voiceovers": len(items),
        "regenerated": len(generated_assets),
        "sync": sync_counts,
        "integrity": integrity["status"],
        "finished_at": datetime.now(UTC).isoformat(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path("/app/projects/BRIK"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = args.project_root.resolve()
    candidate, changes, regenerate = plan(root)
    print("\n".join(changes))
    print(f"Planned changes: {len(changes)}, TTS regeneration: {len(regenerate)}")
    if args.apply:
        print(f"Backup: {backup(root)}")
        print(apply(root, candidate, regenerate))


if __name__ == "__main__":
    main()
