"""Локальный ремонт дыр закэшированного облачного индекса (word-level)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stage2 import local_hole_repair
from stage2.cached_index_repair import (
    repair_cached_index_holes,
    settings_fingerprint,
)
from stage2.sync_models import WordIndex, WordTimestamp

NOW = lambda: "2026-10-21T10:00:00+00:00"  # noqa: E731


def make_index(
    holes: list[list[float]],
    *,
    extra_words: list[tuple[str, float, float]] | None = None,
    skip: tuple[float, float] = (40.0, 60.0),
    coverage_extra: dict[str, Any] | None = None,
    metadata_extra: dict[str, Any] | None = None,
) -> WordIndex:
    words = [
        WordTimestamp(f"c{i}", float(i), float(i) + 0.5)
        for i in range(120)
        if not (skip[0] <= i < skip[1])
    ]
    words += [WordTimestamp(w, s, e) for w, s, e in extra_words or []]
    coverage = {
        "holes_detected": len(holes),
        "holes_repaired": 0,
        "unrecovered_sec": sum(b - a for a, b in holes),
        "unrecovered_ranges": holes,
        "words_per_minute": 100.0,
        **(coverage_extra or {}),
    }
    return WordIndex(
        "src",
        words,
        metadata={
            "backend": "cloud-audio",
            "timestamp_precision": "interpolated",
            "backend_metadata": {"coverage": coverage, "cost_usd": 0.5},
            **(metadata_extra or {}),
        },
    )


@pytest.fixture
def audio(tmp_path: Path) -> Path:
    path = tmp_path / "a.wav"
    path.write_bytes(b"audio")
    return path


class Engine:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, speech: float = 20.0) -> None:
        self.clips: list[tuple[float, float]] = []
        self.calls: list[tuple[str, str]] = []
        self.words: dict[float, list[tuple[str, float, float]]] = {}
        self.error: Exception | None = None

        class Clip:
            path = Path("clip.wav")
            samples = None

            def cleanup(self) -> None:
                pass

        def load_clip(_p, start, end, **_k):
            self.clips.append((start, end))
            return Clip()

        monkeypatch.setattr(local_hole_repair, "_load_clip", load_clip)
        monkeypatch.setattr(local_hole_repair, "_speech_seconds", lambda *_a: speech)

    def transcribe(self, _path, model, language, progress_callback=None):
        self.calls.append((model, language))
        if self.error is not None:
            raise self.error
        clip_start = self.clips[-1][0]
        # слова задаются в абсолютном времени, как их видит вызывающий
        hole_start = min(k for k in self.words if k >= clip_start)
        return [
            {"word": w, "start": s - clip_start, "end": e - clip_start}
            for w, s, e in self.words[hole_start]
        ]


def local_words(start: float, count: int, prefix: str = "l") -> list[tuple[str, float, float]]:
    return [(f"{prefix}{i}", start + i, start + i + 0.5) for i in range(count)]


def run(index: WordIndex, audio: Path, engine: Engine, **kwargs: Any):
    return repair_cached_index_holes(
        index,
        audio,
        language="ru",
        now_iso=NOW,
        transcribe=engine.transcribe,
        settings=kwargs.pop("settings", local_hole_repair.LocalRepairSettings()),
        **kwargs,
    )


def coverage_of(index: WordIndex) -> dict[str, Any]:
    return index.metadata["backend_metadata"]["coverage"]


def test_repairs_hole_with_absolute_times_and_updates_coverage(
    monkeypatch: pytest.MonkeyPatch, audio: Path
) -> None:
    index = make_index([[40.0, 60.0]])
    engine = Engine(monkeypatch)
    engine.words[37.0] = local_words(40.5, 15)

    result = run(index, audio, engine)

    assert result.status == "repaired" and result.changed
    inside = [w for w in index.words if 40 <= w.start < 60]
    assert [w.word for w in inside] == [f"l{i}" for i in range(15)]
    assert inside[0].start == 40.5
    assert [w.start for w in index.words] == sorted(w.start for w in index.words)
    cov = coverage_of(index)
    assert cov["unrecovered_ranges"] == [] and cov["unrecovered_sec"] == 0
    assert cov["local_repaired_ranges"] == [[40.0, 60.0]]
    assert cov["holes_repaired_local"] == 1
    marker = cov["local_repair"]
    assert marker["source"] == "cached-index" and marker["model"] == "medium"
    assert marker["fingerprint"] == settings_fingerprint(local_hole_repair.LocalRepairSettings())
    assert marker["repaired_at"] == NOW() and marker["errors"] == []
    assert not marker["stopped"]
    assert index.metadata["timestamp_precision"] == "interpolated"
    assert index.metadata["backend_metadata"]["cost_usd"] == 0.5


def test_sparse_cloud_words_in_hole_replaced_only_when_local_has_more(
    monkeypatch: pytest.MonkeyPatch, audio: Path
) -> None:
    sparse = [("cloud_a", 45.0, 45.5), ("cloud_b", 50.0, 50.5), ("cloud_c", 55.0, 55.5)]
    index = make_index([[40.0, 60.0]], extra_words=sparse)
    engine = Engine(monkeypatch)
    engine.words[37.0] = local_words(41.0, 3)  # не больше, чем было
    result = run(index, audio, engine)
    # слова не тронуты; меняется только отметка «ремонт выполнен»
    assert result.status == "unchanged" and result.changed
    assert {"cloud_a", "cloud_b", "cloud_c"} <= {w.word for w in index.words}

    engine.words[37.0] = local_words(41.0, 8)
    other = local_hole_repair.LocalRepairSettings(model="small")  # другой отпечаток
    result = run(index, audio, engine, settings=other)
    assert result.status == "repaired"
    words = {w.word for w in index.words}
    assert not words & {"cloud_a", "cloud_b", "cloud_c"}
    assert {f"l{i}" for i in range(8)} <= words


def test_edge_words_duplicating_neighbours_are_dropped(
    monkeypatch: pytest.MonkeyPatch, audio: Path
) -> None:
    index = make_index([[40.0, 60.0]])
    engine = Engine(monkeypatch)
    # до дыры в индексе c38 c39; после — c60 c61
    dup_head = [("c38", 40.1, 40.4), ("c39", 40.5, 40.9)]
    dup_tail = [("c60", 58.0, 58.4), ("c61", 58.5, 58.9)]
    engine.words[37.0] = dup_head + local_words(42.0, 12) + dup_tail

    run(index, audio, engine)

    inside = [w.word for w in index.words if 40 <= w.start < 60]
    assert inside == [f"l{i}" for i in range(12)]
    assert [w.word for w in index.words].count("c38") == 1
    assert [w.word for w in index.words].count("c60") == 1


def test_silence_goes_to_silent_ranges(monkeypatch: pytest.MonkeyPatch, audio: Path) -> None:
    index = make_index([[40.0, 60.0]])
    before = list(index.words)
    engine = Engine(monkeypatch, speech=0.2)

    result = run(index, audio, engine)

    assert result.status == "repaired" and result.silent == 1 and result.repaired == 0
    cov = coverage_of(index)
    assert cov["silent_ranges"] == [[40.0, 60.0]] and cov["silent_sec"] == 20.0
    assert cov["unrecovered_ranges"] == [] and cov["unrecovered_sec"] == 0
    assert cov["local_repaired_ranges"] == [] and cov["holes_repaired_local"] == 0
    assert index.words == before
    assert engine.calls == []


def test_engine_error_leaves_index_untouched_with_reason(
    monkeypatch: pytest.MonkeyPatch, audio: Path
) -> None:
    index = make_index([[40.0, 60.0]])
    snapshot = repr(index.to_dict())
    engine = Engine(monkeypatch)
    engine.error = RuntimeError("model load failed")

    result = run(index, audio, engine)

    assert result.status == "failed" and not result.changed
    assert "model load failed" in result.reason
    assert repr(index.to_dict()) == snapshot


def test_same_settings_marker_prevents_repeat_and_changed_model_repairs_rest(
    monkeypatch: pytest.MonkeyPatch, audio: Path
) -> None:
    index = make_index([[40.0, 60.0], [80.0, 100.0]], skip=(40.0, 60.0))
    index.words = [w for w in index.words if not 80 <= w.start < 100]
    engine = Engine(monkeypatch)
    engine.words[37.0] = local_words(40.5, 15)
    engine.words[77.0] = []  # вторая дыра: слов нет -> остаётся
    first = run(index, audio, engine)
    assert first.status == "repaired" and first.remaining == 1
    assert coverage_of(index)["unrecovered_ranges"] == [[80.0, 100.0]]
    calls_after_first = len(engine.calls)

    again = run(index, audio, engine)
    assert again.status == "already_repaired" and not again.changed
    assert len(engine.calls) == calls_after_first

    engine.words[77.0] = local_words(81.0, 14, "s")
    other = local_hole_repair.LocalRepairSettings(model="small")
    third = run(index, audio, engine, settings=other)
    assert third.status == "repaired"
    assert engine.clips[-1] == (77.0, 103.0)  # только оставшаяся дыра
    assert engine.calls[-1][0] == "small"
    cov = coverage_of(index)
    assert cov["unrecovered_ranges"] == []
    assert cov["local_repaired_ranges"] == [[40.0, 60.0], [80.0, 100.0]]
    assert cov["holes_repaired_local"] == 2
    assert cov["local_repair"]["model"] == "small"


def test_cloud_pass_marker_with_same_settings_is_respected(
    monkeypatch: pytest.MonkeyPatch, audio: Path
) -> None:
    settings = local_hole_repair.LocalRepairSettings()
    marker = {
        "enabled": True,
        "model": settings.model,
        "margin_sec": settings.margin_sec,
        "min_speech_sec": settings.min_speech_sec,
        "words_added": 0,
        "errors": [],
    }
    index = make_index([[40.0, 60.0]], coverage_extra={"local_repair": marker})
    engine = Engine(monkeypatch)
    assert run(index, audio, engine).status == "already_repaired"
    # ремонт при облачном проходе не состоялся (skipped_reason) — чиним
    marker["skipped_reason"] = "disabled"
    engine.words[37.0] = local_words(40.5, 15)
    assert run(index, audio, engine).status == "repaired"


def test_stopped_marker_does_not_block_a_retry(
    monkeypatch: pytest.MonkeyPatch, audio: Path
) -> None:
    index = make_index(
        [[40.0, 60.0]],
        coverage_extra={
            "local_repair": {
                "fingerprint": settings_fingerprint(local_hole_repair.LocalRepairSettings()),
                "stopped": "deadline",
                "complete": False,
            }
        },
    )
    engine = Engine(monkeypatch)
    engine.words[37.0] = local_words(40.5, 15)
    assert run(index, audio, engine).status == "repaired"


def test_skips_disabled_without_audio_non_cloud_and_rescaled(
    monkeypatch: pytest.MonkeyPatch, audio: Path, tmp_path: Path
) -> None:
    engine = Engine(monkeypatch)
    index = make_index([[40.0, 60.0]])
    off = local_hole_repair.LocalRepairSettings(enabled=False)
    assert run(index, audio, engine, settings=off).status == "disabled"
    missing = repair_cached_index_holes(
        index,
        tmp_path / "missing.wav",
        language="ru",
        now_iso=NOW,
        transcribe=engine.transcribe,
        settings=local_hole_repair.LocalRepairSettings(),
    )
    assert missing.status == "no_audio" and missing.reason
    manual = make_index([[40.0, 60.0]])
    manual.metadata["backend"] = "faster-whisper"
    assert run(manual, audio, engine).status == "not_needed"
    rescaled = make_index([[40.0, 60.0]], metadata_extra={"timestamp_scale_repair": {"scale": 0.9}})
    assert run(rescaled, audio, engine).status == "scale_repaired"
    assert engine.calls == []
    assert coverage_of(index)["unrecovered_ranges"] == [[40.0, 60.0]]


def test_cancel_returns_cancelled_and_keeps_index(
    monkeypatch: pytest.MonkeyPatch, audio: Path
) -> None:
    import threading

    index = make_index([[40.0, 60.0]])
    snapshot = repr(index.to_dict())
    cancel = threading.Event()
    cancel.set()
    engine = Engine(monkeypatch)
    result = run(index, audio, engine, cancel=cancel)
    assert result.status == "cancelled" and repr(index.to_dict()) == snapshot


def test_unchanged_writes_complete_marker_and_is_not_repeated(
    monkeypatch: pytest.MonkeyPatch, audio: Path
) -> None:
    index = make_index([[40.0, 60.0]])
    words_before = [(w.word, w.start, w.end) for w in index.words]
    engine = Engine(monkeypatch)
    engine.words[37.0] = []  # Whisper ничего нового не нашёл

    result = run(index, audio, engine)

    assert result.status == "unchanged" and result.changed
    assert [(w.word, w.start, w.end) for w in index.words] == words_before
    marker = coverage_of(index)["local_repair"]
    assert marker["complete"] is True and marker["source"] == "cached-index"
    assert coverage_of(index)["unrecovered_ranges"] == [[40.0, 60.0]]
    calls = len(engine.calls)
    again = run(index, audio, engine)
    assert again.status == "already_repaired" and len(engine.calls) == calls


def test_unchanged_with_engine_failure_writes_no_marker(
    monkeypatch: pytest.MonkeyPatch, audio: Path
) -> None:
    index = make_index([[40.0, 60.0]])
    engine = Engine(monkeypatch)
    engine.error = RuntimeError("boom")
    result = run(index, audio, engine)
    assert result.status == "failed" and not result.changed
    assert "local_repair" not in coverage_of(index)


def test_dense_holes_are_not_recognized_again(monkeypatch: pytest.MonkeyPatch, audio: Path) -> None:
    # в «дыре» 40-60 уже 20 слов (60 слов/мин) -> плотная речь, не дыра
    index = make_index([[40.0, 60.0]], skip=(200.0, 200.0))
    engine = Engine(monkeypatch)
    result = run(index, audio, engine)
    assert engine.calls == [] and result.changed
    cov = coverage_of(index)
    assert cov["unrecovered_ranges"] == [] and cov["unrecovered_sec"] == 0
    assert cov["local_repair"]["complete"] is True
    assert run(index, audio, engine).status == "not_needed"


def test_dense_filter_keeps_sparse_holes(monkeypatch: pytest.MonkeyPatch, audio: Path) -> None:
    index = make_index([[40.0, 60.0], [80.0, 100.0]], skip=(80.0, 100.0))
    # первая дыра плотная (слова 40..60 есть), вторая пустая
    engine = Engine(monkeypatch)
    engine.words[77.0] = local_words(81.0, 14, "s")
    result = run(index, audio, engine)
    assert result.status == "repaired"
    assert engine.clips == [(77.0, 103.0)]
    cov = coverage_of(index)
    assert cov["unrecovered_ranges"] == [] and cov["local_repaired_ranges"] == [[80.0, 100.0]]


def test_cloud_marker_with_engine_errors_is_not_complete(
    monkeypatch: pytest.MonkeyPatch, audio: Path
) -> None:
    settings = local_hole_repair.LocalRepairSettings()
    marker = {
        "enabled": True,
        "model": settings.model,
        "margin_sec": settings.margin_sec,
        "min_speech_sec": settings.min_speech_sec,
        "words_added": 0,
        "errors": [{"range": [40.0, 60.0], "error": "RuntimeError: model load failed"}],
    }
    index = make_index([[40.0, 60.0]], coverage_extra={"local_repair": marker})
    engine = Engine(monkeypatch)
    engine.words[37.0] = local_words(40.5, 15)
    assert run(index, audio, engine).status == "repaired"
    # «новых слов не дало» — не сбой движка: попытка завершена
    marker["errors"] = [
        {"range": [40.0, 60.0], "error": local_hole_repair.NO_NEW_WORDS_ERROR},
    ]
    index = make_index([[40.0, 60.0]], coverage_extra={"local_repair": marker})
    assert run(index, audio, engine).status == "already_repaired"


def test_max_sec_stops_with_deadline_and_keeps_repaired(
    monkeypatch: pytest.MonkeyPatch, audio: Path
) -> None:
    import time

    index = make_index([[40.0, 60.0], [80.0, 100.0]])
    index.words = [w for w in index.words if not 80 <= w.start < 100]
    engine = Engine(monkeypatch)
    engine.words[37.0] = local_words(40.5, 15)
    engine.words[77.0] = local_words(81.0, 14, "s")
    original = engine.transcribe

    def slow(*args: Any, **kwargs: Any):
        if engine.calls:  # вторая дыра: лимит истекает во время распознавания
            time.sleep(0.15)
        return original(*args, **kwargs)

    engine.transcribe = slow  # type: ignore[method-assign]
    settings = local_hole_repair.LocalRepairSettings(max_sec=0.1)
    result = run(index, audio, engine, settings=settings)

    assert result.status == "repaired" and result.stopped == "deadline"
    cov = coverage_of(index)
    assert cov["local_repaired_ranges"] == [[40.0, 60.0]]
    assert cov["unrecovered_ranges"] == [[80.0, 100.0]]
    assert cov["local_repair"]["stopped"] == "deadline"
    assert cov["local_repair"]["complete"] is False
