from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path
from typing import Any

import pytest

from stage2.sync_models import SyncMatch, SyncSource, WordIndex, WordTimestamp
from stage2.sync_window_refine import (
    WindowRefineSettings,
    refine_candidate_window,
    refine_matches,
    select_candidates,
    unrefined_source_ids,
)

QUOTE = "Лиля Брик познакомила Маяковского с композитором Щедриным в Москве"
WORDS = QUOTE.split()


def _audio(tmp_path: Path) -> Path:
    path = tmp_path / "guide.wav"
    if not path.exists():
        path.write_bytes(b"RIFF" + b"\0" * 100)
    return path


def _extract(calls: list[tuple[float, float]] | None = None):
    def extract(audio: Path, start: float, duration: float, out: Path) -> None:
        if calls is not None:
            calls.append((round(start, 2), round(duration, 2)))
        out.write_bytes(b"RIFF" + b"\0" * 100)

    return extract


class _Transcriber:
    """Fake local Whisper: the quote is spoken at ``spoken_at`` (absolute seconds)."""

    def __init__(self, spoken_at: float | None, window_start: float = 0.0) -> None:
        self.spoken_at = spoken_at
        self.calls = 0
        self.window_start = window_start

    def __call__(self, clip: Path, model: str) -> list[dict[str, Any]]:
        self.calls += 1
        words = [{"word": "совсем", "start": 1.0, "end": 1.4}, {"word": "другое", "start": 1.5, "end": 2}]
        if self.spoken_at is not None:
            origin = self.spoken_at - self.window_start
            words += [
                {"word": w, "start": origin + i * 0.5, "end": origin + i * 0.5 + 0.4}
                for i, w in enumerate(WORDS)
            ]
        return words


def test_refine_candidate_window_fixes_17_second_shift(tmp_path: Path) -> None:
    calls: list[tuple[float, float]] = []
    # cloud said 116.7-127.3; the real speech is at 99.26 (window starts at 71.7)
    transcriber = _Transcriber(99.26, window_start=116.7 - 45)

    result = refine_candidate_window(
        _audio(tmp_path), QUOTE, 116.7, 127.3,
        transcribe_fn=transcriber, extract_fn=_extract(calls),
    )  # fmt: skip

    assert calls == [(71.7, 100.6)]
    assert result is not None
    speech_in, speech_out, score = result
    assert speech_in == pytest.approx(99.26, abs=0.01)
    assert speech_out == pytest.approx(99.26 + 0.5 * (len(WORDS) - 1) + 0.4, abs=0.01)
    assert score >= 0.78


def test_refine_candidate_window_quote_absent_returns_none(tmp_path: Path) -> None:
    assert (
        refine_candidate_window(
            _audio(tmp_path), QUOTE, 100, 110,
            transcribe_fn=_Transcriber(None), extract_fn=_extract(),
        )  # fmt: skip
        is None
    )


def test_window_cache_avoids_second_recognition(tmp_path: Path) -> None:
    transcriber = _Transcriber(99.26, window_start=55.0)
    cache = tmp_path / "window-refine"
    kwargs: dict[str, Any] = {
        "transcribe_fn": transcriber, "extract_fn": _extract(), "cache_dir": cache,
    }  # fmt: skip
    audio = _audio(tmp_path)

    first = refine_candidate_window(audio, QUOTE, 100, 110, **kwargs)
    second = refine_candidate_window(audio, QUOTE, 100, 110, **kwargs)

    assert first is not None and first == second
    assert transcriber.calls == 1
    assert len(list(cache.glob("*.json"))) == 1
    # a replaced audio file (new size/mtime) invalidates the cache
    audio.write_bytes(b"RIFF" + b"\0" * 300)
    refine_candidate_window(audio, QUOTE, 100, 110, **kwargs)
    assert transcriber.calls == 2


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_default_extract_cuts_the_window(tmp_path: Path) -> None:
    audio = tmp_path / "tone.wav"
    with wave.open(str(audio), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 16000 * 10)
    seen: dict[str, float] = {}

    def transcribe(clip: Path, model: str) -> list[dict[str, Any]]:
        with wave.open(str(clip), "rb") as handle:
            seen["duration"] = handle.getnframes() / handle.getframerate()
        return []

    assert refine_candidate_window(audio, QUOTE, 4, 5, window_pad_sec=1, transcribe_fn=transcribe) is None
    assert seen["duration"] == pytest.approx(3.0, abs=0.1)
    subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)


# ---- pass over matches ---------------------------------------------------------------


