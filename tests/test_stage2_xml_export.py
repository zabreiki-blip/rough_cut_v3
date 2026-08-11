import json
from xml.etree import ElementTree as ET

import pytest

from stage1.normalizer import load_normalization
from stage2.models import FullTimeline, TimelineSegment
from stage2.timeline_parser import parse_normalized_timeline
from stage2.xml_export import build_xml


def test_placeholder_clip_name_contains_stable_block_identity() -> None:
    timeline = FullTimeline(
        project="BRIK",
        segments=[
            TimelineSegment(
                id="snh98",
                type="snh",
                text="У советских авторов...",
                duration_sec=13.34,
                block_uid="block-266-ea56f9c6",
                speaker="Шпагин",
                timecode="",
                placeholder_path="/tmp/snh98_placeholder.png",
            )
        ],
    )

    root = ET.fromstring(build_xml(timeline).split("\n", 2)[2])
    clip_name = root.findtext("./sequence/media/video/track/clipitem/name")
    marker_name = root.findtext(
        "./sequence/media/video/track/clipitem/marker/name"
    )

    expected = "RC_BLOCK block-266-ea56f9c6 | Шпагин []"
    assert clip_name == expected
    assert marker_name == expected


@pytest.mark.parametrize("block_uid", ["", "не-ascii", "block/unsafe"])
def test_placeholder_xml_rejects_missing_or_unsafe_block_uid(
    block_uid: str,
) -> None:
    timeline = FullTimeline(
        project="BRIK",
        segments=[
            TimelineSegment(
                id="snh1",
                type="snh",
                text="Текст",
                duration_sec=10,
                block_uid=block_uid,
                placeholder_path="/tmp/snh1_placeholder.png",
            )
        ],
    )

    with pytest.raises(ValueError, match="valid canonical block_uid"):
        build_xml(timeline)


def test_placeholder_xml_rejects_duplicate_block_uid() -> None:
    segments = [
        TimelineSegment(
            id=f"snh{index}",
            type="snh",
            text="Текст",
            duration_sec=10,
            block_uid="block-duplicate",
            placeholder_path=f"/tmp/snh{index}_placeholder.png",
        )
        for index in (1, 2)
    ]

    with pytest.raises(ValueError, match="duplicate placeholder block_uid"):
        build_xml(FullTimeline(project="BRIK", segments=segments))


def test_valid_subframe_audio_segment_is_exported_as_one_frame() -> None:
    timeline = FullTimeline(
        project="BRIK",
        fps=25,
        segments=[
            TimelineSegment(
                id="zk1",
                type="zk",
                text="Короткий текст",
                start_sec=0.001,
                duration_sec=0.001,
                wav_path="/tmp/zk1.wav",
            )
        ],
    )

    root = ET.fromstring(build_xml(timeline).split("\n", 2)[2])
    clip = root.find("./sequence/media/audio/track/clipitem")

    assert clip is not None
    assert clip.findtext("duration") == "1"
    assert clip.findtext("end") == str(int(clip.findtext("start")) + 1)


@pytest.mark.parametrize("revision_status", [
    "added", "edited", "type_changed", "speaker_changed", "moved",
])
def test_changed_revision_placeholder_has_red_premiere_label(
    revision_status: str,
) -> None:
    timeline = FullTimeline(
        project="BRIK",
        segments=[TimelineSegment(
            id="snh1",
            type="snh",
            text="Новый фрагмент",
            duration_sec=10,
            block_uid="block-new",
            revision_status=revision_status,
            placeholder_path="/tmp/snh1_placeholder.png",
        )],
    )

    root = ET.fromstring(build_xml(timeline).split("\n", 2)[2])

    assert root.findtext("./sequence/media/video/track/clipitem/labels/label2") == "Rose"


@pytest.mark.parametrize("revision_status", ["", "unchanged"])
def test_reusable_revision_placeholder_keeps_default_premiere_label(
    revision_status: str,
) -> None:
    timeline = FullTimeline(
        project="BRIK",
        segments=[TimelineSegment(
            id="snh1",
            type="snh",
            text="Неизменённый фрагмент",
            duration_sec=10,
            block_uid="block-existing",
            revision_status=revision_status,
            placeholder_path="/tmp/snh1_placeholder.png",
        )],
    )

    root = ET.fromstring(build_xml(timeline).split("\n", 2)[2])

    assert root.find("./sequence/media/video/track/clipitem/labels") is None


def test_loaded_edited_revision_reaches_premiere_xml(tmp_path) -> None:
    normalized_path = tmp_path / "scenario_normalized.json"
    normalized_path.write_text(json.dumps({
        "source": str(tmp_path / "scenario.docx"),
        "source_hash": "abc",
        "status": "confirmed",
        "blocks": [{
            "uid": "stable-sync-block",
            "order": 1,
            "original_text": "ГЕРОЙ: Изменённая реплика",
            "normalized_text": "Изменённая реплика",
            "type": "sync",
            "speaker": "Герой",
            "versioning": {
                "parent_block_uid": "stable-sync-block",
                "change_status": "edited",
            },
        }],
    }), encoding="utf-8")

    normalization = load_normalization(normalized_path)
    timeline = parse_normalized_timeline(
        normalization,
        tmp_path,
        tmp_path,
        "BRIK",
    )
    root = ET.fromstring(build_xml(timeline).split("\n", 2)[2])

    assert root.findtext(
        "./sequence/media/video/track/clipitem/labels/label2"
    ) == "Rose"
