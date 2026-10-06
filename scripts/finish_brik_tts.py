#!/usr/bin/env python3
"""Finish the audited BRIK revision: actor readings, verse, and misplaced cues.

Run inside the application container. The default prints the planned edits.
``--apply`` backs up the current projection, confirms the corrected blocks,
generates the new actor audio, and republishes the revision assets.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from atomic_io import atomic_write_json
from project_service import project_paths
from stage1.gemini_tts import synthesize_tts
from stage1.normalizer import confirm_normalization, save_normalization, validate_for_confirmation
from stage4.revision_assets import restore_revision_assets, save_tts_manifest
from stage4.versioning import inspect_revision_state

from repair_brik_revision import backup, read_json


MALE_READINGS = {63, 98, 108, 119, 146, 156, 165, 205, 247}
FEMALE_READINGS = {78, 94, 130, 134, 153, 199, 315}
MERGE_RANGES = {98: 102, 146: 147, 165: 167, 205: 217, 247: 268}
DIRECT_READINGS = {78, 108, 130, 153, 156}
EDITORIAL = {143, 202}
INTERVIEWS = {278, 279}
STYLE = "историческое документальное кино"
MODEL = "google/gemini-3.1-flash-tts-preview"


def _spoken_text(block: dict) -> str:
    text = str(block.get("normalized_text") or block["original_text"]).strip()
    text = re.sub(r"^(?:ЗК\.?\s*)?Голос\s+[«\"]?(?:Лили|Маяковского)[»\"]?\s*:\s*", "", text, flags=re.I)
    if block["order"] == 119:
        text = "Кто там шагает правой — левой, левой, левой!"
    if block["order"] == 205:
        text = text.lstrip(".")
    return text.strip()


def _set_type(block: dict, kind: str, text: str = "") -> None:
    block.update(
        type=kind,
        normalized_text=text if kind == "voiceover" else block["original_text"],
        text_for_tts=text if kind == "voiceover" else "",
        original_label="",
        label_confirmed=True,
        source="manual",
        confidence=1.0,
        warnings=[],
        speaker="",
        role="",
        source_description="",
        timecode_start=None,
        timecode_end=None,
        sync_fragments=[],
    )


def plan(root: Path) -> tuple[dict, list[str], set[str]]:
    paths = project_paths("BRIK", root.parent)
    current = read_json(paths["normalized"])
    if root.name != "BRIK" or current.get("scenario_revision_id") != "rev_0005":
        raise ValueError("Expected active BRIK revision rev_0005")
    if int(current.get("revision") or 0) != 10 or current.get("status") != "confirmed":
        raise ValueError("Expected confirmed audited BRIK normalization revision 10")
    for path in root.glob("work/*job_status.json"):
        if read_json(path).get("status") in {"queued", "running", "cancelling"}:
            raise RuntimeError(f"Active job prevents repair: {path.name}")

    blocks = {block["order"]: block for block in current["blocks"]}
    changed: list[str] = []
    regenerate: set[str] = set()
    for order in sorted(EDITORIAL):
        _set_type(blocks[order], "technical")
        changed.append(f"{order}: editorial cue -> technical")
    for order in sorted(INTERVIEWS):
        block = blocks[order]
        credit, content = block["original_text"].split(":", 1)
        speaker = credit.split(",", 1)[0].strip()
        block.update(
            type="sync", normalized_text=content.strip(), text_for_tts="",
            original_label=credit.strip(), label_confirmed=True,
            source="manual", confidence=1.0, warnings=[],
            speaker=speaker, role=credit.split(",", 1)[1].strip() if "," in credit else "",
            source_description="", timecode_start="", timecode_end="",
            sync_fragments=[{"text": content.strip(), "timecode_start": "",
                             "timecode_end": ""}],
        )
        changed.append(f"{order}: interview -> sync ({speaker})")
    for order in sorted(MALE_READINGS | FEMALE_READINGS):
        block = blocks[order]
        if order in MERGE_RANGES:
            text = "\n".join(_spoken_text(blocks[index]) for index in range(order, MERGE_RANGES[order] + 1))
        elif order in DIRECT_READINGS:
            text = block["original_text"].split(":", 1)[1].strip() if ":" in block["original_text"] else _spoken_text(block)
        else:
            text = _spoken_text(block)
        if order == 63:
            text = block["original_text"].strip()
        _set_type(block, "voiceover", text)
        regenerate.add(block["uid"])
        changed.append(f"{order}: actor reading -> voiceover ({len(text)} chars)")
    for start, end in MERGE_RANGES.items():
        for order in range(start + 1, end + 1):
            _set_type(blocks[order], "technical")
            changed.append(f"{order}: continuation merged into {start}")
    # The line 62 is a production choice, not speech: line 63 is its text.
    _set_type(blocks[62], "technical")
    changed.append("62: actor direction -> technical")
    errors = validate_for_confirmation(current)
    if errors:
        raise ValueError("Corrections fail confirmation: " + "; ".join(errors))
    return current, changed, regenerate


def apply(root: Path, candidate: dict, regenerate: set[str]) -> dict:
    paths = project_paths("BRIK", root.parent)
    old_manifest = read_json(paths["active_voiceover_manifest"])
    old_by_uid = {item["block_uid"]: item for item in old_manifest["items"]}
    saved = save_normalization(paths["normalized"], candidate, expected_revision=10)
    confirmed = confirm_normalization(
        paths["normalized"], paths["prepared"], paths["raw"],
        expected_revision=int(saved["revision"]),
    )
    prepared = read_json(paths["prepared"])
    speaker_dir = root / "speaker/revisions/rev_0005"
    items: list[dict] = []
    generated = 0
    with TemporaryDirectory() as temporary:
        temp = Path(temporary)
        for gender, selected in (("male", MALE_READINGS), ("female", FEMALE_READINGS)):
            subset = [item for item in prepared["items"] if item["block_uid"] in regenerate
                      and next(block["order"] for block in candidate["blocks"]
                               if block["uid"] == item["block_uid"]) in selected]
            if not subset:
                continue
            subset_path = temp / f"{gender}.json"
            atomic_write_json(subset_path, {"scenario_revision_id": "rev_0005", "items": subset})
            result = synthesize_tts(
                subset_path, speaker_dir, provider="openrouter", gender=gender,
                tts_model=MODEL, tts_style=STYLE,
            )
            generated += result["files_count"]
            for item in result["voiceover_assets"]:
                asset = Path(item["asset"])
                items.append({**item, "asset": asset.relative_to(root).as_posix()})
        # Reuse every unchanged UID and remap the positional alias to the new ID.
        for item in prepared["items"]:
            if item["block_uid"] in regenerate:
                continue
            old = old_by_uid.get(item["block_uid"])
            if old is None:
                raise ValueError(f"No reusable WAV for {item['block_uid']}")
            items.append({**old, "id": item["id"]})
    if len(items) != len(prepared["items"]):
        raise ValueError("Voiceover asset count does not match prepared projection")
    by_uid = {item["block_uid"]: item for item in items}
    manifest = {"schema_version": 1, "scenario_revision_id": "rev_0005",
                "items": [by_uid[item["block_uid"]] for item in prepared["items"]]}
    atomic_write_json(paths["active_voiceover_manifest"], manifest)
    scenario_versions = paths["scenario_versions"]
    atomic_write_json(scenario_versions / "rev_0005.normalized.json", confirmed)
    atomic_write_json(scenario_versions / "rev_0005.prepared.json", prepared)
    atomic_write_json(scenario_versions / "rev_0005.voiceover.json", manifest)
    revision = read_json(scenario_versions / "rev_0005.json")
    restore_revision_assets("rev_0005", revision, confirmed, paths)
    save_tts_manifest(paths, {"scenario_revision_id": "rev_0005", "voiceover_assets": [
        {**item, "asset": str(root / item["asset"])} for item in manifest["items"]
    ]})
    update_tts_status(root, manifest, generated)
    integrity = inspect_revision_state(paths)
    if integrity.get("blocking"):
        raise RuntimeError(f"TTS repair left blocking integrity error: {integrity}")
    return {"normalization_revision": confirmed["revision"],
            "voiceovers": len(items), "generated": generated,
            "integrity": integrity["status"]}


def update_tts_status(root: Path, manifest: dict, generated: int) -> None:
    """Align the visible job summary with the repaired active projection."""
    paths = project_paths("BRIK", root.parent)
    status = read_json(paths["tts_job_status"])
    count = len(manifest["items"])
    status.update(
        status="completed", event="completed", total=count, done=count,
        generated=generated, skipped=count - generated,
        message=f"BRIK: {generated} actor readings synthesized; {count - generated} WAV assets reused",
        speaker_dir=str(root / "speaker/revisions/rev_0005"),
        gender="", resolved_voice="Charon/Aoede",
        finished_at=datetime.now(UTC).isoformat(),
    )
    status["result"] = {
        "prepared_path": str(paths["prepared"]),
        "speaker_dir": str(root / "speaker/revisions/rev_0005"),
        "items_total": count,
        "files_count": generated,
        "skipped_count": count - generated,
        "provider": "openrouter",
        "model": MODEL,
        "style": STYLE,
        "voice": "Charon/Aoede",
        "gender": "mixed",
        "dry_run": False,
        "generated_at": datetime.now(UTC).isoformat(),
        "scenario_revision_id": "rev_0005",
        "voiceover_assets": [
            {**item, "asset": str(root / item["asset"])} for item in manifest["items"]
        ],
    }
    atomic_write_json(paths["tts_job_status"], status)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path("/app/projects/BRIK"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = args.project_root.resolve()
    candidate, changes, regenerate = plan(root)
    print("\n".join(changes))
    print(f"Planned changes: {len(changes)}, generated voiceovers: {len(regenerate)}")
    if args.apply:
        print(f"Backup: {backup(root)}")
        print(apply(root, candidate, regenerate))


if __name__ == "__main__":
    main()
