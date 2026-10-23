import json
from pathlib import Path

import pytest

from stage2.assembly_manifest import (
    ASSEMBLY_MANIFEST_SCHEMA_VERSION,
    attach_scenario_revision,
    build_assembly_manifest,
    validate_assembly_manifest,
)
from stage2.assembly_plan import build_assembly_plan
from stage2.models import FullTimeline, TimelineSegment


def _files(tmp_path: Path) -> tuple[Path, Path, Path]:
    scenario = tmp_path / "scenario_normalized.json"
    timeline = tmp_path / "full_timeline.json"
    wav = tmp_path / "final_review.wav"
    scenario.write_text(json.dumps({"scenario_revision_id": "revision-3"}), encoding="utf-8")
    timeline.write_text('{"timeline": true}', encoding="utf-8")
    wav.write_bytes(b"RIFF" + b"audio" * 20)
    return scenario, timeline, wav


def test_assembly_manifest_links_sequence_wav_and_timeline_blocks(tmp_path: Path) -> None:
    scenario_path, timeline_path, wav_path = _files(tmp_path)
    timeline = FullTimeline(
        project="FRISKE_001",
        segments=[
            TimelineSegment(
                "snh1",
                "snh",
                "реплика",
                5.0,
                block_uid="block-1",
                match_status="confirmed",
                source_id="src1",
                sequence_id="source-sequence",
                sequence_name="SOURCE",
                source_in_sec=10.0,
                source_out_sec=15.0,
            )
        ],
    )
    sequence = {"sequenceId": "target-sequence", "name": "FRISKE_001 final"}
    layout = {
        "sequence": sequence,
        "audio": [
            {
                "track": "A1",
                "muted": False,
                "clips": [
                    {
                        "name": "SOURCE",
                        "startSeconds": 0.0,
                        "endSeconds": 5.0,
                        "durationSeconds": 5.0,
                        "sourceInSeconds": 10.0,
                        "sourceOutSeconds": 15.0,
                        "enabled": True,
                        "hasAudioComponent": True,
                    }
                ],
            }
        ],
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
        segments=[
            TimelineSegment(
                "snh1",
                "snh",
                "реплика",
                5.0,
                block_uid="block-1",
                match_status="confirmed",
                source_id="src1",
                sequence_name="Source Name",
                source_in_sec=10.0,
                source_out_sec=15.0,
            )
        ],
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
            "audio": [
                {
                    "track": "A1",
                    "muted": False,
                    "clips": [
                        {
                            "name": "RC_SRC_SOURCE_NAME",
                            "projectItemName": "RC_SRC_SOURCE_NAME",
                            "startSeconds": 0.0,
                            "endSeconds": 5.0,
                            "durationSeconds": 5.0,
                            "sourceInSeconds": 10.0,
                            "sourceOutSeconds": 15.0,
                            "enabled": True,
                            "hasAudioComponent": True,
                        }
                    ],
                }
            ],
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
        segments=[
            TimelineSegment(
                "snh1",
                "snh",
                "реплика",
                5.0,
                block_uid="block-1",
                match_status="confirmed",
                source_id="src1",
                sequence_name="EXPECTED",
                source_in_sec=10.0,
                source_out_sec=15.0,
            )
        ],
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
            "audio": [
                {
                    "track": "A1",
                    "muted": True,
                    "clips": [
                        {
                            "name": "OTHER",
                            "startSeconds": 0.0,
                            "endSeconds": 4.0,
                            "durationSeconds": 4.0,
                            "sourceInSeconds": 20.0,
                            "sourceOutSeconds": 24.0,
                            "enabled": True,
                            "hasAudioComponent": True,
                        }
                    ],
                }
            ],
        },
    )
    assert {item["kind"] for item in manifest["findings"]} == {
        "muted_audio",
        "duration_mismatch",
        "wrong_source",
        "wrong_source_range",
    }


def _mixed_timeline() -> FullTimeline:
    def snh(seg_id: str, status: str, source_id: str, **extra: object) -> TimelineSegment:
        return TimelineSegment(
            seg_id,
            "snh",
            "реплика",
            2.0,
            block_uid=f"uid-{seg_id}",
            match_status=status,
            source_id=source_id,
            sequence_id=f"seq-{source_id}",
            sequence_name=f"SRC {source_id}",
            source_in_sec=1.0,
            source_out_sec=3.0,
            **extra,
        )

    return FullTimeline(
        project="FRISKE_001",
        segments=[
            TimelineSegment("zk1", "zk", "z", 2.0, block_uid="uid-zk1", wav_path="/w/zk1.wav"),
            TimelineSegment("zk2", "zk", "z", 2.0, block_uid="uid-zk2"),  # missing_wav
            snh("snh1", "confirmed", "a"),
            snh("snh2", "needs_review", "a"),
            snh("snh3", "confirmed", "ghost"),  # unmapped_source
            snh("snh4", "auto_matched", ""),  # not_found
            TimelineSegment(
                "life1",
                "life",
                "l",
                2.0,
                block_uid="uid-life1",
                match_status="confirmed",
                source_id="a",
                sequence_name="SRC a",
                source_in_sec=5.0,
                source_out_sec=7.0,
            ),
            TimelineSegment("life2", "life", "l", 2.0, block_uid="uid-life2"),
            TimelineSegment("tech1", "tech", "t", 1.0, block_uid="uid-tech1"),
        ],
    )


def _manifest_for(tmp_path: Path, timeline: FullTimeline, **kwargs: object) -> dict:
    scenario_path, timeline_path, wav_path = _files(tmp_path)
    sequence = {"sequenceId": "target", "name": "Final"}
    return build_assembly_manifest(
        project="FRISKE_001",
        timeline=timeline,
        timeline_path=timeline_path,
        scenario_path=scenario_path,
        audio_path=wav_path,
        source_sequence=sequence,
        layout={"sequence": sequence, "audio": []},
        **kwargs,
    )


