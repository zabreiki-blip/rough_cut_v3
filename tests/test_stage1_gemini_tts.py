from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from stage1.gemini_tts import (
    _apply_tempo,
    _is_retryable_tts_error,
    _synthesize_openrouter_wav,
    _write_silence_wav,
    synthesize_tts,
)


class TtsErrorClassificationTests(unittest.TestCase):
    def test_retries_only_transient_http_and_network_failures(self) -> None:
        for status in (429, 500, 502, 503):
            exc = RuntimeError("provider error")
            exc.status_code = status  # type: ignore[attr-defined]
            self.assertTrue(_is_retryable_tts_error("gemini", exc), status)

        for status in (400, 401, 403, 404):
            exc = RuntimeError("provider error")
            exc.status_code = status  # type: ignore[attr-defined]
            self.assertFalse(_is_retryable_tts_error("gemini", exc), status)

        self.assertTrue(_is_retryable_tts_error("openrouter", TimeoutError("request timed out")))
        self.assertFalse(
            _is_retryable_tts_error("gemini", RuntimeError("response blocked by safety policy"))
        )
        self.assertFalse(
            _is_retryable_tts_error("gemini", RuntimeError("unknown permanent failure"))
        )


class OpenRouterTtsTests(unittest.TestCase):
    def test_dedicated_tts_endpoint_wraps_pcm_and_closes_response(self) -> None:
        class Response:
            status_code = 200

            def __init__(self) -> None:
                self.closed = False
                self.content = b"\\x01\\x02" * 24

            def close(self) -> None:
                self.closed = True

        response = Response()
        requests_module = SimpleNamespace(post=lambda *_args, **_kwargs: response)
        settings = SimpleNamespace(
            openrouter_tts_model="test-model",
            openrouter_api_key="key",
            openrouter_base_url="https://example.invalid",
            openrouter_tts_sample_rate=24000,
        )

        with patch.dict(sys.modules, {"requests": requests_module}):
            wav = _synthesize_openrouter_wav("text", settings, "voice")

        self.assertTrue(response.closed)
        self.assertTrue(wav.startswith(b"RIFF"))

    def test_dry_run_never_populates_production_cache(self) -> None:
        from stage1 import gemini_tts as tts

        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            settings = SimpleNamespace(gemini_tts_model="model")
            args = ([("zk1", "versioned-uid", "text")], base, "gemini", "voice", "female", 1.0, settings)
            dry = tts._plan_tts_items(*args, True, None)
            tts._publish_local_item(dry[0], 1, [], [], None)
            real = tts._plan_tts_items(*args, False, None)

            self.assertEqual(real[0][-1], "synthesize")
            self.assertFalse(real[0][5].exists())


class TempoTests(unittest.TestCase):
    def test_missing_ffmpeg_logs_warning_when_speed_change_is_requested(self) -> None:
        with patch("stage1.gemini_tts.shutil.which", return_value=None):
            with self.assertLogs("stage1.gemini_tts", level="WARNING") as logs:
                result = _apply_tempo(b"wav", 1.25)

        self.assertEqual(result, b"wav")
        self.assertIn("speed 1.25 was not applied", "\n".join(logs.output))


class ParallelTtsTests(unittest.TestCase):
    def test_parallel_calls_have_bounded_concurrency_and_ordered_progress(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            prepared = base / "prepared.json"
            prepared.write_text(
                json.dumps(
                    {
                        "items": [
                            {"id": f"zk{index}", "text_for_tts": f"text {index}"}
                            for index in range(1, 6)
                        ]
                    }
                ),
                encoding="utf-8",
            )
            template = base / "template.wav"
            _write_silence_wav(template, duration_ms=100)
            wav_bytes = template.read_bytes()
            lock = threading.Lock()
            active = 0
            peak_active = 0

            def synthesize(*_args, **_kwargs):
                nonlocal active, peak_active
                with lock:
                    active += 1
                    peak_active = max(peak_active, active)
                time.sleep(0.03)
                with lock:
                    active -= 1
                return wav_bytes

            events: list[dict] = []
            with (
                patch("stage1.gemini_tts.shutil.which", return_value="/usr/bin/say"),
                patch("stage1.gemini_tts._synthesize_wav_bytes", side_effect=synthesize),
                patch.dict("os.environ", {"TTS_MAX_WORKERS": "3"}),
            ):
                result = synthesize_tts(
                    prepared,
                    base,
                    provider="macos",
                    voice_name="Yuri",
                    gender="male",
                    progress_callback=events.append,
                )

        generated_ids = [event["id"] for event in events if event["event"] == "item_generated"]
        self.assertEqual(generated_ids, ["zk1", "zk2", "zk3", "zk4", "zk5"])
        self.assertEqual(result["files_count"], 5)
        self.assertGreater(peak_active, 1)
        self.assertLessEqual(peak_active, 3)


if __name__ == "__main__":
    unittest.main()
