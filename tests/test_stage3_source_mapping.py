from stage2.models import FullTimeline, TimelineSegment
from stage3.source_mapping import map_catalog_to_timeline


def _source(source_id: str, sequence: str, speaker: str = "") -> dict:
    return {
        "id": source_id,
        "premiere_sequence": sequence,
        "speaker": speaker or sequence,
    }


def _media(source_id: str, name: str, duration: float = 8000) -> dict:
    return {
        "source_id": source_id,
        "name": name,
        "container_path": f"/review-media/{name}",
        "duration_sec": duration,
        "fps": 25,
        "width": 1920,
        "height": 1080,
    }


def _brik_timeline(source_ids: list[str]) -> FullTimeline:
    segments = []
    cursor = 0.0
    for index, source_id in enumerate(source_ids, start=1):
        segments.append(TimelineSegment(
            id=f"snh{index}",
            type="snh",
            text="СНХ",
            duration_sec=2,
            start_sec=cursor,
            source_id=source_id,
            source_in_sec=100,
            source_out_sec=102,
            match_status="confirmed",
        ))
        cursor += 2
    return FullTimeline(project="BRIK", fps=25, segments=segments)


def test_brik_maps_six_viewings_and_leaves_missing_people_unmapped() -> None:
    sources = [
        _source("kurskaya", "Курская", "Дана Курская"),
        _source("smekhov", "Смехов", "Вениамин Смехов"),
        _source("sviblova", "130626_Свиблова", "Ольга Свиблова"),
        _source("vasilieva", "100626_Васильева", "Татьяна Васильева"),
        _source("barton", "180626_Бартон", "Марк Бартон"),
        _source("smolnikova", "Смольникова", "Мария Смольникова"),
        _source("krasnova", "220626_Краснова", "Краснова"),
        _source("shpagin", "Шпагин", "Шпагин"),
    ]
    catalog = {"sources": [
        _media("m1", "080626_Курская.mp4"),
        _media("m2", "080626_Смехов.mp4"),
        _media("m3", "130626_Свиблова.mp4"),
        _media("m4", "100626_Васильева.mp4"),
        _media("m5", "180626_Бартон.mp4"),
        _media("m6", "070626_Смольникова.mp4"),
        _media("m7", "080526_Родионов.mp4"),
        _media("m8", "080626_Воденников.mp4"),
    ]}
    timeline = _brik_timeline([source["id"] for source in sources])

    result = map_catalog_to_timeline(timeline, {"sources": sources}, catalog)

    assert set(result.catalog) == {
        "kurskaya", "smekhov", "sviblova", "vasilieva", "barton", "smolnikova"
    }
    assert result.unmapped_source_ids == ("krasnova", "shpagin")
    assert result.details["kurskaya"]["media_name"] == "080626_Курская.mp4"
    assert result.details["krasnova"]["reason"] == "no_name_match"


def test_exact_sequence_name_wins_over_weaker_speaker_match() -> None:
    timeline = _brik_timeline(["vasilieva"])
    sources = [_source("vasilieva", "100626_Васильева", "Татьяна Васильева")]
    exact = _media("exact", "100626_Васильева.mp4", duration=200)
    generic = _media("generic", "Васильева.mp4", duration=9000)

    result = map_catalog_to_timeline(timeline, sources, [generic, exact])

    assert result.catalog["vasilieva"] is exact
    assert result.details["vasilieva"]["reason"] == "exact_sequence_name"


def test_equal_best_name_matches_fail_closed_as_ambiguous() -> None:
    timeline = _brik_timeline(["kurskaya"])
    sources = [_source("kurskaya", "Курская")]
    catalog = [
        _media("a", "080626_Курская.mp4"),
        _media("b", "090626_Курская.mp4"),
    ]

    result = map_catalog_to_timeline(timeline, sources, catalog)

    assert result.catalog == {}
    assert result.unmapped_source_ids == ("kurskaya",)
    assert result.details["kurskaya"]["reason"] == "ambiguous_name_match"
    assert len(result.details["kurskaya"]["candidates"]) == 2


def test_named_media_is_rejected_when_used_range_exceeds_duration() -> None:
    timeline = _brik_timeline(["smekhov"])
    timeline.segments[0].source_in_sec = 99
    timeline.segments[0].source_out_sec = 101
    source = _source("smekhov", "Смехов")

    result = map_catalog_to_timeline(
        timeline, [source], [_media("media", "080626_Смехов.mp4", duration=100)]
    )

    assert result.catalog == {}
    assert result.details["smekhov"]["reason"] == "media_too_short"
    assert result.details["smekhov"]["max_source_out_sec"] == 101


def test_one_frame_duration_tolerance_is_allowed() -> None:
    timeline = _brik_timeline(["barton"])
    timeline.segments[0].source_in_sec = 98.02
    timeline.segments[0].source_out_sec = 100.02

    result = map_catalog_to_timeline(
        timeline,
        [_source("barton", "Бартон")],
        [_media("media", "180626_Бартон.mp4", duration=100)],
    )

    assert set(result.catalog) == {"barton"}


def test_unrelated_similar_name_is_not_fuzzy_matched() -> None:
    timeline = _brik_timeline(["ira"])
    result = map_catalog_to_timeline(
        timeline,
        [_source("ira", "Ира")],
        [_media("irbe", "090626_Ирбе.mp4")],
    )

    assert result.unmapped_source_ids == ("ira",)
    assert result.details["ira"]["reason"] == "no_name_match"
