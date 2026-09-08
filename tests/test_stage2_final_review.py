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
            sequence_id="source-1", sequence_name="SYNC", start_sec=5.0,
        )],
    )
    report = review_rendered_timeline(timeline, _index("начало длинной реплики"))
    assert report["summary"]["truncated"] == 1
    correction = report["corrections"][0]
    assert correction["source_in_sec"] == 8.0
    assert correction["source_sequence_id"] == "source-1"


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
