from stage2.sync_models import WordIndex, WordTimestamp
from stage2.sync_transcript import reference_audio_mismatch, rescale_stretched_word_index


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


def _local_index(end: float, audio_duration: float, **extra) -> WordIndex:
    return WordIndex(
        source_id="smolnikova",
        words=[WordTimestamp("начало", 0.0, 1.0), WordTimestamp("конец", end - 1.0, end)],
        metadata={
            "backend": "faster-whisper",
            "backend_metadata": {"duration_sec": audio_duration},
            **extra,
        },
    )


def test_rescale_is_not_applied_when_transcribed_audio_is_longer_than_media() -> None:
    # The guide WAV itself lasts ~4211 s while the media lasts 3828 s: the
    # words are correct for that WAV, so linear scaling would misplace them.
    index = _local_index(4208.61, 4211.2)

    assert rescale_stretched_word_index(index, 3828.28) is None
    assert index.duration_sec == 4208.61
    assert "timestamp_scale_repair" not in index.metadata
    mismatch = reference_audio_mismatch(index, 3828.28)
    assert mismatch is not None
    assert mismatch["audio_duration_sec"] == 4211.2
    assert mismatch["media_duration_sec"] == 3828.28


def test_rescale_still_applies_to_true_cloud_stretch_with_media_length_audio() -> None:
    # Cloud answer on audio of media length, but a stretched time axis.
    index = _local_index(493.1, 404.64, backend="cloud-audio")

    assert reference_audio_mismatch(index, 404.64) is None
    assert rescale_stretched_word_index(index, 404.64) == 404.64 / 493.1


def test_index_already_scale_repaired_from_stale_audio_is_stale() -> None:
    index = _local_index(
        3828.28,
        4211.2,
        timestamp_scale_repair={
            "observed_end_sec": 4208.61,
            "duration_sec": 3828.28,
            "scale": 0.9096,
        },
    )

    assert reference_audio_mismatch(index, 3828.28) is not None


def test_scale_repaired_cloud_index_is_not_stale() -> None:
    index = _local_index(
        404.64,
        404.64,
        timestamp_scale_repair={"observed_end_sec": 493.1, "duration_sec": 404.64, "scale": 0.82},
    )

    assert reference_audio_mismatch(index, 404.64) is None


def test_index_without_audio_duration_metadata_keeps_legacy_rescale() -> None:
    index = WordIndex(source_id="s", words=[WordTimestamp("а", 490.0, 493.1)])

    assert reference_audio_mismatch(index, 404.64) is None
    assert rescale_stretched_word_index(index, 404.64) is not None
