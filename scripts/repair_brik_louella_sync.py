#!/usr/bin/env python3
"""Correct BRIK block 276's stale speaker and re-align its four fragments.

Run with PYTHONPATH=src:scripts. The default is a read-only preview; --apply
backs up the current revision and publishes the corrected normalization,
matches, and voiceover projection.
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from atomic_io import atomic_write_json
from project_service import project_paths
from stage1.normalizer import confirm_normalization, save_normalization, validate_for_confirmation
from stage2.sync_matcher import SyncMatcher
from stage2.sync_models import AlignmentRequest, SyncMatch, SyncSource, WordIndex
from stage4.revision_assets import restore_revision_assets
from stage4.versioning import inspect_revision_state

from repair_brik_revision import backup, read_json


BLOCK_UID = "block-276-455f669b"
LOUELLA_SOURCE_IDS = {"media_3f4896a800e6a2afce09", "media_c3677597eadf1d8cfa4e"}
EXPECTED_SOURCE_ID = "media_c3677597eadf1d8cfa4e"


def plan(root: Path) -> tuple[dict, list[SyncMatch]]:
    from api import _sync_alignment_units

    paths = project_paths("BRIK", root.parent)
    current = read_json(paths["normalized"])
    if root.name != "BRIK" or current.get("scenario_revision_id") != "rev_0005":
        raise ValueError("Expected BRIK scenario rev_0005")
    if current.get("status") != "confirmed" or current.get("revision") != 14:
        raise ValueError("Expected confirmed normalization revision 14")
    for path in root.glob("work/*job_status.json"):
        if read_json(path).get("status") in {"queued", "running", "cancelling"}:
            raise RuntimeError(f"Active job prevents repair: {path.name}")

    block = next(item for item in current["blocks"] if item["uid"] == BLOCK_UID)
    if block["order"] != 276 or block["type"] != "sync" or block["speaker"] != "Шпагин":
        raise ValueError("Block 276 has changed since the audit")
    block.update(speaker="Луэлла", source="manual", confidence=1.0, warnings=[])
    errors = validate_for_confirmation(current)
    if errors:
        raise ValueError("Normalization fails confirmation: " + "; ".join(errors))

    source_data = read_json(paths["sync_sources"])["sources"]
    sources = [
        SyncSource.from_dict(item) for item in source_data if item["id"] in LOUELLA_SOURCE_IDS
    ]
    if {item.id for item in sources} != LOUELLA_SOURCE_IDS:
        raise ValueError("Missing Louella sync sources")
    indexes = [
        WordIndex.from_dict(read_json(paths["sync_transcripts"] / f"{source.id}.words.json"))
        for source in sources
    ]
    matcher = SyncMatcher(sources, indexes)
    units = _sync_alignment_units([block])
    if len(units) != 4:
        raise ValueError(f"Expected four quote fragments, found {len(units)}")
    matches = [
        matcher.match(
            AlignmentRequest(
                segment_id=str(unit["segment_id"]),
                text=str(unit["text"]),
                speaker=str(unit["speaker"]),
                approximate_timecode=unit["timecode_start"],
            )
        )
        for unit in units
    ]
    if any(
        match.status != "auto_matched" or match.source_id != EXPECTED_SOURCE_ID for match in matches
    ):
        raise ValueError("Louella quote did not align safely to the expected source")
    for index, match in enumerate(matches):
        match.metadata.update(
            normalization_revision=16,
            scenario_revision_id="rev_0005",
            parent_block_uid=BLOCK_UID,
            fragment_index=index,
            fragment_count=4,
        )
    return current, matches


def apply(root: Path, candidate: dict, replacements: list[SyncMatch]) -> dict:
    paths = project_paths("BRIK", root.parent)
    old_manifest = read_json(paths["active_voiceover_manifest"])
    old_matches = read_json(paths["sync_matches"])
    retained = [
        match for match in old_matches["matches"] if not match["segment_id"].startswith(BLOCK_UID)
    ]
    if len(retained) + 4 != len(old_matches["matches"]):
        raise ValueError("Expected exactly four old block 276 matches")
    if any(match.get("metadata", {}).get("normalization_revision") != 14 for match in retained):
        raise ValueError("Existing matches belong to a different normalization revision")

    saved = save_normalization(paths["normalized"], candidate, expected_revision=14)
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
    if confirmed["revision"] != 16 or len(prepared["items"]) != len(old_manifest["items"]):
        raise RuntimeError("Unexpected voiceover projection after speaker correction")
    for match in retained:
        match["metadata"]["normalization_revision"] = 16
    new_matches = retained + [match.to_dict() for match in replacements]
    atomic_write_json(paths["sync_matches"], {"schema_version": 1, "matches": new_matches})

    counts = Counter(match["status"] for match in new_matches)
    job = read_json(paths["sync_job_status"])
    job.update(
        total_segments=len(new_matches),
        total=len(new_matches),
        done=len(new_matches),
        matched=counts["auto_matched"] + counts["confirmed"],
        review=counts["needs_review"],
        unmatched=counts["not_found"],
        auto_matched=counts["auto_matched"],
        confirmed=counts["confirmed"],
        needs_review=counts["needs_review"],
        not_found=counts["not_found"],
        normalization_revision=16,
        message="BRIK: block 276 aligned to Louella; reviewed ranges retained",
    )
    atomic_write_json(paths["sync_job_status"], job)

    scenario_versions = paths["scenario_versions"]
    atomic_write_json(scenario_versions / "rev_0005.normalized.json", confirmed)
    atomic_write_json(scenario_versions / "rev_0005.prepared.json", prepared)
    atomic_write_json(scenario_versions / "rev_0005.voiceover.json", old_manifest)
    revision = read_json(scenario_versions / "rev_0005.json")
    restore_revision_assets("rev_0005", revision, confirmed, paths)
    integrity = inspect_revision_state(paths)
    if integrity.get("blocking"):
        raise RuntimeError(f"BRIK revision integrity is blocked: {integrity}")
    return {
        "normalization_revision": 16,
        "matches": len(new_matches),
        "sync": dict(counts),
        "integrity": integrity["status"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path("/app/projects/BRIK"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = args.project_root.resolve()
    candidate, matches = plan(root)
    for match in matches:
        print(
            match.segment_id,
            match.status,
            match.confidence,
            match.source_in_sec,
            match.source_out_sec,
        )
    if args.apply:
        print(f"Backup: {backup(root)}")
        print(apply(root, candidate, matches))


if __name__ == "__main__":
    main()
