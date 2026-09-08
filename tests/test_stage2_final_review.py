from stage2.final_review import review_rendered_timeline
from stage2.models import FullTimeline, TimelineSegment
from stage2.sync_models import WordIndex, WordTimestamp


def _index(text: str) -> WordIndex:
    return WordIndex(
        source_id="render",
        words=[WordTimestamp(word, number, number + 0.4) for number, word in enumerate(text.split())],
    )


def test_rendered_review_detects_missing_scenario_block() -> None:
    timeline = FullTimeline(
        project="DEMO",
        segments=[
            TimelineSegment("zk1", "zk", "первая фраза", 2.0),
            TimelineSegment("zk2", "zk", "вторая обязательная фраза", 2.0, start_sec=2.0),
        ],
    )
    report = review_rendered_timeline(timeline, _index("первая фраза"))
    assert report["summary"]["missing"] == 1
    assert report["findings"][0]["segment_id"] == "zk2"


def test_rendered_review_proposes_safe_sync_handle_expansion() -> None:
    timeline = FullTimeline(
        project="DEMO",
        segments=[TimelineSegment(
            "snh1", "snh", "важное начало длинной реплики", 4.0,
            block_uid="sync-1", source_in_sec=10.0, source_out_sec=14.0,
            sequence_id="source-1", sequence_name="SYNC", start_sec=0.0,
            match_status="confirmed",
        )],
    )
    report = review_rendered_timeline(timeline, _index("начало длинной реплики"))
    assert report["summary"]["truncated"] == 1
    assert report["corrections"] == []
    assert report["summary"]["automatic_corrections"] == 0


def test_rendered_review_marks_unexpected_speech_between_scenario_blocks() -> None:
    timeline = FullTimeline(
        project="DEMO",
        segments=[
            TimelineSegment("zk1", "zk", "первая фраза", 2.0),
            TimelineSegment("zk2", "zk", "вторая фраза", 2.0, start_sec=2.0),
        ],
    )
    report = review_rendered_timeline(timeline, _index("первая фраза лишние слова вторая фраза"))
    assert any(item["kind"] == "extra_speech" for item in report["findings"])


def test_rendered_review_excludes_unavailable_sync_sources() -> None:
    timeline = FullTimeline(
        project="DEMO",
        segments=[
            TimelineSegment("zk1", "zk", "готовый закадр", 2.0),
            TimelineSegment(
                "snh1", "snh", "ненайденная реплика", 10.0,
                match_status="not_found", start_sec=2.0,
            ),
            TimelineSegment(
                "snh2", "snh", "сомнительная реплика", 10.0,
                match_status="needs_review", start_sec=12.0,
            ),
        ],
    )
    report = review_rendered_timeline(timeline, _index("готовый закадр"))
    assert report["summary"]["expected_segments"] == 1
    assert report["summary"]["unavailable_source"] == 2
    assert report["summary"]["missing"] == 0


def test_rendered_review_removes_cues_timecodes_and_tracks_splices() -> None:
    timeline = FullTimeline(
        project="DEMO",
        segments=[TimelineSegment(
            "snh1", "snh",
            "СНХ, Маликов: первая часть СКЛ вторая часть 00:08:44",
            4.0, speaker="Маликов", match_status="confirmed",
        )],
    )
    report = review_rendered_timeline(timeline, _index("первая часть вторая часть"))
    assert report["summary"]["findings"] == 0
    assert report["checks"][0]["spoken_text"] == "первая часть вторая часть"
    assert report["checks"][0]["spoken_fragments"] == ["первая часть", "вторая часть"]


def test_rendered_review_keeps_successful_checks_in_report() -> None:
    timeline = FullTimeline(
        project="DEMO",
        segments=[TimelineSegment("zk1", "zk", "точная фраза", 2.0)],
    )
    report = review_rendered_timeline(timeline, _index("точная фраза"))
    assert report["checks"][0]["status"] == "ok"
    assert report["checks"][0]["prefix_coverage"] == 1.0


def test_rendered_review_removes_life_song_title_cue() -> None:
    timeline = FullTimeline(
        project="DEMO",
        segments=[TimelineSegment(
            "zk1", "zk", "ЛАЙФ, «Где-то летом» Где-то летом звёзды нам пели песни", 4.0,
        )],
    )
    report = review_rendered_timeline(
        timeline, _index("Где-то летом звёзды нам пели песни")
    )
    assert report["checks"][0]["spoken_text"] == "Где-то летом звёзды нам пели песни"
    assert report["checks"][0]["status"] == "ok"


def test_cloud_interpolated_timestamps_use_wider_local_window() -> None:
    timeline = FullTimeline(
        project="DEMO",
        segments=[TimelineSegment("zk1", "zk", "точная длинная фраза", 3.0)],
    )
    index = WordIndex(
        source_id="render",
        words=[
            WordTimestamp("точная", 15.0, 15.4),
            WordTimestamp("длинная", 15.5, 15.9),
            WordTimestamp("фраза", 16.0, 16.4),
        ],
        metadata={
            "backend": "cloud-audio",
            "backend_metadata": {"timestamp_precision": "interpolated"},
        },
    )
    report = review_rendered_timeline(timeline, index)
    assert report["checks"][0]["status"] == "ok"