def _match(
    segment_id: str = "seg", *, status: str = "auto_matched", **metadata: Any
) -> SyncMatch:
    base = {
        "speech_in_sec": 116.7,
        "speech_out_sec": 125.8,
        "head_padding_sec": 2.0,
        "tail_padding_sec": 1.5,
        "timestamp_precision": "interpolated",
        "timing_flags": ["timing_unrefined"],
        "timing_reliable": False,
    }
    base.update(metadata)
    return SyncMatch(
        segment_id=segment_id,
        query_text=QUOTE,
        source_id="src",
        source_in_sec=114.7,
        source_out_sec=127.3,
        confidence=0.95,
        status=status,  # type: ignore[arg-type]
        matched_text=QUOTE,
        metadata=base,
    )


def _index(precision: str) -> WordIndex:
    return WordIndex("src", [WordTimestamp("a", 0, 1)], metadata={"timestamp_precision": precision})


def _run(matches, tmp_path: Path, transcriber, *, precision="interpolated", **overrides):
    audio = _audio(tmp_path)
    settings = overrides.pop("settings", WindowRefineSettings())
    duration = overrides.pop("duration", 1000)
    return refine_matches(
        matches,
        [SyncSource(id="src", speaker="x", premiere_sequence="s", duration_sec=duration)],
        [_index(precision)],
        settings=settings,
        resolve_audio_path=lambda _source: audio,
        is_manual=lambda m: m.status == "confirmed" or bool(m.metadata.get("manual_updated_at")),
        cache_dir=tmp_path / "wr",
        transcribe_fn=transcriber,
        extract_fn=_extract(),
        **overrides,
    )


def test_refine_matches_moves_range_and_marks_precision(tmp_path: Path) -> None:
    match = _match(status="needs_review", review_reason="timing_unrefined")
    transcriber = _Transcriber(99.26, window_start=116.7 - 45)

    report = _run([match], tmp_path, transcriber)

    assert report.refined == ["seg"] and report.transcribed == 1
    meta = match.metadata
    assert meta["timestamp_precision"] == "window_refined"
    assert meta["window_refined"]["from"] == [116.7, 125.8]
    assert meta["window_refined"]["to"][0] == pytest.approx(99.26, abs=0.01)
    assert meta["speech_in_sec"] == pytest.approx(99.26, abs=0.01)
    assert match.source_in_sec == pytest.approx(99.26 - 2.0, abs=0.01)
    assert match.source_out_sec == pytest.approx(meta["speech_out_sec"] + 1.5, abs=0.01)
    assert meta["timing_flags"] == [] and meta["timing_reliable"] is True
    # only the timing blocked the text-confirmed candidate: it is accepted now
    assert match.status == "auto_matched" and "review_reason" not in meta


def test_quote_not_found_demotes_auto_to_needs_review(tmp_path: Path) -> None:
    match = _match()

    report = _run([match], tmp_path, _Transcriber(None))

    assert report.downgraded == ["seg"]
    assert match.status == "needs_review"
    assert match.metadata["review_reason"] == "timing_unreliable"
    assert match.source_in_sec == 114.7  # range untouched


def test_missing_audio_demotes_auto(tmp_path: Path) -> None:
    match = _match()
    report = refine_matches(
        [match],
        [SyncSource(id="src", speaker="x", premiere_sequence="s")],
        [_index("interpolated")],
        settings=WindowRefineSettings(),
        resolve_audio_path=lambda _s: None,
        is_manual=lambda _m: False,
    )
    assert report.downgraded == ["seg"] and match.metadata["review_reason"] == "timing_unreliable"


@pytest.mark.parametrize("precision", ["word", "retimed"])
def test_reliable_sources_are_not_refined(tmp_path: Path, precision: str) -> None:
    match = _match()
    transcriber = _Transcriber(99.26)

    report = _run([match], tmp_path, transcriber, precision=precision)

    assert report.candidates == 0 and transcriber.calls == 0
    assert match.status == "auto_matched" and "window_refined" not in match.metadata


def test_manual_and_already_refined_matches_are_untouched(tmp_path: Path) -> None:
    confirmed = _match("c", status="confirmed")
    edited = _match("e", status="needs_review", manual_updated_at="2026-01-01")
    done = _match("d", window_refined={"from": [1, 2], "to": [3, 4], "score": 1.0})
    transcriber = _Transcriber(None)

    report = _run([confirmed, edited, done], tmp_path, transcriber)

    assert report.candidates == 0 and transcriber.calls == 0
    assert confirmed.status == "confirmed" and confirmed.source_in_sec == 114.7
    assert "review_reason" not in confirmed.metadata


def test_second_run_uses_the_cache(tmp_path: Path) -> None:
    transcriber = _Transcriber(99.26, window_start=71.7)
    _run([_match()], tmp_path, transcriber)
    second = _match()

    report = _run([second], tmp_path, transcriber)

    assert transcriber.calls == 1
    assert report.cache_hits == 1 and report.transcribed == 0
    assert second.metadata["timestamp_precision"] == "window_refined"


