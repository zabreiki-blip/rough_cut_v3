"""Tests for the pure word re-timing module (src/stage2/word_retiming.py)."""

from __future__ import annotations

import dataclasses
import random
import time

import pytest

from stage2.sync_models import WordTimestamp
from stage2.word_retiming import RetimeConfig, retime_words

STEP = 0.4
SYLLABLES = ["ба", "ко", "ли", "му", "не", "ро", "су", "ти", "жа", "вы"]


def _vocab(rng: random.Random, count: int) -> list[str]:
    return ["".join(rng.choice(SYLLABLES) for _ in range(4)) + "к" for _ in range(count)]


def _truth(texts: list[str], base: float = 30.0) -> list[tuple[float, float]]:
    return [(base + k * STEP, base + k * STEP + 0.3) for k in range(len(texts))]


def _local(texts, truth) -> list[WordTimestamp]:
    return [WordTimestamp(t, s, e) for t, (s, e) in zip(texts, truth)]


def _cloud(texts, truth, drift) -> list[WordTimestamp]:
    """Cloud times are the truth minus the drift: local = cloud + drift."""
    return [
        WordTimestamp(t, max(0.0, s - drift(k)), max(0.0, e - drift(k)))
        for k, (t, (s, e)) in enumerate(zip(texts, truth))
    ]


def _max_error(result, truth, indices=None) -> float:
    indices = range(len(truth)) if indices is None else indices
    return max(abs(result.words[k].start - truth[k][0]) for k in indices)


def test_uniform_shift_gives_exact_anchors() -> None:
    texts = _vocab(random.Random(1), 400)
    truth = _truth(texts)
    result = retime_words(
        _cloud(texts, truth, lambda k: -7.0), _local(texts, truth), config=RetimeConfig()
    )
    assert [w.word for w in result.words] == texts
    assert set(result.kinds) == {"exact"}
    assert result.metrics.anchor_ratio == 1.0
    assert _max_error(result, truth) < 0.01


def test_drift_changing_along_the_file() -> None:
    texts = _vocab(random.Random(2), 1800)
    truth = _truth(texts)
    drift = lambda k: 7.0 if k < 600 else (21.0 if k < 1200 else -6.0)  # noqa: E731
    result = retime_words(
        _cloud(texts, truth, lambda k: -drift(k)), _local(texts, truth), config=RetimeConfig()
    )
    assert result.metrics.anchor_ratio > 0.97
    assert _max_error(result, truth) < 0.5


def test_long_consistent_error_section_keeps_its_anchors() -> None:
    texts = _vocab(random.Random(3), 900)
    truth = _truth(texts)
    drift = lambda k: 80.0 if 300 <= k < 450 else 7.0  # noqa: E731
    result = retime_words(
        _cloud(texts, truth, lambda k: -drift(k)), _local(texts, truth), config=RetimeConfig()
    )
    assert all(result.kinds[k] == "exact" for k in range(300, 450))
    assert _max_error(result, truth) < 0.5


def test_steep_drift_ramp_from_crammed_cloud_times_keeps_anchors() -> None:
    texts = _vocab(random.Random(13), 900)
    truth = _truth(texts)
    cloud = _cloud(texts, truth, lambda k: 0.0)
    # Cloud squeezes 300 words into ~8 s: the shift grows ~0.3 s per word.
    for k in range(300, 600):
        squeezed = truth[300][0] + (k - 300) * 0.027
        cloud[k] = WordTimestamp(texts[k], squeezed, squeezed + 0.02)
    result = retime_words(cloud, _local(texts, truth), config=RetimeConfig())
    assert result.metrics.anchor_ratio > 0.98
    assert _max_error(result, truth) < 0.5


def test_fillers_only_in_cloud_are_interpolated_between_neighbours() -> None:
    texts = _vocab(random.Random(4), 200)
    truth = _truth(texts)
    local = _local(texts, truth)
    cloud_texts = list(texts)
    cloud_texts[100:100] = ["э", "э"]
    cloud_truth = _truth(cloud_texts)
    result = retime_words(
        _cloud(cloud_texts, cloud_truth, lambda k: -7.0), local, config=RetimeConfig()
    )
    assert result.kinds[100] == result.kinds[101] == "interp"
    assert result.words[99].end <= result.words[100].start
    assert result.words[101].end <= result.words[102].start
    assert result.words[100].start >= local[99].end - 1e-9
    assert result.words[101].end <= local[100].start + 1e-9


