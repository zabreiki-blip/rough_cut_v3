"""Boundary cases for ``stage1.validator`` (WAV metrics and manifest rules)."""

from __future__ import annotations

import json
import struct
import wave
from pathlib import Path

import pytest

from stage1 import validator
from stage1.validator import _audio_metrics_wav, _read_pcm_stats, validate_stage1


def _write_wav(
    path: Path,
    samples: list[int],
    *,
    sample_width: int = 2,
    channels: int = 1,
    rate: int = 8000,
) -> None:
    fmt = {1: "B", 2: "h", 4: "i"}[sample_width]
    if sample_width == 1:
        samples = [s + 128 for s in samples]
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(rate)
        wf.writeframes(struct.pack(f"<{len(samples)}{fmt}", *samples))


def _loud(n: int = 8000, amplitude: int = 5000) -> list[int]:
    return [amplitude if i % 2 else -amplitude for i in range(n)]


def _prepared(tmp_path: Path, ids: list[str]) -> Path:
    path = tmp_path / "prepared.json"
    path.write_text(
        json.dumps({"items": [{"id": i, "text_for_tts": f" text {i} "} for i in ids]}),
        encoding="utf-8",
    )
    return path


def _validate(tmp_path: Path, ids: list[str], **kwargs) -> dict:
    return validate_stage1(
        _prepared(tmp_path, ids),
        tmp_path / "speaker",
        tmp_path / "speaker" / "manifest.json",
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _default_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VALIDATE_MIN_WAV_DURATION_MS", raising=False)
    monkeypatch.delenv("VALIDATE_SILENCE_RMS_THRESHOLD", raising=False)


# --- PCM metrics --------------------------------------------------------------


@pytest.mark.parametrize("width", [1, 2, 4])
def test_metrics_support_all_pcm_widths(tmp_path: Path, width: int) -> None:
    wav = tmp_path / "a.wav"
    _write_wav(wav, _loud(8000, 100), sample_width=width)

    duration, rms, peak = _audio_metrics_wav(wav)

    assert duration == 1000
    assert rms == 100
    assert peak == 100


def test_metrics_use_first_channel_only(tmp_path: Path) -> None:
    wav = tmp_path / "stereo.wav"
    frames: list[int] = []
    for _ in range(4000):
        frames.extend([1000, 30000])  # left=1000, right is much louder
    _write_wav(wav, frames, channels=2)

    _duration, rms, peak = _audio_metrics_wav(wav)

    assert rms == 1000
    assert peak == 1000


def test_metrics_of_silence_are_zero_but_duration_is_kept(tmp_path: Path) -> None:
    wav = tmp_path / "silent.wav"
    _write_wav(wav, [0] * 4000)

    assert _audio_metrics_wav(wav) == (500, 0, 0)


def test_unsupported_sample_width_returns_zero_stats(tmp_path: Path) -> None:
    wav = tmp_path / "a.wav"
    _write_wav(wav, [1, 2, 3])
    with wave.open(str(wav), "rb") as wf:
        assert _read_pcm_stats(wf, sample_width=3, n_channels=1) == (0, 0)


def test_empty_wav_has_zero_duration_and_stats(tmp_path: Path) -> None:
    wav = tmp_path / "empty.wav"
    _write_wav(wav, [])

    assert _audio_metrics_wav(wav) == (0, 0, 0)


def test_large_file_is_read_in_multiple_chunks(tmp_path: Path) -> None:
    wav = tmp_path / "long.wav"
    _write_wav(wav, _loud(20000, 300))

    duration, rms, peak = _audio_metrics_wav(wav)

    assert (duration, rms, peak) == (2500, 300, 300)


@pytest.mark.parametrize("content", [b"not a wav at all", b"", b"RIFF\x00\x00"])
def test_corrupt_wav_returns_zeros_instead_of_raising(tmp_path: Path, content: bytes) -> None:
    wav = tmp_path / "bad.wav"
    wav.write_bytes(content)

    assert _audio_metrics_wav(wav) == (0, 0, 0)


def test_directory_in_place_of_wav_is_unreadable(tmp_path: Path) -> None:
    folder = tmp_path / "dir.wav"
    folder.mkdir()

    assert _audio_metrics_wav(folder) == (0, 0, 0)


def test_zero_frame_rate_is_treated_as_unreadable(tmp_path: Path, monkeypatch) -> None:
    wav = tmp_path / "a.wav"
    _write_wav(wav, _loud(10))
    monkeypatch.setattr(wave.Wave_read, "getframerate", lambda self: 0)

    assert _audio_metrics_wav(wav) == (0, 0, 0)


# --- validate_stage1 ------------------------------------------------------------


def test_prepared_without_items_raises(tmp_path: Path) -> None:
    for payload in ({"items": []}, {"items": "x"}, {}):
        path = tmp_path / "p.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError, match="no items"):
            validate_stage1(path, tmp_path, tmp_path / "m.json")


def test_valid_project_writes_manifest_with_custom_project_name(tmp_path: Path) -> None:
    speaker = tmp_path / "speaker"
    speaker.mkdir()
    _write_wav(speaker / "zk1.wav", _loud())
    _write_wav(speaker / "zk2.wav", _loud())

    result = _validate(tmp_path, ["zk1", "zk2"], project_name="demo")

    assert result["ok"] is True
    assert result["errors"] == [] and result["warnings"] == []
    manifest = json.loads((speaker / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["project"] == "demo"
    assert [i["id"] for i in manifest["items"]] == ["zk1", "zk2"]
    assert manifest["items"][0]["duration_ms"] == 1000
    assert manifest["items"][0]["text_for_tts"] == "text zk1"


def test_missing_audio_is_an_error_and_stale_manifest_is_removed(tmp_path: Path) -> None:
    speaker = tmp_path / "speaker"
    speaker.mkdir()
    manifest = speaker / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    _write_wav(speaker / "zk1.wav", _loud())

    result = _validate(tmp_path, ["zk1", "zk2"])

    assert result["ok"] is False
    assert any("Missing audio file" in e and "zk2.wav" in e for e in result["errors"])
    assert not manifest.exists()


def test_corrupt_wav_is_reported_as_error(tmp_path: Path) -> None:
    speaker = tmp_path / "speaker"
    speaker.mkdir()
    (speaker / "zk1.wav").write_bytes(b"garbage")

    result = _validate(tmp_path, ["zk1"])

    assert result["ok"] is False
    assert any("Invalid or unreadable WAV" in e for e in result["errors"])


@pytest.mark.parametrize(
    ("ids", "expected"),
    [
        (["zk1", "zk1"], "Duplicate id: zk1"),
        (["zk2", "zk1"], "IDs are not a continuous sequence"),
        (["zk1", "zk3"], "IDs are not a continuous sequence"),
        (["zk1", "bad"], "Invalid id format: bad"),
        (["ZK1"], "Invalid id format: ZK1"),
        (["zk1 x"], "Invalid id format"),
    ],
)
def test_id_rules(tmp_path: Path, ids: list[str], expected: str) -> None:
    (tmp_path / "speaker").mkdir()

    result = _validate(tmp_path, ids)

    assert result["ok"] is False
    assert any(expected in e for e in result["errors"]), result["errors"]


def test_short_wav_and_silent_wav_produce_warnings_not_errors(tmp_path: Path) -> None:
    speaker = tmp_path / "speaker"
    speaker.mkdir()
    _write_wav(speaker / "zk1.wav", _loud(800))  # 100 ms, loud
    _write_wav(speaker / "zk2.wav", [0] * 8000)  # 1 s, silent

    result = _validate(tmp_path, ["zk1", "zk2"])

    assert result["ok"] is True
    assert any("too short" in w and "zk1.wav" in w for w in result["warnings"])
    assert any("looks silent" in w and "zk2.wav" in w for w in result["warnings"])
    assert (speaker / "manifest.json").exists()


def test_rms_exactly_at_threshold_counts_as_silent(tmp_path: Path, monkeypatch) -> None:
    speaker = tmp_path / "speaker"
    speaker.mkdir()
    _write_wav(speaker / "zk1.wav", _loud(8000, 120))
    monkeypatch.setenv("VALIDATE_SILENCE_RMS_THRESHOLD", "120")

    assert any("looks silent" in w for w in _validate(tmp_path, ["zk1"])["warnings"])

    monkeypatch.setenv("VALIDATE_SILENCE_RMS_THRESHOLD", "119")
    assert _validate(tmp_path, ["zk1"])["warnings"] == []


def test_duration_exactly_at_minimum_is_not_short(tmp_path: Path, monkeypatch) -> None:
    speaker = tmp_path / "speaker"
    speaker.mkdir()
    _write_wav(speaker / "zk1.wav", _loud(2800))  # exactly 350 ms at 8 kHz
    result = _validate(tmp_path, ["zk1"])

    assert result["validation_config"]["min_wav_duration_ms"] == 350
    assert result["warnings"] == []


def test_thresholds_can_be_disabled_with_zero(tmp_path: Path, monkeypatch) -> None:
    speaker = tmp_path / "speaker"
    speaker.mkdir()
    _write_wav(speaker / "zk1.wav", [0] * 400)
    monkeypatch.setenv("VALIDATE_MIN_WAV_DURATION_MS", "0")
    monkeypatch.setenv("VALIDATE_SILENCE_RMS_THRESHOLD", "0")

    result = _validate(tmp_path, ["zk1"])

    assert result["ok"] is True
    assert result["warnings"] == []
    assert result["validation_config"] == {
        "min_wav_duration_ms": 0,
        "silence_rms_threshold": 0,
    }


def test_invalid_env_thresholds_fall_back_or_clamp(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "speaker").mkdir()
    monkeypatch.setenv("VALIDATE_MIN_WAV_DURATION_MS", "soon")
    monkeypatch.setenv("VALIDATE_SILENCE_RMS_THRESHOLD", "-5")

    config = _validate(tmp_path, ["zk1"])["validation_config"]

    assert config["min_wav_duration_ms"] == 350
    # Negative values are clamped to 0, which disables the silence check.
    assert config["silence_rms_threshold"] == 0


def test_env_int_helper_clamps_values(monkeypatch) -> None:
    monkeypatch.setenv("X_TEST_INT", "999")
    assert validator._env_int("X_TEST_INT", 5, min_value=0, max_value=10) == 10