def test_disabled_does_nothing(tmp_path: Path) -> None:
    match = _match()
    transcriber = _Transcriber(99.26)

    report = _run([match], tmp_path, transcriber, settings=WindowRefineSettings(enabled=False))

    assert report.candidates == 0 and transcriber.calls == 0
    assert match.status == "auto_matched" and match.metadata["timestamp_precision"] == "interpolated"


def test_window_limit_demotes_the_rest(tmp_path: Path) -> None:
    first, second = _match("a"), _match("b", speech_in_sec=300.0, speech_out_sec=309.0)
    transcriber = _Transcriber(None)

    report = _run([first, second], tmp_path, transcriber, settings=WindowRefineSettings(max_windows=1))

    assert transcriber.calls == 1 and report.skipped_limit == 1
    assert second.metadata["window_refine"]["status"] == "limit"
    assert second.status == "needs_review"


def test_settings_from_env() -> None:
    env = {"SYNC_WINDOW_REFINE": "0", "SYNC_WINDOW_REFINE_MAX": "7"}
    settings = WindowRefineSettings.from_env(lambda name, default: env.get(name, default))
    assert (settings.enabled, settings.max_windows) == (False, 7)
    default = WindowRefineSettings.from_env(lambda name, default: default)
    assert (default.enabled, default.max_windows) == (True, 300)


def test_progress_callback(tmp_path: Path) -> None:
    seen: list[tuple[int, int]] = []
    _run([_match("a"), _match("b", speech_in_sec=300.0, speech_out_sec=309.0)], tmp_path,
         _Transcriber(None), on_progress=lambda d, t: seen.append((d, t)))  # fmt: skip
    assert seen == [(1, 2), (2, 2)]


def test_interpolated_source_with_rejected_retime_is_unreliable_in_both_passes(
    tmp_path: Path,
) -> None:
    # Regression: the index reloaded from disk (retime rejected) used to look "word"-precise,
    # so refine and audio verify disagreed about which sources are unreliable.
    import json

    import sync_audio_verify_phase as audio_phase
    from stage2.sync_transcript import load_word_index
    from stage2.sync_matcher import match_timing_unreliable, unreliable_timing_source_ids

    path = tmp_path / "src.words.json"
    path.write_text(
        json.dumps(
            {
                "source_id": "src",
                "metadata": {
                    "backend": "cloud-audio",
                    "timestamp_precision": "interpolated",
                    "backend_metadata": {"timestamp_precision": "interpolated"},
                },
                "words": [{"word": "а", "start": 0.0, "end": 1.0}],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "src.retime_attempt.json").write_text('{"status": "rejected"}', encoding="utf-8")
    index = load_word_index(path, "src")
    assert unrefined_source_ids([index]) == {"src"} == unreliable_timing_source_ids([index])
    match = _match()
    assert match_timing_unreliable(match, {"src"})
    assert [m.segment_id for m in audio_phase.select_candidates([match], [index])] == ["seg"]
    assert select_candidates([match], unrefined_source_ids([index]), is_manual=lambda _m: False)


def test_refined_range_is_clamped_to_source_duration(tmp_path: Path) -> None:
    match = _match(status="needs_review", review_reason="timing_unrefined")
    _run([match], tmp_path, _Transcriber(99.26, window_start=116.7 - 45), duration=100.0)
    assert match.source_in_sec == pytest.approx(99.26 - 2.0, abs=0.01)
    assert match.source_out_sec == 100.0  # speech_out + tail would be beyond the file end
    assert match.metadata["speech_out_sec"] > 100.0 - 1.5


def test_review_reason_is_replaced_not_dropped_when_still_needs_review(tmp_path: Path) -> None:
    flagged = _match(
        "flagged", status="needs_review", review_reason="timing_unrefined",
        plausibility_flags=["implausible_word_rate"],
    )
    low = _match("low", status="needs_review", review_reason="timing_unrefined")
    low.confidence = 0.5  # below the auto threshold: the text verdict does not stand
    _run([flagged, low], tmp_path, _Transcriber(99.26, window_start=116.7 - 45))
    assert flagged.status == "needs_review"
    assert flagged.metadata["review_reason"] == "implausible_word_rate"
    assert low.status == "needs_review" and low.metadata["review_reason"] == "low_confidence"


def test_checkpoint_callback_runs_every_n_windows(tmp_path: Path) -> None:
    matches = [_match(f"s{i}", status="needs_review") for i in range(5)]
    seen: list[list[str]] = []
    _run(
        matches, tmp_path, _Transcriber(None),
        on_checkpoint=lambda ids: seen.append(list(ids)), checkpoint_every=2,
    )
    assert [len(ids) for ids in seen] == [2, 4]
