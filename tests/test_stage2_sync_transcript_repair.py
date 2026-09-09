from stage2.sync_models import WordIndex, WordTimestamp
from stage2.sync_transcript import rescale_stretched_word_index


def test_rescale_stretched_interpolated_index_to_media_duration() -> None:
    index = WordIndex(
        source_id="kirkorov",
        words=[
            WordTimestamp("начало", 0.0, 1.0),
            WordTimestamp("конец", 492.0, 493.1),
        ],
        metadata={"backend": "cloud-audio", "timestamp_precision": "interpolated"},
    )

    scale = rescale_stretched_word_index(index, 404.64)

    assert scale == 404.64 / 493.1
    assert index.duration_sec == 404.64
    assert index.words[-1].start < index.words[-1].end
    assert index.metadata["timestamp_scale_repair"]["observed_end_sec"] == 493.1


def test_small_timestamp_rounding_overrun_is_not_rescaled() -> None:
    index = WordIndex(
        source_id="source",
        words=[WordTimestamp("слово", 59.9, 60.5)],
    )

    assert rescale_stretched_word_index(index, 60.0) is None
    assert index.duration_sec == 60.5
