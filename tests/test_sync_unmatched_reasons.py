from stage2.sync_matcher import SyncMatcher
from stage2.sync_models import AlignmentRequest, SyncSource, WordIndex


def test_short_query_reports_its_reason() -> None:
    match = SyncMatcher([], []).match(
        AlignmentRequest(segment_id="snh1", text="Тебя", speaker="Краснова")
    )
    assert match.status == "not_found"
    assert match.metadata["unmatched_reason"] == "query_too_short"


def test_named_speaker_with_no_eligible_source_reports_its_reason() -> None:
    source = SyncSource(
        id="vasilieva",
        speaker="Васильева",
        premiere_sequence="Васильева",
        duration_sec=10,
    )
    index = WordIndex(source_id="vasilieva", words=[])
    match = SyncMatcher([source], [index]).match(
        AlignmentRequest(
            segment_id="snh2",
            text="Мы делали кино, чтобы показать живых людей.",
            speaker="Сторожева",
        )
    )
    assert match.status == "not_found"
    assert match.metadata["unmatched_reason"] == "no_eligible_source"