def test_manifest_v2_eligibility_and_expected_source_follow_the_plan(tmp_path: Path) -> None:
    timeline = _mixed_timeline()
    mapping = {"a"}
    plan = build_assembly_plan(timeline, media_mapping=mapping)
    manifest = _manifest_for(tmp_path, timeline, media_mapping=mapping)

    assert manifest["schema_version"] == ASSEMBLY_MANIFEST_SCHEMA_VERSION == 2
    assert len(manifest["blocks"]) == len(plan["items"])
    reasons = {}
    for block, item in zip(manifest["blocks"], plan["items"], strict=True):
        assert block["segment_id"] == item["segment_id"]
        assert block["eligible_for_review"] is (item["status"] == "ready")
        assert block["plan_status"] == item["status"]
        assert block["plan_reason"] == item["reason"]
        reasons[block["segment_id"]] = block["plan_reason"]
        if item["status"] == "ready":
            expected = block["expected_source"]
            assert expected["source_id"] == item["source"]["source_id"]
            assert expected["sequence_name"] == item["source"]["sequence_name"]
            if item["type"] != "zk":
                assert expected["source_in_sec"] == item["edit_range"]["in_sec"]
                assert expected["source_out_sec"] == item["edit_range"]["out_sec"]
    assert reasons == {
        "zk1": None,
        "zk2": "missing_wav",
        "snh1": None,
        "snh2": "needs_review",
        "snh3": "unmapped_source",
        "snh4": "not_found",
        "life1": None,
        "life2": "life_unassigned",
        "tech1": None,
    }
    eligible = {b["segment_id"] for b in manifest["blocks"] if b["eligible_for_review"]}
    assert eligible == {"zk1", "snh1", "life1"}
    assert manifest["summary"]["eligible_blocks"] == 3
    # Unmapped source stays out of the review instead of producing a missing_from_sequence.
    assert not [f for f in manifest["findings"] if f["segment_id"] in {"snh3", "life2", "tech1"}]


def test_manifest_v2_without_mapping_does_not_check_media(tmp_path: Path) -> None:
    manifest = _manifest_for(tmp_path, _mixed_timeline())
    by_id = {b["segment_id"]: b for b in manifest["blocks"]}
    assert by_id["snh3"]["eligible_for_review"] is True
    assert by_id["snh3"]["plan_reason"] is None


def test_manifest_v2_plan_sha256_is_deterministic_and_matches_plan(tmp_path: Path) -> None:
    from stage3.builds import canonical_json, sha256_text

    timeline = _mixed_timeline()
    first = _manifest_for(tmp_path, timeline, media_mapping={"a"})
    second = _manifest_for(tmp_path, timeline, media_mapping={"a"})
    plan = build_assembly_plan(timeline, media_mapping={"a"})
    expected = sha256_text(canonical_json(plan))
    assert first["plan_sha256"] == second["plan_sha256"] == expected
    other = _manifest_for(tmp_path, timeline, media_mapping={"a", "ghost"})
    assert other["plan_sha256"] != expected


def test_manifest_v2_requires_plan_sha256_on_validation(tmp_path: Path) -> None:
    scenario_path, timeline_path, wav_path = _files(tmp_path)
    manifest = _manifest_for(tmp_path, _mixed_timeline())
    validate_assembly_manifest(
        manifest, timeline_path=timeline_path, scenario_path=scenario_path, audio_path=wav_path
    )
    manifest["plan_sha256"] = ""
    with pytest.raises(ValueError, match="plan_sha256"):
        validate_assembly_manifest(
            manifest, timeline_path=timeline_path, scenario_path=scenario_path, audio_path=wav_path
        )


def test_manifest_v1_still_validates_and_unknown_schema_is_rejected(tmp_path: Path) -> None:
    scenario_path, timeline_path, wav_path = _files(tmp_path)
    manifest = _manifest_for(tmp_path, _mixed_timeline())
    manifest.pop("plan_sha256")
    manifest["schema_version"] = 1
    validate_assembly_manifest(
        manifest, timeline_path=timeline_path, scenario_path=scenario_path, audio_path=wav_path
    )
    manifest["schema_version"] = 3
    with pytest.raises(ValueError, match="unsupported assembly manifest schema"):
        validate_assembly_manifest(
            manifest, timeline_path=timeline_path, scenario_path=scenario_path, audio_path=wav_path
        )


def test_manifest_contains_recorded_voiceover_ranges(tmp_path: Path) -> None:
    timeline = FullTimeline(
        project="FRISKE_001",
        segments=[
            TimelineSegment(
                "zk1", "zk", "z", 5.0, block_uid="uid-zk1", voiceover_kind="recorded",
                wav_path="/w/montage.wav", source_id="rec1", source_in_sec=10.0,
                source_out_sec=15.0, speech_in_sec=10.5, speech_out_sec=14.5,
                audio_source_duration_sec=120.0,
            ),
            TimelineSegment(
                "zk2", "zk", "z", 2.0, start_sec=5.0, block_uid="uid-zk2", wav_path="/w/zk2.wav"
            ),
        ],
    )
    manifest = _manifest_for(tmp_path, timeline)
    first, second = manifest["blocks"]
    assert first["plan_status"] == "ready"
    assert first["expected_source"]["source_id"] == "rec1"
    assert (first["expected_source"]["source_in_sec"], first["expected_source"]["source_out_sec"]) == (10.0, 15.0)
    assert first["voiceover_kind"] == "recorded"
    assert second["voiceover_kind"] == "tts"
    assert second["expected_source"]["source_in_sec"] is None
