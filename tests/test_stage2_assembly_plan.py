from __future__ import annotations

import json

import pytest

from stage2.assembly_plan import build_assembly_plan, plan_to_report
from stage2.models import FullTimeline, TimelineSegment
from stage_common import placeholder_reason


def _snh(seg_id: str = "snh1", **overrides) -> dict:
    segment = {
        "id": seg_id,
        "type": "snh",
        "text": "Цитата",
        "duration_sec": 10.0,
        "block_uid": f"uid-{seg_id}",
        "speaker": "ИВАН ПЕТРОВ",
        "match_status": "not_found",
        "source_id": "",
        "source_in_sec": 0.0,
        "source_out_sec": 0.0,
        "placeholder_path": f"/tmp/{seg_id}.png",
    }
    segment.update(overrides)
    return segment


def _matched(seg_id: str = "snh1", **overrides) -> dict:
    fields = {
        "match_status": "auto_matched",
        "source_id": "ivan_01",
        "source_in_sec": 1.0,
        "source_out_sec": 11.0,
        "speech_in_sec": 1.5,
        "speech_out_sec": 10.0,
    }
    fields.update(overrides)
    return _snh(seg_id, **fields)


@pytest.mark.parametrize(
    ("segment", "mapped", "expected"),
    [
        ({"type": "zk", "wav_path": "", "duration_sec": 3.0}, None, "missing_wav"),
        ({"type": "zk", "wav_path": "/a.wav", "duration_sec": 0.0}, None, "missing_wav"),
        ({"type": "zk", "wav_path": "/a.wav", "duration_sec": 3.0}, None, None),
        (_snh(unmatched_reason="no_eligible_source", speaker=""), None, "no_speaker"),
        (_snh(unmatched_reason="no_eligible_source", speaker="12.03 -"), None, "no_speaker"),
        (_snh(unmatched_reason="no_eligible_source"), None, "no_file"),
        (_snh(unmatched_reason="asr_failed"), None, "asr_failed"),
        (_snh(unmatched_reason="no_candidate"), None, "not_found"),
        (_snh(unmatched_reason="below_review_threshold"), None, "not_found"),
        (_snh(unmatched_reason="query_too_short"), None, "not_found"),
        (_snh(), None, "not_found"),
        (_snh(match_status="needs_review"), None, "needs_review"),
        (_matched(match_status="needs_review"), None, "needs_review"),
        (_matched(match_status="confirmed", retime_pending_review=True), None, "needs_review"),
        (_matched(match_status="confirmed"), None, None),
        (_matched(), {"ivan_01"}, None),
        (_matched(), set(), "unmapped_source"),
        (_matched(), None, None),
        ({"type": "host", "match_status": "not_found", "duration_sec": 5.0}, None, "not_found"),
        ({"type": "life", "duration_sec": 10.0}, None, "life_unassigned"),
        ({"type": "tech", "duration_sec": 0.0}, None, None),
    ],
)
def test_placeholder_reason_table(segment: dict, mapped: set[str] | None, expected) -> None:
    assert placeholder_reason(segment, mapped=mapped) == expected


def test_placeholder_reason_legacy_segment_without_new_fields_is_not_found() -> None:
    legacy = {"type": "snh", "match_status": "not_found", "duration_sec": 5.0, "speaker": ""}

    assert placeholder_reason(legacy) == "not_found"


def _synthetic_timeline() -> dict:
    segments: list[dict] = []
    start = 0.0

    def add(segment: dict) -> None:
        nonlocal start
        segment["start_sec"] = start
        start += float(segment["duration_sec"])
        segments.append(segment)

    add({"id": "zk1", "type": "zk", "text": "z", "duration_sec": 4.0, "wav_path": "/z.wav"})
    for index in range(40):
        add(_snh(f"snh{index + 1}", match_status="needs_review"))
    reasons = ["no_eligible_source", "asr_failed", "no_candidate", ""]
    for index in range(36):
        add(
            _snh(
                f"snh{index + 41}",
                unmatched_reason=reasons[index % 4],
                speaker="" if index % 8 == 0 else "ИВАН ПЕТРОВ",
            )
        )
    for index in range(8):
        add(
            {
                "id": f"life{index + 1}",
                "type": "life",
                "text": "l",
                "duration_sec": 10.0,
                "placeholder_path": f"/tmp/life{index + 1}.png",
            }
        )
    add(_matched("snh77"))
    add({"id": "tech1", "type": "tech", "text": "t", "duration_sec": 0.0})
    return {
        "schema_version": 1,
        "project": "demo",
        "sync_matches_fingerprint": "fp-1",
        "segments": segments,
    }


def test_plan_counts_synthetic_placeholders() -> None:
    plan = build_assembly_plan(_synthetic_timeline(), media_mapping={"ivan_01"})

    assert plan["schema_version"] == 1
    assert plan["sync_matches_fingerprint"] == "fp-1"
    assert plan["edit_policy_version"] is None
    summary = plan["summary"]
    assert summary["placeholders"]["segments"] == 84
    assert summary["placeholders"]["duration_sec"] == 840.0
    by_reason = summary["placeholders"]["by_reason"]
    assert by_reason["needs_review"] == {"segments": 40, "duration_sec": 400.0}
    assert by_reason["life_unassigned"] == {"segments": 8, "duration_sec": 80.0}
    not_found_like = sum(
        by_reason.get(key, {}).get("segments", 0)
        for key in ("no_speaker", "no_file", "asr_failed", "not_found")
    )
    assert not_found_like == 36
    assert by_reason["asr_failed"]["segments"] == 9
    assert summary["ready"] == 2  # matched snh + zk
    assert summary["items"] == len(plan["items"])
    assert summary["open_tasks"] == 40 + 8 + by_reason.get("no_file", {}).get("segments", 0)


