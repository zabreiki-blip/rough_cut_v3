from __future__ import annotations

import json
import math
from pathlib import Path
import struct
import wave

import pytest

from stage1.validator import _audio_metrics_wav, _env_int, _read_pcm_stats, validate_stage1


RATE = 8000


def _write_wav(
    path: Path,
    samples: list[int],
    *,
    sample_width: int = 2,
    channels: int = 1,
    rate: int = RATE,
) -> Path:
    """Write interleaved integer samples as a PCM WAV file."""
    fmt = {1: "B", 2: "h", 4: "i"}[sample_width]
    if sample_width == 1:
        samples = [s + 128 for s in samples]
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(rate)
        wf.writeframes(struct.pack(f"<{len(samples)}{fmt}", *samples))
    return path


def _loud(ms: int, amplitude: int = 8000) -> list[int]:
    count = RATE * ms // 1000
    return [int(amplitude * math.sin(2 * math.pi * 440 * i / RATE)) for i in range(count)]


def _silent(ms: int) -> list[int]:
    return [0] * (RATE * ms // 1000)


def _prepare(tmp_path: Path, items: list[dict] | object) -> Path:
    prepared = tmp_path / "prepared.json"
    prepared.write_text(json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")
    return prepared


def _run(tmp_path: Path, items: list[dict] | object, **kwargs: object) -> dict:
    speaker_dir = tmp_path / "speaker"
    speaker_dir.mkdir(exist_ok=True)
    return validate_stage1(
        _prepare(tmp_path, items),
        speaker_dir,
        tmp_path / "manifest.json",
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.fixture(autouse=True)
def _validator_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VALIDATE_MIN_WAV_DURATION_MS", raising=False)
    monkeypatch.delenv("VALIDATE_SILENCE_RMS_THRESHOLD", raising=False)
    monkeypatch.delenv("PROJECT_NAME", raising=False)


# ---------------------------------------------------------------- _env_int


def test_env_int_uses_default_clamps_and_ignores_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VALIDATOR_TEST_INT", raising=False)
    assert _env_int("VALIDATOR_TEST_INT", 7) == 7

    monkeypatch.setenv("VALIDATOR_TEST_INT", " 12 ")
    assert _env_int("VALIDATOR_TEST_INT", 7) == 12

    monkeypatch.setenv("VALIDATOR_TEST_INT", "abc")
    assert _env_int("VALIDATOR_TEST_INT", 7) == 7

    monkeypatch.setenv("VALIDATOR_TEST_INT", "-5")
    assert _env_int("VALIDATOR_TEST_INT", 7, min_value=0, max_value=10) == 0

    monkeypatch.setenv("VALIDATOR_TEST_INT", "999")
    assert _env_int("VALIDATOR_TEST_INT", 7, min_value=0, max_value=10) == 10


# ----------------------------------------------------------- WAV metrics


def test_audio_metrics_reports_duration_rms_and_peak(tmp_path: Path) -> None:
    path = _write_wav(tmp_path / "a.wav", [1000, -1000] * (RATE // 2))  # 1 second

    duration_ms, rms, peak = _audio_metrics_wav(path)

    assert duration_ms == 1000
    assert rms == 1000
    assert peak == 1000


def test_audio_metrics_silence_has_zero_rms_and_peak(tmp_path: Path) -> None:
    path = _write_wav(tmp_path / "a.wav", _silent(500))

    duration_ms, rms, peak = _audio_metrics_wav(path)

    assert (duration_ms, rms, peak) == (500, 0, 0)


def test_audio_metrics_uses_first_channel_only(tmp_path: Path) -> None:
    frames = RATE // 4
    interleaved = [500, 20000] * frames  # left=500, right=20000
    path = _write_wav(tmp_path / "stereo.wav", interleaved, channels=2)

    duration_ms, rms, peak = _audio_metrics_wav(path)

    assert duration_ms == 250
    assert rms == 500
    assert peak == 500


@pytest.mark.parametrize(
    ("sample_width", "value"),
    [(1, 50), (2, 3000), (4, 1_000_000)],
)
def test_audio_metrics_supports_8_16_and_32_bit_pcm(
    tmp_path: Path, sample_width: int, value: int
) -> None:
    path = _write_wav(tmp_path / "a.wav", [value, -value] * 400, sample_width=sample_width)

    duration_ms, rms, peak = _audio_metrics_wav(path)

    assert duration_ms == 100
    assert rms == value
    assert peak == value


def test_audio_metrics_unsupported_sample_width_returns_zero_levels(tmp_path: Path) -> None:
    path = tmp_path / "w24.wav"
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(3)
        wf.setframerate(RATE)
        wf.writeframes(b"\x00\x10\x00" * RATE)

    assert _audio_metrics_wav(path) == (1000, 0, 0)


def test_audio_metrics_empty_wav_has_zero_duration(tmp_path: Path) -> None:
    path = _write_wav(tmp_path / "empty.wav", [])

    assert _audio_metrics_wav(path) == (0, 0, 0)


def test_audio_metrics_garbage_and_missing_files_return_zeros(tmp_path: Path) -> None:
    garbage = tmp_path / "garbage.wav"
    garbage.write_bytes(b"this is not a wav file")

    assert _audio_metrics_wav(garbage) == (0, 0, 0)
    assert _audio_metrics_wav(tmp_path / "missing.wav") == (0, 0, 0)


def test_read_pcm_stats_without_frames_returns_zero(tmp_path: Path) -> None:
    path = _write_wav(tmp_path / "empty.wav", [])

    with wave.open(str(path), "rb") as wf:
        assert _read_pcm_stats(wf, sample_width=2, n_channels=1) == (0, 0)


# ------------------------------------------------------- validate_stage1


def test_validate_stage1_happy_path_writes_manifest(tmp_path: Path) -> None:
    items = [
        {"id": "zk1", "text_for_tts": "  Привет, мир! Ёлка 12.  "},
        {"id": "zk2", "text_for_tts": "Второй блок"},
    ]
    speaker_dir = tmp_path / "speaker"
    speaker_dir.mkdir()
    _write_wav(speaker_dir / "zk1.wav", _loud(1000))
    _write_wav(speaker_dir / "zk2.wav", _loud(500))

    result = _run(tmp_path, items, project_name="Мой проект")

    assert result["ok"] is True
    assert result["errors"] == []
    assert result["warnings"] == []
    assert result["items_count"] == 2
    assert result["validation_config"] == {
        "min_wav_duration_ms": 350,
        "silence_rms_threshold": 120,
    }
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["project"] == "Мой проект"
    assert manifest["generated_at"]
    assert [(m["id"], m["duration_ms"], m["text_for_tts"]) for m in manifest["items"]] == [
        ("zk1", 1000, "Привет, мир! Ёлка 12."),
        ("zk2", 500, "Второй блок"),
    ]
    assert manifest["items"][0]["file"] == str(speaker_dir / "zk1.wav")


def test_validate_stage1_project_name_defaults_to_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROJECT_NAME", "env_project")
    speaker_dir = tmp_path / "speaker"
    speaker_dir.mkdir()
    _write_wav(speaker_dir / "zk1.wav", _loud(500))

    result = _run(tmp_path, [{"id": "zk1", "text_for_tts": "x"}])

    assert result["ok"] is True
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["project"] == "env_project"


@pytest.mark.parametrize("payload", [[], None, {"a": 1}, "text"])
def test_validate_stage1_rejects_missing_or_non_list_items(tmp_path: Path, payload: object) -> None:
    with pytest.raises(ValueError, match="no items"):
        _run(tmp_path, payload)


def test_validate_stage1_rejects_payload_without_items_key(tmp_path: Path) -> None:
    prepared = tmp_path / "prepared.json"
    prepared.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="no items"):
        validate_stage1(prepared, tmp_path, tmp_path / "manifest.json")


@pytest.mark.parametrize("bad_id", ["", "zk", "ZK1", "zk1a", "x1", "zk-1", " ", "зк1"])
def test_validate_stage1_reports_invalid_id_format(tmp_path: Path, bad_id: str) -> None:
    result = _run(tmp_path, [{"id": bad_id, "text_for_tts": "x"}])

    assert result["ok"] is False
    assert f"Invalid id format: {bad_id.strip()}" in result["errors"]
    assert not (tmp_path / "manifest.json").exists()


def test_validate_stage1_missing_id_key_is_invalid(tmp_path: Path) -> None:
    result = _run(tmp_path, [{"text_for_tts": "x"}])

    assert result["ok"] is False
    assert "Invalid id format: " in result["errors"]


def test_validate_stage1_strips_whitespace_around_id(tmp_path: Path) -> None:
    speaker_dir = tmp_path / "speaker"
    speaker_dir.mkdir()
    _write_wav(speaker_dir / "zk1.wav", _loud(500))

    result = _run(tmp_path, [{"id": "  zk1 ", "text_for_tts": "x"}])

    assert result["ok"] is True


def test_validate_stage1_reports_duplicate_ids(tmp_path: Path) -> None:
    speaker_dir = tmp_path / "speaker"
    speaker_dir.mkdir()
    _write_wav(speaker_dir / "zk1.wav", _loud(500))

    result = _run(tmp_path, [{"id": "zk1"}, {"id": "zk1"}])

    assert result["ok"] is False
    assert result["errors"].count("Duplicate id: zk1") == 1
    assert "IDs are not a continuous sequence zk1..zkN in order" in result["errors"]


@pytest.mark.parametrize(
    "ids",
    [["zk2"], ["zk1", "zk3"], ["zk2", "zk1"], ["zk0", "zk1"]],
)
def test_validate_stage1_requires_continuous_ordered_ids(tmp_path: Path, ids: list[str]) -> None:
    speaker_dir = tmp_path / "speaker"
    speaker_dir.mkdir()
    for block_id in ids:
        _write_wav(speaker_dir / f"{block_id}.wav", _loud(500))

    result = _run(tmp_path, [{"id": block_id, "text_for_tts": "x"} for block_id in ids])

    assert result["ok"] is False
    assert "IDs are not a continuous sequence zk1..zkN in order" in result["errors"]
    assert not (tmp_path / "manifest.json").exists()


def test_validate_stage1_reports_missing_audio_file(tmp_path: Path) -> None:
    speaker_dir = tmp_path / "speaker"
    speaker_dir.mkdir()
    _write_wav(speaker_dir / "zk1.wav", _loud(500))

    result = _run(tmp_path, [{"id": "zk1"}, {"id": "zk2"}])

    assert result["ok"] is False
    assert result["errors"] == [f"Missing audio file: {speaker_dir / 'zk2.wav'}"]
    assert result["items_count"] == 2


def test_validate_stage1_reports_unreadable_wav(tmp_path: Path) -> None:
    speaker_dir = tmp_path / "speaker"
    speaker_dir.mkdir()
    (speaker_dir / "zk1.wav").write_bytes(b"not audio")

    result = _run(tmp_path, [{"id": "zk1"}])

    assert result["ok"] is False
    assert result["errors"] == [f"Invalid or unreadable WAV file: {speaker_dir / 'zk1.wav'}"]


def test_validate_stage1_short_wav_is_warning_not_error(tmp_path: Path) -> None:
    speaker_dir = tmp_path / "speaker"
    speaker_dir.mkdir()
    _write_wav(speaker_dir / "zk1.wav", _loud(100))

    result = _run(tmp_path, [{"id": "zk1", "text_for_tts": "x"}])

    assert result["ok"] is True
    assert len(result["warnings"]) == 1
    assert "WAV is too short (100 ms < 350 ms)" in result["warnings"][0]
    assert (tmp_path / "manifest.json").exists()


def test_validate_stage1_silent_wav_is_warning(tmp_path: Path) -> None:
    speaker_dir = tmp_path / "speaker"
    speaker_dir.mkdir()
    _write_wav(speaker_dir / "zk1.wav", _silent(1000))

    result = _run(tmp_path, [{"id": "zk1", "text_for_tts": "x"}])

    assert result["ok"] is True
    assert len(result["warnings"]) == 1
    assert "WAV looks silent (rms=0 <= 120)" in result["warnings"][0]


def test_validate_stage1_thresholds_are_configurable_and_zero_disables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    speaker_dir = tmp_path / "speaker"
    speaker_dir.mkdir()
    _write_wav(speaker_dir / "zk1.wav", _silent(100))

    monkeypatch.setenv("VALIDATE_MIN_WAV_DURATION_MS", "0")
    monkeypatch.setenv("VALIDATE_SILENCE_RMS_THRESHOLD", "0")
    disabled = _run(tmp_path, [{"id": "zk1"}])
    assert disabled["warnings"] == []
    assert disabled["validation_config"] == {
        "min_wav_duration_ms": 0,
        "silence_rms_threshold": 0,
    }

    monkeypatch.setenv("VALIDATE_MIN_WAV_DURATION_MS", "200")
    monkeypatch.setenv("VALIDATE_SILENCE_RMS_THRESHOLD", "5")
    enabled = _run(tmp_path, [{"id": "zk1"}])
    assert len(enabled["warnings"]) == 2
    assert enabled["validation_config"]["min_wav_duration_ms"] == 200


def test_validate_stage1_removes_stale_manifest_when_errors_found(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"stale": true}', encoding="utf-8")

    result = _run(tmp_path, [{"id": "zk1"}])  # wav is missing

    assert result["ok"] is False
    assert not manifest.exists()


def test_validate_stage1_overwrites_stale_manifest_on_success(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"stale": true}', encoding="utf-8")
    speaker_dir = tmp_path / "speaker"
    speaker_dir.mkdir()
    _write_wav(speaker_dir / "zk1.wav", _loud(500))

    result = _run(tmp_path, [{"id": "zk1", "text_for_tts": "x"}])

    assert result["ok"] is True
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert "stale" not in data
    assert len(data["items"]) == 1


def test_validate_stage1_result_echoes_paths(tmp_path: Path) -> None:
    result = _run(tmp_path, [{"id": "zk1"}])

    assert result["prepared_path"] == str(tmp_path / "prepared.json")
    assert result["speaker_dir"] == str(tmp_path / "speaker")
    assert result["manifest_path"] == str(tmp_path / "manifest.json")


def test_validate_stage1_rejects_non_dict_item_with_index(tmp_path: Path) -> None:
    prepared = tmp_path / "prepared.json"
    prepared.write_text(
        json.dumps({"items": [{"id": "zk1", "text_for_tts": "a"}, "oops"]}), encoding="utf-8"
    )

    with pytest.raises(ValueError, match=r"items\[1\]"):
        validate_stage1(prepared, tmp_path, tmp_path / "manifest.json")
