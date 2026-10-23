from __future__ import annotations

import pytest

from stage2.models import FullTimeline, TimelineSegment


def test_timeline_segment_from_dict_ignores_derived_and_future_fields() -> None:
    segment = TimelineSegment.from_dict(
        {
            "id": "zk1",
            "type": "zk",
            "text": "Текст",
            "duration_sec": 1.5,
            "end_sec": 99,
            "future_field": "value",
        }
    )

    assert segment.id == "zk1"
    assert segment.end_sec == 1.5


def test_full_timeline_rejects_unknown_schema_version() -> None:
    with pytest.raises(ValueError, match="schema_version"):
        FullTimeline.from_dict({"schema_version": 2, "project": "demo", "segments": []})


def test_full_timeline_requires_positive_fps() -> None:
    with pytest.raises(ValueError, match="positive"):
        FullTimeline.from_dict({"project": "demo", "fps": 0, "segments": []})


def test_timeline_segment_new_diagnostic_fields_default_for_old_json() -> None:
    segment = TimelineSegment.from_dict(
        {"id": "snh1", "type": "snh", "text": "Текст", "duration_sec": 2}
    )

    assert segment.unmatched_reason == ""
    assert segment.match_strategy == ""
    assert segment.manual_range is False
    assert segment.merged_from == []
    # Mutable default must not be shared between instances.
    other = TimelineSegment.from_dict({"id": "snh2", "type": "snh", "text": "x", "duration_sec": 1})
    segment.merged_from.append("a")
    assert other.merged_from == []


def test_timeline_segment_new_fields_round_trip() -> None:
    segment = TimelineSegment(
        id="snh1",
        type="snh",
        text="Текст",
        duration_sec=2.0,
        unmatched_reason="no_candidate",
        match_strategy="fuzzy",
        manual_range=True,
        merged_from=["u1", "u2"],
    )

    restored = TimelineSegment.from_dict(segment.to_dict())

    assert restored == segment