def test_digits_match_spoken_numbers() -> None:
    rng = random.Random(5)
    left, right = _vocab(rng, 60), _vocab(rng, 60)
    cloud_texts = left + ["в", "1945", "году", "27", "числа"] + right
    local_texts = (
        left
        + ["в", "тысяча", "девятьсот", "сорок", "пятом", "году", "двадцать", "седьмого", "числа"]
        + right
    )
    cloud = [WordTimestamp(t, 10.0 + k, 10.5 + k) for k, t in enumerate(cloud_texts)]
    local = [WordTimestamp(t, 17.0 + k, 17.5 + k) for k, t in enumerate(local_texts)]
    result = retime_words(cloud, local, config=RetimeConfig())
    i1945, i27 = 61, 63
    assert result.kinds[i1945] == "fuzzy" and result.kinds[i27] == "fuzzy"
    assert result.kinds[62] == "exact"
    assert abs(result.words[i1945].start - local[61].start) < 1e-6
    assert abs(result.words[i1945].end - local[64].end) < 1e-6
    assert abs(result.words[i27].start - local[66].start) < 1e-6


def test_spoken_numbers_in_cloud_match_digits_in_local() -> None:
    rng = random.Random(6)
    left, right = _vocab(rng, 40), _vocab(rng, 40)
    cloud_texts = left + ["двадцать", "седьмого"] + right
    local_texts = left + ["27-го"] + right
    cloud = [WordTimestamp(t, 5.0 + k, 5.5 + k) for k, t in enumerate(cloud_texts)]
    local = [WordTimestamp(t, 9.0 + k, 9.5 + k) for k, t in enumerate(local_texts)]
    result = retime_words(cloud, local, config=RetimeConfig())
    assert result.kinds[40] == result.kinds[41] == "fuzzy"
    assert result.words[40].start == local[40].start
    assert result.words[41].end == local[40].end


def test_lone_far_anchor_is_discarded() -> None:
    texts = _vocab(random.Random(7), 400)
    truth = _truth(texts)
    local_truth = list(truth)
    local_truth[200] = (truth[200][0] + 100.0, truth[200][1] + 100.0)
    result = retime_words(
        _cloud(texts, truth, lambda k: -7.0), _local(texts, local_truth), config=RetimeConfig()
    )
    assert result.kinds[200] == "interp"
    assert abs(result.words[200].start - truth[200][0]) < 0.5
    assert result.metrics.dropped_by_drift >= 1


def test_absolute_cap_is_a_safety_net() -> None:
    texts = _vocab(random.Random(8), 300)
    truth = _truth(texts)
    result = retime_words(
        _cloud(texts, truth, lambda k: -400.0),
        _local(texts, truth),
        config=RetimeConfig(absolute_max_shift_sec=180.0),
    )
    assert set(result.kinds) == {"unchanged"}


def test_empty_local_leaves_words_unchanged() -> None:
    texts = _vocab(random.Random(9), 30)
    cloud = _cloud(texts, _truth(texts), lambda k: 7.0)
    result = retime_words(cloud, [], config=RetimeConfig())
    assert result.words == cloud
    assert set(result.kinds) == {"unchanged"}
    assert result.metrics.anchor_ratio == 0.0
    assert retime_words([], cloud, config=RetimeConfig()).words == []


def test_edges_follow_nearest_anchor_drift() -> None:
    texts = _vocab(random.Random(10), 300)
    truth = _truth(texts)
    cloud = _cloud(texts, truth, lambda k: -7.0)
    local = _local(texts[50:250], truth[50:250])
    result = retime_words(cloud, local, config=RetimeConfig())
    assert set(result.kinds[:50]) == {"interp"} and set(result.kinds[250:]) == {"interp"}
    assert _max_error(result, truth) < 0.01


def test_output_is_monotone_without_overlap_and_min_duration() -> None:
    rng = random.Random(11)
    texts = _vocab(rng, 600)
    truth = _truth(texts)
    cloud = _cloud(texts, truth, lambda k: -7.0)
    # Noisy local: overlapping/zero-length words, some words missing or replaced.
    local = []
    for t, (s, e) in zip(texts, truth):
        if rng.random() < 0.1:
            continue
        s2 = s + rng.uniform(-0.2, 0.2)
        local.append(
            WordTimestamp(
                rng.choice(texts) if rng.random() < 0.05 else t,
                max(0.0, s2),
                max(0.0, s2) + rng.choice([0.0, 0.3, 0.9]),
            )
        )
    cfg = RetimeConfig(min_word_duration_sec=0.02)
    result = retime_words(cloud, local, config=cfg)
    previous_end = 0.0
    for word in result.words:
        assert word.start >= previous_end - 1e-9
        assert word.end - word.start >= cfg.min_word_duration_sec - 1e-9
        previous_end = word.end
    assert retime_words(cloud, local, config=cfg) == result


def test_large_input_is_fast() -> None:
    rng = random.Random(12)
    vocab = _vocab(rng, 800)
    weights = [1.0 / (rank + 1) for rank in range(len(vocab))]
    texts = rng.choices(vocab, weights, k=5000)
    truth = _truth(texts)
    cloud = _cloud(texts, truth, lambda k: -7.0 - k / 500.0)
    keep = sorted(rng.sample(range(5000), 3500))
    local = _local([texts[k] for k in keep], [truth[k] for k in keep])
    started = time.perf_counter()
    result = retime_words(cloud, local, config=RetimeConfig())
    assert time.perf_counter() - started < 2.0
    assert result.metrics.anchor_ratio > 0.5


