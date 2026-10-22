"""Резервный поиск по local index и проверки правдоподобия СНХ-совпадений."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import api
from stage2.index_retiming import LOCAL_WORDS_VERSION, local_words_path
from stage2.match_verification import load_local_word_index
from stage2.sync_matcher import MatcherConfig, SyncMatcher
from stage2.sync_models import AlignmentRequest, SyncSource, WordIndex, WordTimestamp
from sync_match_phase import SyncMatchContext, run_match_phase

PHRASE = (
    "мы проверили ящик внимательно и увидели надпись аккумуляторы после этого "
    "отправили его дальше по цепочке без задержек и лишних вопросов"
)
FILLER = "погода сегодня стояла удивительно тёплая море было спокойным а чайки кружили над берегом"


def _words(
    text: str, start: float, step: float = 0.4, duration: float | None = None
) -> list[WordTimestamp]:
    dur = step if duration is None else duration
    return [
        WordTimestamp(word, start + i * step, start + i * step + dur)
        for i, word in enumerate(text.split())
    ]


def _index(source_id: str, words: list[WordTimestamp], **metadata: Any) -> WordIndex:
    return WordIndex(source_id=source_id, words=words, metadata=dict(metadata))


def _source(source_id: str = "s1", **kwargs: Any) -> SyncSource:
    return SyncSource(id=source_id, speaker="Иван Петров", premiere_sequence="SEQ", **kwargs)


def _request(text: str = PHRASE, approx: float | None = 100.0) -> AlignmentRequest:
    return AlignmentRequest(segment_id="seg", text=text, approximate_timecode=approx)


def _local(words: list[WordTimestamp], source_id: str = "s1") -> WordIndex:
    return _index(source_id, words, timestamp_precision="word", index_kind="local_whisper")


def _primary_with_filler_only() -> WordIndex:
    return _index("s1", _words(FILLER, 100.0))


# ----------------------------- A. резервный поиск ------------------------------


def test_fallback_finds_phrase_absent_from_primary_but_present_in_local() -> None:
    primary = _primary_with_filler_only()
    local = _local(_words(PHRASE, 120.0))

    base = SyncMatcher([_source()], [primary]).match(_request())
    assert base.status == "not_found"

    result = SyncMatcher([_source()], [primary], local_indexes=[local]).match(_request())

    assert result.status == "needs_review"
    assert result.strategy == "local_index_fallback"
    assert result.source_id == "s1"
    assert result.confidence >= 0.5
    assert result.metadata["review_reason"] == "local_index_fallback"
    assert result.metadata["index_kind"] == "local_whisper"
    assert result.metadata["primary_unmatched_reason"] == base.metadata["unmatched_reason"]
    assert result.metadata["primary_confidence"] == base.confidence
    assert result.metadata["speech_in_sec"] == pytest.approx(120.0, abs=0.01)
    assert result.source_in_sec == pytest.approx(118.0, abs=0.01)  # head padding 2 s


def test_fallback_can_be_auto_only_when_explicitly_enabled() -> None:
    primary = _primary_with_filler_only()
    local = _local(_words(PHRASE, 120.0))
    config = MatcherConfig(local_fallback_auto=True)
    result = SyncMatcher([_source()], [primary], config, local_indexes=[local]).match(_request())
    assert result.strategy == "local_index_fallback"
    assert result.status == "auto_matched"
    assert MatcherConfig().local_fallback_auto is False


def test_without_local_index_not_found_is_unchanged() -> None:
    primary = _primary_with_filler_only()
    plain = SyncMatcher([_source()], [primary]).match(_request())
    for local_indexes in (None, []):
        result = SyncMatcher([_source()], [primary], local_indexes=local_indexes).match(_request())
        assert result.to_dict() == plain.to_dict()
    assert "local_fallback_tried" not in plain.metadata


def test_local_index_of_other_source_is_not_used() -> None:
    primary = _primary_with_filler_only()
    other = _local(_words(PHRASE, 120.0), source_id="other")
    result = SyncMatcher([_source()], [primary], local_indexes=[other]).match(_request())
    assert result.status == "not_found"
    assert "local_fallback_tried" not in result.metadata


def test_fallback_miss_keeps_not_found_and_reports_attempt() -> None:
    primary = _primary_with_filler_only()
    local = _local(_words(FILLER, 120.0))
    result = SyncMatcher([_source()], [primary], local_indexes=[local]).match(_request())
    assert result.status == "not_found"
    assert result.metadata["local_fallback_tried"] is True
    assert 0.0 <= result.metadata["local_best_confidence"] < 0.5
    assert "unmatched_reason" in result.metadata


def test_fallback_keeps_primary_alternatives_after_local_best() -> None:
    partial = " ".join(PHRASE.split()[:6]) + " " + FILLER
    primary = _index("s1", _words(partial, 100.0))
    local = _local(_words(PHRASE, 300.0))
    result = SyncMatcher([_source()], [primary], local_indexes=[local]).match(
        _request(approx=None)
    )
    assert result.strategy == "local_index_fallback"
    assert result.alternatives
    assert len(result.alternatives) <= MatcherConfig().max_alternatives
    # локальный лучший не дублируется среди альтернатив
    assert all(
        not (alt.source_in_sec == result.source_in_sec and alt.source_out_sec == result.source_out_sec)
        for alt in result.alternatives
    )


def test_fallback_respects_speaker_filter_of_primary_eligible_sources() -> None:
    # A (Иван Петров) has no local index; B (no speaker metadata) has one with the phrase.
    # The speaker filter must not be re-run on the reduced set and pick B.
    source_a = _source("a")
    source_b = SyncSource(id="b", speaker="", premiere_sequence="SEQ_B")
    primary_a = _index("a", _words(FILLER, 100.0))
    primary_b = _index("b", _words(FILLER, 100.0))
    local_b = _local(_words(PHRASE, 120.0), source_id="b")
    request = AlignmentRequest(
        segment_id="seg", text=PHRASE, speaker="Иван Петров", approximate_timecode=100.0
    )
    result = SyncMatcher(
        [source_a, source_b], [primary_a, primary_b], local_indexes=[local_b]
    ).match(request)
    assert result.status == "not_found"
    assert result.strategy != "local_index_fallback"
    assert "local_fallback_tried" not in result.metadata


def test_fallback_still_works_for_eligible_source_with_local_index() -> None:
    source_a = _source("a")
    source_b = SyncSource(id="b", speaker="", premiere_sequence="SEQ_B")
    primary = [_index("a", _words(FILLER, 100.0)), _index("b", _words(FILLER, 100.0))]
    local_a = _local(_words(PHRASE, 120.0), source_id="a")
    request = AlignmentRequest(
        segment_id="seg", text=PHRASE, speaker="Иван Петров", approximate_timecode=100.0
    )
    result = SyncMatcher([source_a, source_b], primary, local_indexes=[local_a]).match(request)
    assert result.strategy == "local_index_fallback"
    assert result.source_id == "a"


# ------------------------ B. правдоподобие времени ----------------------------


def test_collapsed_twenty_millisecond_words_go_to_review() -> None:
    words = _words(PHRASE, 235.27, step=0.02)  # 20 слов за 0.4 с
    primary = _index("s1", words, timestamp_precision="retimed")
    request = _request(approx=235.0)

    clean = SyncMatcher([_source()], [_index("s1", _words(PHRASE, 235.27))]).match(request)
    assert clean.status == "auto_matched"

    result = SyncMatcher([_source()], [primary]).match(request)

    assert result.status == "needs_review"
    flags = result.metadata["plausibility_flags"]
    assert "collapsed_word_times" in flags and "implausible_word_rate" in flags
    assert result.metadata["timing_reliable"] is False
    assert result.metadata["review_reason"] == flags[0]


def test_dense_words_over_rate_limit_are_flagged_without_collapse() -> None:
    # 20 слов по 0.1 с = 10 слов/с: длительность слова выше порога схлопывания
    primary = _index("s1", _words(PHRASE, 50.0, step=0.1))
    result = SyncMatcher([_source()], [primary]).match(_request(approx=50.0))
    assert result.status == "needs_review"
    assert result.metadata["plausibility_flags"] == ["implausible_word_rate"]
    assert result.metadata["timing_reliable"] is False


def test_tail_truncated_vs_local_adds_local_range_first() -> None:
    head17 = " ".join(PHRASE.split()[:17])
    primary = _index("s1", _words(head17, 277.3) + _words(FILLER, 284.5))
    local = _local(_words(PHRASE, 277.3))
    request = _request(approx=277.0)

    assert SyncMatcher([_source()], [primary]).match(request).status == "auto_matched"

    result = SyncMatcher([_source()], [primary], local_indexes=[local]).match(request)

    assert result.status == "needs_review"
    assert "tail_truncated_vs_local" in result.metadata["plausibility_flags"]
    assert result.metadata["review_reason"] == result.metadata["plausibility_flags"][0]
    assert result.metadata["local_speech_in_sec"] == pytest.approx(277.3, abs=0.01)
    assert result.metadata["local_speech_out_sec"] == pytest.approx(277.3 + 20 * 0.4, abs=0.01)
    assert result.metadata["local_confidence"] >= 0.9
    first = result.alternatives[0]
    assert first.source_id == "s1"
    assert first.source_out_sec == pytest.approx(277.3 + 20 * 0.4 + 1.5, abs=0.01)
    assert first.source_out_sec > result.source_out_sec
    assert result.metadata["timing_reliable"] is True  # флаги 1/2 не сработали


def test_head_truncated_vs_local() -> None:
    tail17 = " ".join(PHRASE.split()[3:])
    primary = _index("s1", _words(tail17, 101.2))
    local = _local(_words(PHRASE, 100.0))
    result = SyncMatcher([_source()], [primary], local_indexes=[local]).match(_request())
    assert result.status == "needs_review"
    assert "head_truncated_vs_local" in result.metadata["plausibility_flags"]
    assert result.alternatives[0].source_in_sec == pytest.approx(98.0, abs=0.01)


def test_range_disagrees_with_local_when_ranges_do_not_overlap() -> None:
    primary = _index("s1", _words(PHRASE, 100.0))
    local = _local(_words(PHRASE, 150.0))
    result = SyncMatcher([_source()], [primary], local_indexes=[local]).match(_request())
    assert result.status == "needs_review"
    assert "range_disagrees_with_local" in result.metadata["plausibility_flags"]


def test_local_text_mismatch_when_local_has_other_speech_near_range() -> None:
    primary = _index("s1", _words(PHRASE, 100.0))
    local = _local(_words(FILLER, 100.0) + _words(FILLER, 130.0))
    result = SyncMatcher([_source()], [primary], local_indexes=[local]).match(_request())
    assert result.status == "needs_review"
    assert result.metadata["plausibility_flags"] == ["local_text_mismatch"]


def test_missing_local_words_near_range_is_not_a_mismatch() -> None:
    primary = _index("s1", _words(PHRASE, 100.0))
    local = _local(_words(FILLER, 2000.0))  # вне окна поиска: данных нет
    result = SyncMatcher([_source()], [primary], local_indexes=[local]).match(_request())
    assert result.status == "auto_matched"
    assert "plausibility_flags" not in result.metadata


def test_clean_consistent_match_stays_auto_matched() -> None:
    primary = _index("s1", _words(PHRASE, 100.0), timestamp_precision="retimed")
    # локальная расшифровка с небольшим дрейфом границ внутри допуска
    local = _local(_words(PHRASE, 100.15, step=0.39))
    request = _request()

    baseline = SyncMatcher([_source()], [primary]).match(request)
    result = SyncMatcher([_source()], [primary], local_indexes=[local]).match(request)

    assert result.status == "auto_matched"
    assert "plausibility_flags" not in result.metadata
    assert result.to_dict() == baseline.to_dict()


def test_guide_offset_does_not_break_local_consistency() -> None:
    source = _source(guide_offset_sec=50.0)
    primary = _index("s1", _words(PHRASE, 100.0))
    local = _local(_words(PHRASE, 100.0))
    request = _request(approx=150.0)
    result = SyncMatcher([source], [primary], local_indexes=[local]).match(request)
    assert result.status == "auto_matched"
    assert "plausibility_flags" not in result.metadata


def test_zero_max_alternatives_does_not_add_local_alternative() -> None:
    head17 = " ".join(PHRASE.split()[:17])
    primary = _index("s1", _words(head17, 277.3) + _words(FILLER, 284.5))
    local = _local(_words(PHRASE, 277.3))
    config = MatcherConfig(max_alternatives=0)
    result = SyncMatcher([_source()], [primary], config, local_indexes=[local]).match(
        _request(approx=277.0)
    )
    assert "tail_truncated_vs_local" in result.metadata["plausibility_flags"]
    assert result.alternatives == []


def test_words_in_range_tolerates_unsorted_words_in_index() -> None:
    words = _words(PHRASE, 100.0)
    index = _index("s1", words)
    matcher = SyncMatcher([_source()], [index])
    expected = [w.word for w in matcher._words_in_range("s1", 100.0, 108.0)]
    # words mutated out of order after construction (WordIndex sorts only once)
    index.words[:] = list(reversed(words))
    matcher = SyncMatcher([_source()], [index])
    assert [w.word for w in matcher._words_in_range("s1", 100.0, 108.0)] == expected


def test_config_validation_of_new_options() -> None:
    for kwargs in (
        {"max_words_per_sec": 0.0},
        {"collapsed_word_sec": -1.0},
        {"local_boundary_tolerance_sec": -0.1},
    ):
        with pytest.raises(ValueError):
            MatcherConfig(**kwargs)


# ------------------------------ загрузчик -------------------------------------


def _write_local(
    index_path: Path, words: list[dict[str, Any]] | None, **overrides: Any
) -> Path:
    payload: dict[str, Any] = {
        "version": LOCAL_WORDS_VERSION,
        "model": "small",
        "language": "ru",
        "audio": {"size": 10, "mtime_ns": 1, "sample_sha256": "abc"},
        "words": words
        if words is not None
        else [{"word": "привет", "start": 1.0, "end": 1.4}, {"word": "мир", "start": 1.5, "end": 1.9}],
    }
    payload.update(overrides)
    path = local_words_path(index_path, "s1")
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_loader_reads_local_words(tmp_path: Path) -> None:
    index_path = tmp_path / "s1.words.json"
    _write_local(index_path, None)
    index = load_local_word_index(index_path, "s1")
    assert index is not None
    assert index.source_id == "s1"
    assert [w.word for w in index.words] == ["привет", "мир"]
    assert index.metadata["timestamp_precision"] == "word"
    assert index.metadata["index_kind"] == "local_whisper"


def test_loader_returns_none_for_missing_bad_or_empty_files(tmp_path: Path) -> None:
    index_path = tmp_path / "s1.words.json"
    assert load_local_word_index(index_path, "s1") is None
    path = _write_local(index_path, None, version=LOCAL_WORDS_VERSION + 1)
    assert load_local_word_index(index_path, "s1") is None
    _write_local(index_path, [])
    assert load_local_word_index(index_path, "s1") is None
    path.write_text("{not json", encoding="utf-8")
    assert load_local_word_index(index_path, "s1") is None


def test_loader_rejects_local_words_of_another_audio(tmp_path: Path) -> None:
    index_path = tmp_path / "s1.words.json"
    _write_local(index_path, None)

    def primary(sha: str) -> WordIndex:
        cache = {"version": 1, "audio": {"size": 10, "mtime_ns": 99, "sample_sha256": sha}}
        return _index("s1", _words(FILLER, 0.0), transcription_cache=cache)

    assert load_local_word_index(index_path, "s1", primary_index=primary("abc")) is not None
    assert load_local_word_index(index_path, "s1", primary_index=primary("zzz")) is None
    # без сигнатуры у основного индекса файл принимается
    plain = _index("s1", _words(FILLER, 0.0))
    assert load_local_word_index(index_path, "s1", primary_index=plain) is not None


# ------------------------ отпечаток и подключение фазы ------------------------


def _fingerprint_arguments(**extra: Any) -> dict[str, Any]:
    return {
        "units": [
            {
                "segment_id": "block-1",
                "parent_block_uid": "block-1",
                "text": "точная цитата",
                "speaker": "A",
                "timecode_start": "00:10",
            }
        ],
        "sources": [SyncSource(id="s1", speaker="A", premiere_sequence="Seq")],
        "indexes": [_index("s1", [WordTimestamp("цитата", 10.0, 10.5)], generated_at="stable")],
        "normalization_revision": 3,
        "timecode_mode": "relative",
        "search_window_sec": 180.0,
        "llm_enabled": False,
        "llm_model": "",
        "llm_threshold": 0.8,
        "llm_limit": 30,
        **extra,
    }


def test_fingerprint_changes_when_local_index_appears_or_changes() -> None:
    without = api._sync_matching_fingerprint(**_fingerprint_arguments())
    assert without == api._sync_matching_fingerprint(**_fingerprint_arguments(local_indexes=[]))

    local = _local([WordTimestamp("цитата", 10.0, 10.5)])
    with_local = api._sync_matching_fingerprint(**_fingerprint_arguments(local_indexes=[local]))
    assert with_local != without
    assert with_local == api._sync_matching_fingerprint(**_fingerprint_arguments(local_indexes=[local]))

    other = _local([WordTimestamp("другая", 10.0, 10.5)])
    assert (
        api._sync_matching_fingerprint(**_fingerprint_arguments(local_indexes=[other]))
        != with_local
    )


def test_match_phase_loads_local_indexes_and_passes_them_to_matcher(tmp_path: Path) -> None:
    index_path = tmp_path / "s1.words.json"
    _write_local(index_path, None)
    captured: dict[str, Any] = {}

    class _Matcher:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured["args"] = args
            captured["kwargs"] = kwargs

        def match(self, request: Any) -> Any:  # pragma: no cover - нет сегментов
            raise AssertionError("no segments expected")

    def fingerprint(**kwargs: Any) -> str:
        captured["fingerprint_kwargs"] = kwargs
        return "fp"

    ctx = _minimal_context(
        matcher_cls=_Matcher,
        matching_fingerprint=fingerprint,
        word_index_path=lambda _paths, sid: tmp_path / f"{sid}.words.json",
    )
    source = SimpleNamespace(id="s1", speaker_names=[])
    run_match_phase(
        ctx,
        [source],
        [SimpleNamespace(source_id="s1", metadata={})],
        cost_tracker=_Tracker(),  # type: ignore[arg-type]
        checkpoint=_checkpoint(),
    )

    local_indexes = captured["kwargs"]["local_indexes"]
    assert [item.source_id for item in local_indexes] == ["s1"]
    assert captured["fingerprint_kwargs"]["local_indexes"] == local_indexes


def test_match_phase_does_not_pass_local_indexes_when_absent(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class _Matcher:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured["kwargs"] = kwargs

    ctx = _minimal_context(
        matcher_cls=_Matcher,
        word_index_path=lambda _paths, sid: tmp_path / f"{sid}.words.json",
    )
    run_match_phase(
        ctx,
        [SimpleNamespace(id="s1", speaker_names=[])],
        [SimpleNamespace(source_id="s1", metadata={})],
        cost_tracker=_Tracker(),  # type: ignore[arg-type]
        checkpoint=_checkpoint(),
    )
    assert captured["kwargs"] == {}


class _Tracker:
    llm_budget = None

    def init_llm_budget(self, environ: Any) -> bool:
        return True

    def llm_cost_fields(self) -> dict[str, Any]:
        return {}


def _checkpoint() -> Any:
    from sync_match_phase import SyncMatchCheckpoint

    return SyncMatchCheckpoint()


class _Store:
    def list(self) -> list[Any]:
        return []

    def replace_all(self, items: list[Any]) -> None:
        pass


def _minimal_context(**overrides: Any) -> SyncMatchContext:
    import logging
    import threading

    values: dict[str, Any] = {
        "job_id": "job-1",
        "paths": {"normalized": "norm"},
        "match_store": _Store(),
        "timecode_mode": "none",
        "rematch_confirmed": False,
        "search_window_sec": 60.0,
        "job_llm_cost_env": {},
        "ensure_not_cancelled": lambda: None,
        "should_persist_progress": lambda force=False: False,
        "semantic_usage_callback": lambda _event: None,
        "logger": logging.getLogger("test_sync_match_verification"),
        "getenv": lambda name, default: default,
        "environ": lambda: {},
        "data_lock": threading.Lock(),
        "update_job": lambda *a, **kw: None,
        "utc_now_iso": lambda: "2026-01-01T00:00:00+00:00",
        "load_normalization": lambda _path: {"revision": 3, "blocks": []},
        "alignment_units": lambda blocks, **kw: [],
        "matcher_cls": lambda *a, **kw: None,
        "matcher_config_cls": lambda **kw: kw,
        "get_settings": lambda: SimpleNamespace(openrouter_api_key="", openrouter_model="m"),
        "semantic_review_candidates": lambda match: [],
        "normalize_text": lambda text: list(str(text)),
        "review_candidates_with_llm": lambda **kw: None,
        "apply_semantic_review": lambda match, *a, **kw: match,
        "alignment_request_cls": lambda **kw: SimpleNamespace(**kw),
        "matching_fingerprint": lambda **kw: "fp",
        "sync_summary": lambda matches: {"matched": len(matches)},
    }
    values.update(overrides)
    return SyncMatchContext(**values)
