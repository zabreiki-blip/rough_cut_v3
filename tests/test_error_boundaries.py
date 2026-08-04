from __future__ import annotations

from pathlib import Path
import subprocess
from unittest.mock import patch
import wave

from stage1.gemini_tts import _apply_tempo
from stage1.validator import _audio_metrics_wav


def test_optional_tempo_timeout_keeps_original_audio() -> None:
    with (
        patch("stage1.gemini_tts.shutil.which", return_value="/usr/bin/ffmpeg"),
        patch(
            "stage1.gemini_tts.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["ffmpeg"], 180),
        ),
        patch("stage1.gemini_tts.logger.warning") as warning,
    ):
        result = _apply_tempo(b"original-wav", 1.25)

    assert result == b"original-wav"
    warning.assert_called_once()
    assert "timed out" in warning.call_args.args[0]


def test_unreadable_wav_keeps_sentinel_contract_and_logs_reason(tmp_path: Path) -> None:
    broken = tmp_path / "broken.wav"
    with (
        patch("stage1.validator.wave.open", side_effect=wave.Error("bad header")),
        patch("stage1.validator.logger.warning") as warning,
    ):
        assert _audio_metrics_wav(broken) == (0, 0, 0)

    warning.assert_called_once()
    assert warning.call_args.args[1] == broken
