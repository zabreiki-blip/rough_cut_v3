import json
from pathlib import Path

import pytest

from stage2.assembly_manifest import (
    attach_scenario_revision,
    build_assembly_manifest,
    validate_assembly_manifest,
)
from stage2.models import FullTimeline, TimelineSegment


def _files(tmp_path: Path) -> tuple[Path, Path, Path]:
    scenario = tmp_path / "scenario_normalized.json"
    timeline = tmp_path / "full_timeline.json"
    wav = tmp_path / "final_review.wav"
    scenario.write_text(
        json.dumps({"scenario_revision_id": "revision-3"}), encoding="utf-8"
    )
    timeline.write_text('{"timeline": true}', encoding="utf-8")
    wav.write_bytes(b"RIFF" + b"audio" * 20)
    return scenario, timeline, wav


def test_assembly_manifest_links_sequence_wav_and_timeline_blocks(tmp_path: Path) -> None:
    scenario_path, timeline_path, wav_path = _files(tmp_path)
    timeline = FullTimeline(
        project="FRISKE_001",
        segments=[TimelineSegment(
            "snh1", "snh", "реплика", 5.0, block_uid="block-1",
            match_status="confirmed", sequence_id="source-sequence",
            sequence_name="SOURCE", source_in_sec=10.0, source_out_sec=15.0,
        )],
    )
    sequence = {"sequenceId": "target-sequence", "name": "FRISKE_001 final"}
    layout = {
        "sequence": sequence,
        "audio": [{
            "track": "A1",
            "muted": False,
            "clips": [{
                "name": "SOURCE", "startSeconds": 0.0, "endSeconds": 5.0,
                "durationSeconds": 5.0,
                "sourceInSeconds": 10.0, "sourceOutSeconds": 15.0,
                "enabled": True, "hasAudioComponent": True,
            }],
        }],
    }
    manifest = build_assembly_manifest(
        project="FRISKE_001",
        timeline=timeline,
        timeline_path=timeline_path,
        scenario_path=scenario_path,
        audio_path=wav_path,
        source_sequence=sequence,
        layout=layout,
    )
    attach_scenario_revision(manifest, scenario_path)
    assert manifest["scenario_revision_id"] == "revision-3"
    assert manifest["sequence"]["sequenceId"] == "target-sequence"
    assert manifest["blocks"][0]["actual_audio_clip"]["track"] == "A1"
    assert manifest["blocks"][0]["eligible_for_review"] is True
    assert manifest["summary"]["eligible_blocks"] == 1
    assert manifest["findings"] == []
    validate_assembly_manifest(
        manifest,
        timeline_path=timeline_path,
        scenario_path=scenario_path,
        audio_path=wav_path,
    )


def test_assembly_manifest_detects_stale_wav_before_asr(tmp_path: Path) -> None:
    scenario_path, timeline_path, wav_path = _files(tmp_path)
    timeline = FullTimeline(project="FRISKE_001")
    sequence = {"sequenceId": "target-sequence", "name": "FRISKE_001 final"}
    manifest = build_assembly_manifest(
        project="FRISKE_001",
        timeline=timeline,
        timeline_path=timeline_path,
        scenario_path=scenario_path,
        audio_path=wav_path,
        source_sequence=sequence,
        layout={"sequence": sequence, "audio": []},
    )
    wav_path.write_bytes(wav_path.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="stale_snapshot: WAV"):
        validate_assembly_manifest(
            manifest,
            timeline_path=timeline_path,
            scenario_path=scenario_path,
            audio_path=wav_path,
        )


def test_assembly_manifest_accepts_premiere_source_prefix(tmp_path: Path) -> None:
    scenario_path, timeline_path, wav_path = _files(tmp_path)
    timeline = FullTimeline(
        project="FRISKE_001",
        segments=[TimelineSegment(
            "snh1", "snh", "реплика", 5.0, block_uid="block-1",
            match_status="confirmed", sequence_name="Source Name",
            source_in_sec=10.0, source_out_sec=15.0,
        )],
    )
    sequence = {"sequenceId": "target", "name": "Final"}
    manifest = build_assembly_manifest(
        project="FRISKE_001", timeline=timeline,
        timeline_path=timeline_path, scenario_path=scenario_path,
        audio_path=wav_path, source_sequence=sequence,
        layout={
            "sequence": sequence,
            "audio": [{"track": "A1", "muted": False, "clips": [{
                "name": "RC_SRC_SOURCE_NAME", "projectItemName": "RC_SRC_SOURCE_NAME",
                "startSeconds": 0.0, "endSeconds": 5.0,
                "durationSeconds": 5.0, "sourceInSeconds": 10.0,
                "sourceOutSeconds": 15.0, "enabled": True,
                "hasAudioComponent": True,
            }]}],
        },
    )
    assert manifest["findings"] == []


def test_assembly_manifest_rejects_layout_from_another_sequence(tmp_path: Path) -> None:
    scenario_path, timeline_path, wav_path = _files(tmp_path)
    with pytest.raises(ValueError, match="does not belong"):
        build_assembly_manifest(
            project="FRISKE_001",
            timeline=FullTimeline(project="FRISKE_001"),
            timeline_path=timeline_path,
            scenario_path=scenario_path,
            audio_path=wav_path,
            source_sequence={"sequenceId": "one", "name": "Final"},
            layout={"sequence": {"sequenceId": "two", "name": "Final"}},
        )


def test_assembly_manifest_reports_wrong_or_muted_source_range(tmp_path: Path) -> None:
    scenario_path, timeline_path, wav_path = _files(tmp_path)
    timeline = FullTimeline(
        project="FRISKE_001",
        segments=[TimelineSegment(
            "snh1", "snh", "реплика", 5.0, block_uid="block-1",
            match_status="confirmed", sequence_name="EXPECTED",
            source_in_sec=10.0, source_out_sec=15.0,
        )],
    )
    sequence = {"sequenceId": "target", "name": "Final"}
    manifest = build_assembly_manifest(
        project="FRISKE_001",
        timeline=timeline,
        timeline_path=timeline_path,
        scenario_path=scenario_path,
        audio_path=wav_path,
        source_sequence=sequence,
        layout={
            "sequence": sequence,
            "audio": [{
                "track": "A1", "muted": True,
                "clips": [{
                    "name": "OTHER", "startSeconds": 0.0, "endSeconds": 4.0,
                    "durationSeconds": 4.0, "sourceInSeconds": 20.0,
                    "sourceOutSeconds": 24.0, "enabled": True,
                    "hasAudioComponent": True,
                }],
            }],
        },
    )
    assert {item["kind"] for item in manifest["findings"]} == {
        "muted_audio", "duration_mismatch", "wrong_source", "wrong_source_range"
    }
