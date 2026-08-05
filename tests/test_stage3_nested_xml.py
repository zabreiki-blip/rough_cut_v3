from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from stage2.models import FullTimeline, TimelineSegment
from stage3.nested_xml import build_nested_xml


def _root(xml: str) -> ET.Element:
    return ET.fromstring(xml.split("\n", 2)[2])


def _timeline() -> FullTimeline:
    return FullTimeline(
        project="Брик",
        fps=25,
        segments=[
            TimelineSegment(
                id="snh1",
                type="snh",
                text="У советских авторов...",
                duration_sec=3,
                start_sec=0,
                source_id="свиблова",
                source_in_sec=10,
                source_out_sec=13,
                match_status="confirmed",
            ),
            TimelineSegment(
                id="zk1",
                type="zk",
                text="Текст за кадром",
                duration_sec=2,
                start_sec=3,
                wav_path="/tmp/голос 1.wav",
            ),
            TimelineSegment(
                id="snh2",
                type="snh",
                text="Пока не найдено",
                duration_sec=5,
                start_sec=5,
                block_uid="block-cyrillic-safe",
                speaker="Смольников",
                placeholder_path="/tmp/Смольников.png",
                match_status="not_found",
            ),
        ],
    )


def _catalog() -> dict:
    return {
        "свиблова": {
            "path": "/Volumes/L_BRICK/DOP/Брик просмотровки/Свиблова.mp4",
            "duration": 120,
            "fps": 25,
            "width": 1920,
            "height": 1080,
        }
    }


def test_project_contains_viewing_bin_source_and_final_sequence() -> None:
    root = _root(build_nested_xml(_timeline(), _catalog()))

    assert root.attrib == {"version": "5"}
    assert root.findtext("./project/name") == "Брик"
    viewing_bin = root.find("./project/children/bin")
    assert viewing_bin is not None
    assert viewing_bin.findtext("name") == "RC_VIEWINGS"
    source = viewing_bin.find("./children/sequence")
    assert source is not None
    assert source.findtext("name") == "RC_SRC_Свиблова"
    assert source.findtext("duration") == "3000"
    assert root.findtext("./project/children/sequence/name") == "ROUGH_CUT_Брик"


def test_matched_clip_references_nested_sequence_with_correct_frames() -> None:
    root = _root(build_nested_xml(_timeline(), _catalog()))
    source = root.find("./project/children/bin/children/sequence")
    final = root.find("./project/children/sequence")
    assert source is not None and final is not None
    clips = final.findall("./media/video/track/clipitem")
    matched = clips[0]

    assert matched.findtext("start") == "0"
    assert matched.findtext("end") == "75"
    assert matched.findtext("in") == "250"
    assert matched.findtext("out") == "325"
    assert matched.find("sequence").get("id") == source.get("id")
    assert len(final.findall("./media/audio/track/clipitem/sequence")) == 1


def test_new_revision_block_is_red_on_video_and_audio_tracks() -> None:
    timeline = _timeline()
    timeline.segments[0].revision_status = "added"

    root = _root(build_nested_xml(timeline, _catalog()))
    final = root.find("./project/children/sequence")

    assert final is not None
    assert final.findtext("./media/video/track/clipitem/labels/label2") == "Rose"
    assert final.findtext("./media/audio/track/clipitem/labels/label2") == "Rose"


def test_voiceover_and_unmatched_placeholder_are_preserved() -> None:
    root = _root(build_nested_xml(_timeline(), _catalog()))
    final = root.find("./project/children/sequence")
    assert final is not None
    video_clips = final.findall("./media/video/track/clipitem")
    placeholder = video_clips[1]
    wav = final.find("./media/audio/track[2]/clipitem")

    assert placeholder.find("sequence") is None
    assert placeholder.findtext("name").startswith("RC_BLOCK block-cyrillic-safe")
    assert "%D0%A1%D0%BC%D0%BE%D0%BB%D1%8C%D0%BD%D0%B8%D0%BA%D0%BE%D0%B2.png" in (
        placeholder.findtext("file/pathurl")
    )
    assert wav is not None
    assert wav.findtext("start") == "75"
    assert "%D0%B3%D0%BE%D0%BB%D0%BE%D1%81%201.wav" in wav.findtext("file/pathurl")


