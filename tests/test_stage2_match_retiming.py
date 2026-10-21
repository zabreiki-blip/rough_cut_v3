"""Пересчёт границ подтверждённых матчей после перерасчёта времени слов индекса."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from stage2.match_retiming import (
    RetimeMap,
    apply_retime_to_match,
    load_retime_map,
    retime_map_path,
    save_retime_map,
)
from stage2.sync_models import SyncMatch, WordTimestamp

FP = "small|v2|x"
RETIMED_AT = "2026-03-01T12:00:00+00:00"


def _rmap(shift: float = 5.0, count: int = 30) -> RetimeMap:
    old = [WordTimestamp(f"w{i}", float(i), i + 0.5) for i in range(count)]
    new = [WordTimestamp(w.word, w.start + shift, w.end + shift) for w in old]
    return RetimeMap.from_words(old, new, fingerprint=FP, retimed_at=RETIMED_AT)


def _match(
    *,
    speech: tuple[float, float] = (10.0, 12.5),
    manual: str | None = "2026-02-01T00:00:00+00:00",
    status: str = "confirmed",
    source_in: float | None = None,
    source_out: float | None = None,
    **meta: Any,
) -> SyncMatch:
    metadata: dict[str, Any] = {
        "speech_in_sec": speech[0],
        "speech_out_sec": speech[1],
        "head_padding_sec": 2.0,
        "tail_padding_sec": 1.5,
        **meta,
    }
    if manual:
        metadata["manual_updated_at"] = manual
    return SyncMatch(
        segment_id="a",
        query_text="q",
        source_id="s",
        source_in_sec=speech[0] - 2.0 if source_in is None else source_in,
        source_out_sec=speech[1] + 1.5 if source_out is None else source_out,
        status=status,  # type: ignore[arg-type]
        metadata=metadata,
    )


def _apply(match: SyncMatch, rmap: RetimeMap | None = None, **kw: Any) -> str:
    return apply_retime_to_match(match, rmap or _rmap(), now_iso="2026-04-01T00:00:00+00:00", **kw)


def test_unedited_confirmed_match_is_shifted_and_keeps_status_and_padding() -> None:
    match = _match()

    assert _apply(match) == "retimed"

    assert match.status == "confirmed"
    assert (match.source_in_sec, match.source_out_sec) == (13.0, 19.0)  # 15-2, 17.5+1.5
    assert match.metadata["speech_in_sec"] == 15.0 and match.metadata["speech_out_sec"] == 17.5
    assert match.metadata["retimed_from"] == {
        "source_in_sec": 8.0,
        "source_out_sec": 14.0,
        "speech_in_sec": 10.0,
        "speech_out_sec": 12.5,
        "fingerprint": FP,
    }
    assert match.metadata["retimed_at"] == "2026-04-01T00:00:00+00:00"
    assert "retime_pending_review" not in match.metadata
    assert match.metadata["manual_updated_at"] == "2026-02-01T00:00:00+00:00"


def test_guide_offset_and_source_duration_are_respected() -> None:
    # speech_in включает guide_offset: слово 10.0 -> speech_in 110.0.
    match = _match(speech=(110.0, 112.5))

    assert _apply(match, guide_offset_sec=100.0, duration_sec=116.0) == "retimed"

    assert match.metadata["speech_in_sec"] == 115.0
    assert (match.source_in_sec, match.source_out_sec) == (113.0, 116.0)  # out зажат


def test_rounded_confirmation_values_still_count_as_unedited() -> None:
    match = _match(source_in=8.0004, source_out=14.0003)
    assert _apply(match) == "retimed"


def test_manual_boundary_edit_is_not_touched_and_marked_for_review() -> None:
    match = _match(source_in=8.4)  # сдвинули in на 0.4 с

    assert _apply(match) == "review"

    assert (match.source_in_sec, match.source_out_sec) == (8.4, 14.0)
    assert match.status == "confirmed"
    assert match.metadata["retime_pending_review"] is True
    assert "retimed_from" not in match.metadata


def test_chosen_alternative_is_not_touched_and_marked_for_review() -> None:
    match = _match(source_in=21.0, source_out=25.0)  # другая альтернатива
    assert _apply(match) == "review"
    assert match.source_in_sec == 21.0 and match.metadata["retime_pending_review"] is True


def test_ambiguous_span_is_not_touched_and_marked_for_review() -> None:
    match = _match(speech=(10.3, 12.5), source_in=8.3)  # такого слова нет
    assert _apply(match) == "review"
    assert match.source_in_sec == 8.3 and match.metadata["retime_pending_review"] is True


def test_missing_speech_metadata_is_marked_for_review() -> None:
    match = _match()
    del match.metadata["speech_in_sec"]
    assert _apply(match) == "review"


def test_second_application_is_a_noop() -> None:
    match = _match()
    _apply(match)
    snapshot = (match.source_in_sec, match.source_out_sec, dict(match.metadata))

    assert _apply(match) == "skip"

    assert (match.source_in_sec, match.source_out_sec, match.metadata) == snapshot


def test_review_mark_is_not_repeated_for_same_retime() -> None:
    match = _match(source_in=8.4)
    _apply(match)
    snapshot = dict(match.metadata)
    assert _apply(match) == "skip"
    assert match.metadata == snapshot


def test_match_confirmed_after_retime_is_not_touched() -> None:
    match = _match(manual="2026-03-02T00:00:00+00:00")
    assert _apply(match) == "skip"
    assert (match.source_in_sec, match.source_out_sec) == (8.0, 14.0)
    assert "retimed_from" not in match.metadata and "retime_pending_review" not in match.metadata


def test_unparseable_manual_timestamp_is_not_touched() -> None:
    match = _match(manual="вчера")
    assert _apply(match) == "skip"
    assert match.source_in_sec == 8.0


def test_legacy_confirmed_without_manual_timestamp_is_retimed() -> None:
    match = _match(manual=None)
    assert _apply(match) == "retimed"
    assert match.source_in_sec == 13.0


def test_unconfirmed_match_is_skipped() -> None:
    match = _match(status="needs_review")
    assert _apply(match) == "skip"
    assert match.source_in_sec == 8.0


def test_new_retime_fingerprint_is_applied_again_on_current_coordinates() -> None:
    match = _match()
    _apply(match)
    second = RetimeMap.from_words(
        [WordTimestamp(f"w{i}", i + 5.0, i + 5.5) for i in range(30)],
        [WordTimestamp(f"w{i}", i + 6.0, i + 6.5) for i in range(30)],
        fingerprint="other",
        retimed_at="2026-05-01T00:00:00+00:00",
    )
    assert _apply(match, second) == "retimed"
    assert match.source_in_sec == 14.0 and match.metadata["retimed_from"]["fingerprint"] == "other"


def test_retime_map_roundtrip_is_atomic_and_validates_version(tmp_path: Path) -> None:
    path = retime_map_path(tmp_path / "a.words.json", "a")
    assert path.name == "a.retime_map.json"
    rmap = _rmap()

    save_retime_map(path, rmap)

    loaded = load_retime_map(path)
    assert loaded is not None and loaded.fingerprint == FP and loaded.retimed_at == RETIMED_AT
    assert loaded.old_starts == rmap.old_starts and loaded.new_ends == rmap.new_ends
    assert list(tmp_path.glob("*.tmp")) == []
    path.write_text('{"version": 99}', encoding="utf-8")
    assert load_retime_map(path) is None
    path.write_text("не json", encoding="utf-8")
    assert load_retime_map(path) is None
    assert load_retime_map(tmp_path / "missing.json") is None


def test_map_consistency_with_index_marker() -> None:
    rmap = _rmap()
    marker = {"fingerprint": FP, "retimed_at": RETIMED_AT}
    index = SimpleNamespace(metadata={"backend_metadata": {"retiming": marker}})
    assert rmap.matches_index(index)
    marker["retimed_at"] = "2026-03-02T00:00:00+00:00"
    assert not rmap.matches_index(index)
    assert not rmap.matches_index(SimpleNamespace(metadata={}))