def test_plan_item_shape_ready_and_placeholder() -> None:
    timeline = {
        "segments": [
            {**_matched("snh1"), "start_sec": 0.0, "match_confidence": 0.9, "sequence_id": "q1"},
            {**_snh("snh2", unmatched_reason="asr_failed"), "start_sec": 10.0},
        ]
    }

    ready, placeholder = build_assembly_plan(timeline, media_mapping={"ivan_01"})["items"]

    assert ready["status"] == "ready" and ready["reason"] is None
    assert ready["required"] is True
    assert ready["timeline"] == {"in_sec": 0.0, "out_sec": 10.0, "duration_sec": 10.0}
    assert ready["source"]["media_status"] == "mapped"
    assert ready["source"]["sequence_id"] == "q1"
    assert ready["speech_range"] == {"in_sec": 1.5, "out_sec": 10.0}
    assert ready["edit_range"] == {"in_sec": 1.0, "out_sec": 11.0}
    assert ready["padding"] == {
        "head_sec": 0.5,
        "tail_sec": 1.0,
        "trimmed_head_sec": 0,
        "trimmed_tail_sec": 0,
    }
    assert ready["tasks"] == []
    assert placeholder["status"] == "placeholder" and placeholder["reason"] == "asr_failed"
    assert placeholder["placeholder"]["path"] == "/tmp/snh2.png"
    assert placeholder["order"] == 1
    assert placeholder["provenance"]["match_status"] == "not_found"


def test_plan_media_status_and_load_source_task() -> None:
    timeline = {"segments": [_matched("snh1"), _snh("snh2", unmatched_reason="no_eligible_source")]}

    unchecked = build_assembly_plan(timeline)["items"][0]
    unmapped, no_file = build_assembly_plan(timeline, media_mapping=set())["items"]

    assert unchecked["source"]["media_status"] == "unchecked"
    assert unchecked["status"] == "ready"
    assert unmapped["source"]["media_status"] == "unmapped"
    assert unmapped["reason"] == "unmapped_source"
    assert unmapped["tasks"] == [{"kind": "load_source", "open": True}]
    assert no_file["reason"] == "no_file"
    assert no_file["tasks"] == [{"kind": "load_source", "open": True}]


def test_plan_task_kinds() -> None:
    timeline = {
        "segments": [
            _snh("snh1", match_status="needs_review"),
            {"id": "life1", "type": "life", "text": "l", "duration_sec": 3.0},
        ]
    }

    review, life = build_assembly_plan(timeline)["items"]

    assert review["tasks"] == [{"kind": "review_match", "open": True}]
    assert life["tasks"] == [{"kind": "assign_life_material", "open": True}]
    assert life["required"] is True


def test_plan_missing_wav_is_skipped_but_counted_by_reason() -> None:
    timeline = {
        "segments": [
            {"id": "zk1", "type": "zk", "text": "z", "duration_sec": 0.0, "wav_path": ""},
            {"id": "tech1", "type": "tech", "text": "t", "duration_sec": 0.0},
        ]
    }

    zk, tech = build_assembly_plan(timeline)["items"]
    summary = build_assembly_plan(timeline)["summary"]

    assert zk["status"] == "skipped" and zk["reason"] == "missing_wav"
    assert tech["status"] == "skipped" and tech["required"] is False
    assert summary["placeholders"]["segments"] == 0
    assert summary["placeholders"]["by_reason"]["missing_wav"]["segments"] == 1


def test_plan_is_deterministic_and_accepts_full_timeline() -> None:
    timeline = _synthetic_timeline()

    first = json.dumps(build_assembly_plan(timeline, media_mapping={"ivan_01"}), sort_keys=True)
    second = json.dumps(build_assembly_plan(timeline, media_mapping={"ivan_01"}), sort_keys=True)

    assert first == second
    assert len(json.loads(first)["timeline_sha256"]) == 64
    full = FullTimeline(
        project="demo",
        segments=[TimelineSegment(id="life1", type="life", text="l", duration_sec=2.0)],
    )
    assert build_assembly_plan(full)["summary"]["placeholders"]["segments"] == 1


def test_plan_hash_changes_with_timeline() -> None:
    timeline = _synthetic_timeline()
    changed = json.loads(json.dumps(timeline))
    changed["segments"][1]["match_status"] = "not_found"

    assert (
        build_assembly_plan(timeline)["timeline_sha256"]
        != build_assembly_plan(changed)["timeline_sha256"]
    )


def test_plan_to_report_counts_synthetic_placeholders() -> None:
    timeline = _synthetic_timeline()

    report = plan_to_report(build_assembly_plan(timeline, media_mapping={"ivan_01"}))

    assert report["placeholder_segments"] == 84
    assert report["placeholder_duration_sec"] == 840.0
    assert report["unmapped_source_placeholders"] == 0
    assert set(report) >= {"placeholder_breakdown", "plan_summary"}


def test_plan_to_report_counts_unmapped_sources() -> None:
    timeline = {"segments": [_matched("snh1"), _matched("snh2")]}

    report = plan_to_report(build_assembly_plan(timeline, media_mapping=set()))

    assert report["unmapped_source_placeholders"] == 2
    assert report["placeholder_duration_sec"] == 20.0