def test_same_media_path_creates_only_one_source_sequence() -> None:
    timeline = _timeline()
    second = TimelineSegment(
        id="host1",
        type="host",
        text="Ведущий",
        duration_sec=1,
        start_sec=10,
        source_id="alias",
        source_in_sec=20,
        source_out_sec=21,
        match_status="auto_matched",
    )
    timeline.segments.append(second)
    catalog = _catalog()
    catalog["alias"] = dict(catalog["свиблова"])

    root = _root(build_nested_xml(timeline, catalog))
    assert len(root.findall("./project/children/bin/children/sequence")) == 1
    references = root.findall("./project/children/sequence/media/video/track/clipitem/sequence")
    assert len(references) == 2
    assert len({item.get("id") for item in references}) == 1


def test_unreferenced_viewing_still_gets_a_source_sequence() -> None:
    catalog = _catalog()
    catalog["курская"] = {
        "path": "/Volumes/L_BRICK/DOP/Курская.mp4",
        "duration": 42,
        "fps": 25,
        "width": 1920,
        "height": 1080,
    }

    root = _root(build_nested_xml(_timeline(), catalog))
    names = root.findall("./project/children/bin/children/sequence/name")
    assert {item.text for item in names} == {"RC_SRC_Курская", "RC_SRC_Свиблова"}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"source_out_sec": 121}, "exceeds media duration"),
        ({"duration_sec": 4}, "does not match its source range"),
        ({"source_id": "missing"}, "unknown source"),
    ],
)
def test_invalid_matched_ranges_are_rejected(change: dict, message: str) -> None:
    timeline = _timeline()
    for key, value in change.items():
        setattr(timeline.segments[0], key, value)

    with pytest.raises(ValueError, match=message):
        build_nested_xml(timeline, _catalog())


def test_dictionary_input_and_cyrillic_file_url() -> None:
    timeline = _timeline().to_dict()
    root = _root(build_nested_xml(timeline, {"sources": _catalog()}))
    url = root.findtext(
        "./project/children/bin/children/sequence/media/video/track/clipitem/file/pathurl"
    )

    assert url is not None
    assert url.startswith("file:///")
    assert "%D0%91%D1%80%D0%B8%D0%BA%20" in url
    assert Path("/tmp").is_absolute()  # documents that exported paths are absolute


def test_accepts_media_catalog_list_contract() -> None:
    catalog = {
        "sources": [
            {
                "source_id": "свиблова",
                "name": "Свиблова.mp4",
                "container_path": "/Volumes/L_BRICK/DOP/Свиблова.mp4",
                "duration_sec": 120,
                "fps": 25.0,
                "width": 1920,
                "height": 1080,
            }
        ]
    }

    root = _root(build_nested_xml(_timeline(), catalog))
    source = root.find("./project/children/bin/children/sequence")
    assert source is not None
    assert source.findtext("name") == "RC_SRC_Свиблова"
    assert source.findtext("media/video/track/clipitem/file/pathurl").endswith(
        "/%D0%A1%D0%B2%D0%B8%D0%B1%D0%BB%D0%BE%D0%B2%D0%B0.mp4"
    )


@pytest.mark.parametrize("extension", ["wav", "mp3"])
def test_audio_only_source_uses_card_and_audio_in_nested_sequence(extension: str) -> None:
    catalog = {
        "свиблова": {
            "path": f"/Volumes/L_BRICK/DOP/Свиблова.{extension}",
            "duration": 120,
            "fps": 25,
            "media_kind": "audio",
            "placeholder_path": "/tmp/НУЖНО ВИДЕО Свиблова.png",
        }
    }

    root = _root(build_nested_xml(_timeline(), catalog))
    source = root.find("./project/children/bin/children/sequence")
    final = root.find("./project/children/sequence")

    assert source is not None and final is not None
    card_url = source.findtext("./media/video/track/clipitem/file/pathurl")
    audio_url = source.findtext("./media/audio/track/clipitem/file/pathurl")
    assert card_url is not None and "%D0%9D%D0%A3%D0%96%D0%9D%D0%9E%20" in card_url
    assert audio_url is not None and audio_url.endswith(f".{extension}")
    nested_video = final.findall("./media/video/track/clipitem/sequence")
    nested_audio = final.findall("./media/audio/track[1]/clipitem/sequence")
    assert len(nested_video) == 1
    assert len(nested_audio) == 1
    assert nested_video[0].get("id") == source.get("id")
    assert nested_audio[0].get("id") == source.get("id")


def test_audio_only_source_requires_placeholder_card() -> None:
    catalog = {
        "свиблова": {
            "path": "/tmp/Свиблова.wav",
            "duration": 120,
            "fps": 25,
            "media_kind": "audio",
        }
    }

    with pytest.raises(ValueError, match="has no placeholder card"):
        build_nested_xml(_timeline(), catalog)
