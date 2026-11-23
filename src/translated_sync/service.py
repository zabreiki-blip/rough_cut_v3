"""Project-owned transcript import helpers for translated sync."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile

from atomic_io import atomic_write_json
from .transcript import _seconds, parse_docx_transcript


def transcript_path(paths, source_id):
    # Digest the id, so even legacy source ids cannot escape the project.
    return (
        Path(paths["work"])
        / "translated_sync"
        / "transcripts"
        / (hashlib.sha256(source_id.encode()).hexdigest() + ".json")
    )


def load_transcript(paths, source_id):
    path = transcript_path(paths, source_id)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def import_transcript(
    paths, source, data, mode, sequence_start_tc, source_store, match_store, guide_duration=None
):
    if mode not in ("relative", "display"):
        raise ValueError("invalid timecode mode")
    import re

    value = sequence_start_tc or source.sequence_start_tc
    if not re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2}(?::\d{2})?)?(?:\.\d+)?", value):
        raise ValueError("invalid sequence start timecode")
    offset = _seconds(value, source.fps)
    if offset < 0:
        raise ValueError("invalid sequence start timecode")
    folder = transcript_path(paths, source.id).parent
    folder.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(suffix=".docx", dir=folder)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
        cues = parse_docx_transcript(
            Path(name),
            source.id,
            duration_sec=guide_duration
            if guide_duration is not None
            else source.duration_sec - source.guide_offset_sec,
            fps=source.fps,
            timecode_mode=mode,
            sequence_start_sec=offset + source.guide_offset_sec,
            guide_offset_sec=source.guide_offset_sec,
        )
        digest = hashlib.sha256(data).hexdigest()
        cue_values = [cue.to_dict() for cue in cues]
        cue_hash = hashlib.sha256(
            json.dumps(cue_values, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        previous = load_transcript(paths, source.id)
        summary = {
            "source_id": source.id,
            "document_sha256": digest,
            "cues_sha256": cue_hash,
            "cue_count": len(cues),
            "timecode_mode": mode,
            "sequence_start_tc": sequence_start_tc or source.sequence_start_tc,
        }
        result = {**summary, "cues": cue_values}
        if previous != result:
            document_path = folder / (digest + ".docx")
            if not document_path.exists():
                os.replace(name, document_path)
            old_ids = {c["id"] for c in (previous or {}).get("cues", [])}
            new_ids = {c["id"] for c in cue_values}
            changed = old_ids - new_ids
            matches = match_store.list()
            for match in matches:
                if match.source_id == source.id and (
                    match.metadata.get("document_cue_id") in changed
                    or match.metadata.get("document_cue_unbound")
                ):
                    if match.status == "confirmed":
                        match.metadata["retime_pending_review"] = True
                    else:
                        match.status = "needs_review"
                        match.metadata["retime_pending_review"] = True
            match_store.replace_all(matches)
            atomic_write_json(transcript_path(paths, source.id), result)
            source_store.save(
                replace(
                    source,
                    sequence_start_tc=summary["sequence_start_tc"],
                    metadata={**source.metadata, "translated_transcript": summary},
                )
            )
        return result
    finally:
        Path(name).unlink(missing_ok=True)
