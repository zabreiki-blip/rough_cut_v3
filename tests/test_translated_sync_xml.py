from xml.etree import ElementTree as ET
import pytest
from stage2.models import FullTimeline, TimelineSegment
from stage2.xml_export import build_xml
from stage3.nested_xml import build_nested_xml


def sample():
    audio = {
        "kind": "tts",
        "path": "/tmp/ru.wav",
        "source_in_sec": 1.0,
        "source_out_sec": 15.0,
        "source_duration_sec": 16.0,
        "sha256": "a" * 64,
        "text_sha256": "b" * 64,
        "delay_sec": 0.8,
        "timeline_offset_sec": 0.8,
        "duck_db": -18.0,
        "gain_db": 6.0,
        "fade_sec": 0.2,
    }
    s = TimelineSegment(
        id="snh1",
        type="snh",
        text="hello",
        duration_sec=14.8,
        source_id="s",
        source_in_sec=5,
        source_out_sec=15,
        match_status="confirmed",
        translation_audio=audio,
    )
    return FullTimeline(project="t", segments=[s]), {
        "s": {"path": "/tmp/source.mp4", "duration": 30, "fps": 25, "width": 1920, "height": 1080}
    }


def test_nested_xml_translation_lane_and_original_clip_duration():
    timeline, catalog = sample()
    final = ET.fromstring(build_nested_xml(timeline, catalog).split("\n", 2)[2]).find(
        "./project/children/sequence"
    )
    assert final.findtext("duration") == "370"
    assert final.findtext("./media/video/track/clipitem/end") == "250"
    original = final.find("./media/audio/track[1]/clipitem")
    assert original.findtext("end") == "250"
    assert original.find("./filter/effect/parameter/keyframe") is not None
    clip = final.find("./media/audio/track[3]/clipitem")
    assert [clip.findtext(k) for k in ("start", "end", "in", "out")] == ["20", "370", "25", "375"]
    assert float(clip.findtext("./filter/effect/parameter/value")) == pytest.approx(
        1.995262, rel=1e-5
    )


def test_legacy_export_rejects_translation():
    timeline, _ = sample()
    with pytest.raises(ValueError, match="Stage 3"):
        build_xml(timeline)


def test_duck_remains_through_source_end_when_translation_continues():
    timeline, catalog = sample()
    final = ET.fromstring(build_nested_xml(timeline, catalog).split("\n", 2)[2]).find(
        "./project/children/sequence"
    )
    keys = final.findall("./media/audio/track[1]/clipitem/filter/effect/parameter/keyframe")
    assert float(keys[-1].findtext("value")) == pytest.approx(10 ** (-18 / 20))
    assert int(keys[-1].findtext("when")) == 375


def test_duck_restores_if_translation_finishes_before_source_end():
    timeline, catalog = sample()
    segment = timeline.segments[0]
    segment.translation_audio["source_out_sec"] = 5.0
    segment.duration_sec = 10.0
    final = ET.fromstring(build_nested_xml(timeline, catalog).split("\n", 2)[2]).find(
        "./project/children/sequence"
    )
    keys = final.findall("./media/audio/track[1]/clipitem/filter/effect/parameter/keyframe")
    assert float(keys[-1].findtext("value")) == 1.0
    assert int(keys[-1].findtext("when")) < 375


def test_draft_placeholder_includes_translation_but_complete_plan_is_blocked():
    from stage2.assembly_plan import build_assembly_plan, validate_plan_complete

    timeline, catalog = sample()
    timeline.segments[0].placeholder_path = "/tmp/placeholder.png"
    plan = build_assembly_plan(timeline, media_mapping=set())
    assert plan["items"][0]["status"] == "placeholder"
    assert any(b["segment_id"] == "snh1" for b in validate_plan_complete(plan))
    final = ET.fromstring(build_nested_xml(timeline, catalog, plan=plan).split("\n", 2)[2]).find(
        "./project/children/sequence"
    )
    assert final.find("./media/video/track/clipitem/file") is not None
    assert final.find("./media/audio/track[3]/clipitem/file/pathurl") is not None