# --------------------------------------------------------------------------
# Quality gate: do not rewrite the whole file on rare false anchors
# --------------------------------------------------------------------------


def _gate_metrics():
    texts = _vocab(random.Random(11), 300)
    truth = _truth(texts)
    result = retime_words(
        _cloud(texts, truth, lambda k: -7.0), _local(texts, truth), config=RetimeConfig()
    )
    return result.metrics


def test_quality_gate_accepts_realistic_retime() -> None:
    from stage2.word_retiming import quality_rejection

    texts = _vocab(random.Random(2), 1800)
    truth = _truth(texts)
    drift = lambda k: 7.0 if k < 600 else (21.0 if k < 1200 else -6.0)  # noqa: E731
    result = retime_words(
        _cloud(texts, truth, lambda k: -drift(k)), _local(texts, truth), config=RetimeConfig()
    )
    assert quality_rejection(result.metrics, RetimeConfig()) is None


def test_quality_gate_accepts_real_world_shape() -> None:
    from stage2.word_retiming import quality_rejection

    # ratio 0.62, p90 20 s, max 107 s were measured on real data
    metrics = dataclasses.replace(
        _gate_metrics(),
        total_words=7000,
        anchor_count=4340,
        anchor_ratio=0.62,
        dropped_by_drift=600,
        shift_p90_sec=20.0,
        shift_max_sec=107.0,
        duration_ratio=0.98,
        word_count_ratio=1.1,
    )
    assert quality_rejection(metrics, RetimeConfig()) is None


def test_other_text_with_same_rhythm_is_rejected() -> None:
    from stage2.word_retiming import quality_rejection

    cloud_texts = _vocab(random.Random(5), 400)
    local_texts = _vocab(random.Random(99), 400)
    for k in range(10, 400, 33):  # a dozen false anchors surviving the filters
        local_texts[k] = cloud_texts[k]
    truth = _truth(cloud_texts)
    result = retime_words(
        _cloud(cloud_texts, truth, lambda k: -7.0),
        _local(local_texts, truth),
        config=RetimeConfig(),
    )
    assert 0 < result.metrics.anchor_count < 0.3 * 400
    assert "anchor" in (quality_rejection(result.metrics, RetimeConfig()) or "")


def test_other_language_is_rejected() -> None:
    from stage2.word_retiming import quality_rejection

    cloud_texts = _vocab(random.Random(6), 300)
    latin = ["".join(random.Random(k).choice("abcdefgh") for _ in range(6)) for k in range(300)]
    truth = _truth(cloud_texts)
    result = retime_words(
        _cloud(cloud_texts, truth, lambda k: -7.0), _local(latin, truth), config=RetimeConfig()
    )
    assert quality_rejection(result.metrics, RetimeConfig()) is not None


def test_few_anchors_in_a_long_file_are_rejected() -> None:
    from stage2.word_retiming import quality_rejection

    cloud_texts = _vocab(random.Random(7), 600)
    truth = _truth(cloud_texts)
    local_texts = list(cloud_texts[:600:15]) + _vocab(random.Random(8), 20)  # 40 true words
    local_truth = [(30.0 + k * STEP * 15, 30.0 + k * STEP * 15 + 0.3) for k in range(60)]
    result = retime_words(
        _cloud(cloud_texts, truth, lambda k: -7.0),
        _local(local_texts, local_truth),
        config=RetimeConfig(),
    )
    assert quality_rejection(result.metrics, RetimeConfig()) is not None


def test_short_file_needs_a_proportional_number_of_anchors() -> None:
    from stage2.word_retiming import quality_rejection

    texts = _vocab(random.Random(9), 20)
    truth = _truth(texts)
    good = retime_words(
        _cloud(texts, truth, lambda k: -3.0), _local(texts, truth), config=RetimeConfig()
    )
    assert quality_rejection(good.metrics, RetimeConfig()) is None
    # 2 of 20 words is far from the 30 percent bar
    weak = dataclasses.replace(good.metrics, anchor_count=2, anchor_ratio=0.1)
    assert quality_rejection(weak, RetimeConfig()) is not None


@pytest.mark.parametrize(
    "change",
    [
        {"anchor_ratio": 0.29, "anchor_count": 87},
        {"anchor_count": 2},
        {"dropped_by_drift": 400},  # more than half of the raw anchors were dropped
        {"duration_ratio": 0.4},
        {"duration_ratio": 2.5},
        {"word_count_ratio": 0.45},
        {"word_count_ratio": 2.2},
        {"shift_p90_sec": 121.0},
    ],
)
def test_each_quality_gate_rejects(change: dict) -> None:
    from stage2.word_retiming import quality_rejection

    base = dataclasses.replace(_gate_metrics(), dropped_by_drift=0)
    assert quality_rejection(base, RetimeConfig()) is None
    assert quality_rejection(dataclasses.replace(base, **change), RetimeConfig())
