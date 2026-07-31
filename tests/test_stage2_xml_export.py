from xml.etree import ElementTree as ET

from stage2.models import FullTimeline, TimelineSegment
from stage2.xml_export import build_xml
import pytest


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
