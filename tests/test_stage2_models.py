from __future__ import annotations

import pytest

from stage2.models import FullTimeline, TimelineSegment


def test_timeline_segment_from_dict_ignores_derived_and_future_fields() -> None:
    segment = TimelineSegment.from_dict({
        "id": "zk1",
        "type": "zk",
        "text": "Текст",
        "duration_sec": 1.5,
        "end_sec": 99,
        "future_field": "value",
    })

    assert segment.id == "zk1"
    assert segment.end_sec == 1.5


def test_full_timeline_rejects_unknown_schema_version() -> None:
    with pytest.raises(ValueError, match="schema_version"):
        FullTimeline.from_dict({"schema_version": 2, "project": "demo", "segments": []})


def test_full_timeline_requires_positive_fps() -> None:
    with pytest.raises(ValueError, match="positive"):
        FullTimeline.from_dict({"project": "demo", "fps": 0, "segments": []})
