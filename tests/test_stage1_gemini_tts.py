from __future__ import annotations

import json
import os
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
    _build_wav_from_pcm,
    _is_retryable_tts_error,
    _is_valid_wav,
    _pick_voice,
    _synthesis_fingerprint,
    _synthesize_macos_connector_wav,
    _synthesize_openrouter_wav,
    _synthesize_with_retry,
    _write_silence_wav,
    synthesize_tts,
)


class TtsErrorClassificationTests(unittest.TestCase):
    def test_retries_only_transient_http_and_network_failures(self) -> None:
        for status in (429, 500, 502, 503):
            exc = RuntimeError("provider error")
            exc.status_code = status  # type: ignore[attr-defined]
            self.assertTrue(_is_retryable_tts_error("openrouter", exc), status)

        for status in (400, 401, 403, 404):
            exc = RuntimeError("provider error")
            exc.status_code = status  # type: ignore[attr-defined]
            self.assertFalse(_is_retryable_tts_error("openrouter", exc), status)

        self.assertTrue(_is_retryable_tts_error("openrouter", TimeoutError("request timed out")))
        self.assertFalse(
            _is_retryable_tts_error("openrouter", RuntimeError("response blocked by safety policy"))
        )
        self.assertFalse(
            _is_retryable_tts_error("openrouter", RuntimeError("unknown permanent failure"))
        )


class OpenRouterTtsTests(unittest.TestCase):
    def test_gemini_31_uses_openrouter_voice_configuration(self) -> None:
        settings = SimpleNamespace(
            tts_gender="female",
            openrouter_gemini_tts_voice="Default",
            openrouter_gemini_tts_voice_female="Configured female",
            openrouter_gemini_tts_voice_male="Configured male",
        )

        voice, gender = _pick_voice(
            settings,
            "openrouter",
            None,
            "male",
            "google/gemini-3.1-flash-tts-preview",
        )

        self.assertEqual((voice, gender), ("Configured male", "male"))

    def test_dedicated_tts_endpoint_wraps_pcm_and_closes_response(self) -> None:
        class Response:
            status_code = 200

            def __init__(self) -> None:
                self.closed = False
                self.content = b"\\x01\\x02" * 24
                self.headers = {"X-Generation-Id": "gen-voice-1"}

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

        usage: list[dict] = []
        with patch.dict(sys.modules, {"requests": requests_module}):
            wav = _synthesize_openrouter_wav("text", settings, "voice", usage.append)

        self.assertTrue(response.closed)
        self.assertEqual(usage[0]["generation_id"], "gen-voice-1")
        self.assertTrue(usage[0]["pending"])
        self.assertTrue(wav.startswith(b"RIFF"))

    def test_gemini_model_receives_director_notes_and_selected_model(self) -> None:
        captured = {}

        class Response:
            status_code = 200
            content = b"\\x01\\x02" * 24
            headers = {}

            def close(self) -> None:
                pass

        def post(*_args, **kwargs):
            captured.update(kwargs["json"])
            return Response()

        settings = SimpleNamespace(
            openrouter_tts_model="microsoft/mai-voice-2",
            openrouter_api_key="key",
            openrouter_base_url="https://example.invalid",
            openrouter_tts_sample_rate=24000,
        )
        with patch.dict(sys.modules, {"requests": SimpleNamespace(post=post)}):
            _synthesize_openrouter_wav(
                "Текст ролика",
                settings,
                "Aoede",
                tts_model="google/gemini-3.1-flash-tts-preview",
                tts_style="Серьёзное документальное кино",
            )

        self.assertEqual(captured["model"], "google/gemini-3.1-flash-tts-preview")
        self.assertIn("# DIRECTOR'S NOTES", captured["input"])
        self.assertIn("Серьёзное документальное кино", captured["input"])
        self.assertIn("# TRANSCRIPT\nТекст ролика", captured["input"])
        self.assertIn("Read the TRANSCRIPT aloud verbatim", captured["input"])

    def test_empty_gemini_audio_retries_with_plain_transcript(self) -> None:
        calls = []

        def synthesize(*_args, **kwargs):
            calls.append(kwargs.get("tts_style"))
            if len(calls) == 1:
                exc = RuntimeError(
                    "Provider returned an empty audio stream after returning HTTP 200"
                )
                exc.status_code = 502  # type: ignore[attr-defined]
                raise exc
            return b"wav"

        with (
            patch("stage1.gemini_tts._synthesize_wav_bytes", side_effect=synthesize),
            patch("stage1.gemini_tts.time.sleep"),
        ):
            audio, attempts, errors, final_error, style_dropped = _synthesize_with_retry(
                "openrouter",
                "На экране возникает текст",
                SimpleNamespace(),
                "Aoede",
                1.0,
                None,
                None,
                "google/gemini-3.1-flash-tts-preview",
                "документальное кино",
            )

        self.assertEqual(audio, b"wav")
        self.assertEqual(attempts, 2)
        self.assertEqual(calls, ["документальное кино", ""])
        self.assertEqual(len(errors), 1)
        self.assertIsNone(final_error)
        self.assertTrue(style_dropped)

    def test_mai_direction_maps_to_azure_style(self) -> None:
        captured = {}

        class Response:
            status_code = 200
            content = b"\\x01\\x02" * 24
            headers = {}

            def close(self) -> None:
                pass

        def post(*_args, **kwargs):
            captured.update(kwargs["json"])
            return Response()

        settings = SimpleNamespace(
            openrouter_tts_model="microsoft/mai-voice-2",
            openrouter_api_key="key",
            openrouter_base_url="https://example.invalid",
            openrouter_tts_sample_rate=24000,
        )
        with patch.dict(sys.modules, {"requests": SimpleNamespace(post=post)}):
            _synthesize_openrouter_wav(
                "Текст ролика",
                settings,
                "ru-RU-Masha:MAI-Voice-2",
                tts_model="microsoft/mai-voice-2",
                tts_style="Детский развлекательный контент",
            )

        azure = captured["provider"]["options"]["azure"]
        self.assertEqual(azure["style"], "cheerful")
        self.assertEqual(azure["styledegree"], 1.2)
        self.assertEqual(captured["input"], "Текст ролика")

    def test_style_is_part_of_tts_cache_fingerprint(self) -> None:
        common = ("Текст", "openrouter", "Aoede", "female", 1.0)
        first = _synthesis_fingerprint(*common, model="model", style="детский")
        second = _synthesis_fingerprint(*common, model="model", style="документальный")
        self.assertNotEqual(first, second)

    def test_dry_run_never_populates_production_cache(self) -> None:
        from stage1 import gemini_tts as tts

        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            settings = SimpleNamespace(
                openrouter_tts_model="model", openrouter_tts_sample_rate=24000
            )
            args = (
                [("zk1", "versioned-uid", "text")],
                base,
                "openrouter",
                "voice",
                "female",
                1.0,
                settings,
            )
            dry = tts._plan_tts_items(*args, True, None)
            tts._publish_local_item(dry[0], 1, [], [], None)
            real = tts._plan_tts_items(*args, False, None)

            self.assertEqual(real[0][-1], "synthesize")
            self.assertFalse(real[0][5].exists())


class DegradedStyleCacheTests(unittest.TestCase):
    MODEL = "google/gemini-3.1-flash-tts-preview"

    @staticmethod
    def _settings() -> SimpleNamespace:
        return SimpleNamespace(
            tts_provider="openrouter",
            tts_gender="female",
            openrouter_tts_model="google/gemini-3.1-flash-tts-preview",
            openrouter_tts_sample_rate=24000,
            openrouter_api_key="key",
            openrouter_tts_voice="Aoede",
            openrouter_tts_voice_female="Aoede",
            openrouter_tts_voice_male="Aoede",
        )

    def test_style_less_fallback_is_not_cached_under_styled_fingerprint(self) -> None:
        from stage1 import gemini_tts as tts

        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            prepared = base / "prepared.json"
            prepared.write_text(
                json.dumps({"items": [{"id": "zk1", "text_for_tts": "Текст"}]}),
                encoding="utf-8",
            )
            wav_bytes = _build_wav_from_pcm(b"\x01\x00" * 2400)
            styles: list[str] = []

            def degraded(*_args, **kwargs):
                styles.append(kwargs.get("tts_style"))
                if kwargs.get("tts_style"):
                    exc = RuntimeError("Provider returned an empty audio stream")
                    exc.status_code = 502  # type: ignore[attr-defined]
                    raise exc
                return wav_bytes

            def run(side_effect):
                with (
                    patch("stage1.gemini_tts.get_settings", return_value=self._settings()),
                    patch("stage1.gemini_tts._synthesize_wav_bytes", side_effect=side_effect),
                    patch("stage1.gemini_tts.time.sleep"),
                ):
                    return synthesize_tts(
                        prepared,
                        base,
                        provider="openrouter",
                        voice_name="Aoede",
                        tts_model=self.MODEL,
                        tts_style="документальное кино",
                    )

            first = run(degraded)
            self.assertEqual(styles, ["документальное кино", ""])

            styled_fp = tts._synthesis_fingerprint(
                "Текст", "openrouter", "Aoede", "female", 1.0, self.MODEL, 24000, 0,
                "документальное кино",
            )  # fmt: skip
            plain_fp = tts._synthesis_fingerprint(
                "Текст", "openrouter", "Aoede", "female", 1.0, self.MODEL, 24000, 0, ""
            )
            assets = base / "assets" / "zk1"
            self.assertFalse((assets / f"{styled_fp}.wav").exists())
            self.assertTrue((assets / f"{plain_fp}.wav").is_file())
            self.assertEqual(tts._read_meta_fingerprint(base / "zk1.wav"), plain_fp)
            meta = json.loads((base / "zk1.wav.meta").read_text(encoding="utf-8"))
            self.assertEqual(meta, {"fingerprint": plain_fp})
            self.assertNotEqual(meta["fingerprint"], styled_fp)
            self.assertEqual(first["voiceover_assets"][0]["fingerprint"], plain_fp)
            self.assertEqual(first["voiceover_assets"][0]["asset"], str(assets / f"{plain_fp}.wav"))

            # The next styled run must retry the style instead of hitting cache.
            styles.clear()
            second = run(lambda *_a, **kw: (styles.append(kw.get("tts_style")), wav_bytes)[1])
            self.assertEqual(styles, ["документальное кино"])
            self.assertEqual(second["files_count"], 1)
            self.assertTrue((assets / f"{styled_fp}.wav").is_file())
            self.assertEqual(tts._read_meta_fingerprint(base / "zk1.wav"), styled_fp)
            self.assertEqual(second["voiceover_assets"][0]["fingerprint"], styled_fp)


class WavValidationTests(unittest.TestCase):
    def test_rejects_empty_and_truncated_pcm_but_accepts_complete(self) -> None:
        good = _build_wav_from_pcm(b"\x01\x00" * 4800)
        empty = _build_wav_from_pcm(b"")
        truncated = good[:-1000]
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            results = {}
            for name, data in (("good", good), ("empty", empty), ("cut", truncated)):
                path = base / f"{name}.wav"
                path.write_bytes(data)
                results[name] = _is_valid_wav(path)
            # Header-only file of >= 44 bytes with a lying data length.
            lying = base / "lying.wav"
            lying.write_bytes(good[:60])
            results["lying"] = _is_valid_wav(lying)
        self.assertEqual(results, {"good": True, "empty": False, "cut": False, "lying": False})

    def test_connector_response_uses_same_wav_validation(self) -> None:
        from stage1 import gemini_tts as tts

        settings = SimpleNamespace(
            macos_connector_url="http://connector",
            macos_connector_token="",
            macos_tts_rate=180,
            macos_tts_sample_rate=24000,
            macos_connector_timeout=10,
        )
        good = _build_wav_from_pcm(b"\x01\x00" * 4800)
        cases = {
            "riff-only": b"RIFF-not-really-a-wav" + b"\x00" * 60,
            "empty": _build_wav_from_pcm(b""),
            "truncated": good[:-1000],
        }
        for name, body in cases.items():
            response = SimpleNamespace(status_code=200, content=body, text="")
            with (
                self.subTest(name),
                patch("requests.post", return_value=response),
                self.assertRaises(RuntimeError),
            ):
                tts._synthesize_macos_connector_wav("text", settings, "Yuri")
        ok = SimpleNamespace(status_code=200, content=good, text="")
        with patch("requests.post", return_value=ok):
            self.assertEqual(tts._synthesize_macos_connector_wav("text", settings, "Yuri"), good)


class MacosFingerprintVoiceTests(unittest.TestCase):
    def test_fingerprint_uses_resolved_voice_not_requested_name(self) -> None:
        from stage1 import gemini_tts as tts

        settings = SimpleNamespace(macos_tts_sample_rate=24000, macos_tts_rate=180)

        def plan(voice: str):
            with TemporaryDirectory() as tmp:
                return tts._plan_tts_items(
                    [("zk1", "uid", "text")],
                    Path(tmp),
                    "macos",
                    voice,
                    "female",
                    1.0,
                    settings,
                    False,
                    None,
                )[0][6]

        with (
            patch("stage1.gemini_tts.shutil.which", return_value="/usr/bin/say"),
            patch("stage1.gemini_tts._list_macos_voices", return_value=("Alex", "Yuri", "Milena")),
        ):
            missing_a = plan("Missing A")
            missing_b = plan("Missing B")
            milena = plan("Milena")
            yuri = plan("Yuri")
        # Both unknown voices resolve to the same fallback (Milena).
        self.assertEqual(missing_a, missing_b)
        self.assertEqual(missing_a, milena)
        self.assertNotEqual(milena, yuri)

        # Voice inventory changes -> same requested name, different actual voice.
        with (
            patch("stage1.gemini_tts.shutil.which", return_value="/usr/bin/say"),
            patch("stage1.gemini_tts._list_macos_voices", return_value=("Alex", "Yuri")),
        ):
            self.assertEqual(plan("Milena"), yuri)

    def test_connector_without_local_say_keeps_requested_voice(self) -> None:
        from stage1 import gemini_tts as tts

        settings = SimpleNamespace(macos_tts_sample_rate=24000, macos_tts_rate=180)
        with (
            TemporaryDirectory() as tmp,
            patch("stage1.gemini_tts.shutil.which", return_value=None),
            patch("stage1.gemini_tts._list_macos_voices") as listing,
        ):
            plan_a = tts._plan_tts_items(
                [("zk1", "uid", "text")], Path(tmp), "macos", "A", "female", 1.0,
                settings, False, None,
            )  # fmt: skip
            plan_b = tts._plan_tts_items(
                [("zk1", "uid", "text")], Path(tmp), "macos", "B", "female", 1.0,
                settings, False, None,
            )  # fmt: skip
        listing.assert_not_called()
        self.assertNotEqual(plan_a[0][6], plan_b[0][6])


class TempoTests(unittest.TestCase):
    def test_missing_ffmpeg_logs_warning_when_speed_change_is_requested(self) -> None:
        with patch("stage1.gemini_tts.shutil.which", return_value=None):
            with self.assertLogs("stage1.gemini_tts", level="WARNING") as logs:
                result = _apply_tempo(b"wav", 1.25)

        self.assertEqual(result, b"wav")
        self.assertIn("speed 1.25 was not applied", "\n".join(logs.output))


class MacosFingerprintVoiceDirectTests(unittest.TestCase):
    SETTINGS = SimpleNamespace(macos_command_timeout=900)

    def test_installed_voice_matches_fingerprint_of_requested_name(self) -> None:
        from stage1 import gemini_tts as tts

        with (
            patch("stage1.gemini_tts.shutil.which", return_value="/usr/bin/say"),
            patch("stage1.gemini_tts._list_macos_voices", return_value=("Alex", "Yuri", "Milena")),
        ):
            voice = tts._fingerprint_voice("macos", "Yuri", self.SETTINGS)
        self.assertEqual(voice, "Yuri")
        common = ("Текст", "macos", "female", 1.0, "", 24000, 175)
        self.assertEqual(
            _synthesis_fingerprint(common[0], common[1], voice, *common[2:]),
            _synthesis_fingerprint(common[0], common[1], "Yuri", *common[2:]),
        )

    def test_unavailable_voice_uses_fallback_voice(self) -> None:
        from stage1 import gemini_tts as tts

        with (
            patch("stage1.gemini_tts.shutil.which", return_value="/usr/bin/say"),
            patch("stage1.gemini_tts._list_macos_voices", return_value=("Alex", "Milena")),
        ):
            self.assertEqual(tts._fingerprint_voice("macos", "Missing", self.SETTINGS), "Milena")

    def test_non_macos_provider_keeps_requested_voice(self) -> None:
        from stage1 import gemini_tts as tts

        self.assertEqual(tts._fingerprint_voice("openrouter", "Aoede", self.SETTINGS), "Aoede")


class MacosVoiceInventoryCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        from stage1 import gemini_tts as tts

        tts._list_macos_voices_cached.cache_clear()
        self.addCleanup(tts._list_macos_voices_cached.cache_clear)

    def test_empty_inventory_is_not_cached(self) -> None:
        from stage1 import gemini_tts as tts

        results = [(), ("Alex", "Milena")]
        with patch("stage1.gemini_tts.list_macos_voices", side_effect=results) as listing:
            self.assertEqual(tts._list_macos_voices(timeout=15), ())
            self.assertEqual(tts._list_macos_voices(timeout=15), ("Alex", "Milena"))
            # Non-empty result is cached.
            self.assertEqual(tts._list_macos_voices(timeout=15), ("Alex", "Milena"))
        self.assertEqual(listing.call_count, 2)


class ParallelTtsTests(unittest.TestCase):
    def test_connector_path_limits_client_parallelism(self) -> None:
        for env_value, expected_peak in (("", 1), ("2", 2)):
            with self.subTest(max_parallel=env_value or "default"):
                peak = self._run_connector(env_value)
                self.assertEqual(peak, expected_peak)

    @staticmethod
    def _run_connector(max_parallel: str) -> int:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            prepared = base / "prepared.json"
            prepared.write_text(
                json.dumps(
                    {
                        "items": [
                            {"id": f"zk{index}", "text_for_tts": f"text {index}"}
                            for index in range(1, 7)
                        ]
                    }
                ),
                encoding="utf-8",
            )
            template = base / "template.wav"
            _write_silence_wav(template, duration_ms=100)
            wav_bytes = template.read_bytes()
            lock = threading.Lock()
            state = {"active": 0, "peak": 0}

            def synthesize(*_args, **_kwargs):
                with lock:
                    state["active"] += 1
                    state["peak"] = max(state["peak"], state["active"])
                time.sleep(0.05)
                with lock:
                    state["active"] -= 1
                return wav_bytes

            env = {
                "TTS_MAX_WORKERS": "4",
                "MACOS_CONNECTOR_URL": "http://127.0.0.1:8001/synthesize",
            }
            if max_parallel:
                env["MACOS_CONNECTOR_MAX_PARALLEL"] = max_parallel
            with (
                patch("stage1.gemini_tts.shutil.which", return_value=None),
                patch("stage1.gemini_tts._synthesize_wav_bytes", side_effect=synthesize),
                patch.dict("os.environ", env),
            ):
                if not max_parallel:
                    os.environ.pop("MACOS_CONNECTOR_MAX_PARALLEL", None)
                result = synthesize_tts(
                    prepared,
                    base,
                    provider="macos",
                    voice_name="Yuri",
                    gender="male",
                )
            assert result["files_count"] == 6
            return state["peak"]

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


class MacosConnectorTimeoutTests(unittest.TestCase):
    def _settings(self) -> SimpleNamespace:
        return SimpleNamespace(
            macos_connector_url="http://connector.invalid/synthesize",
            macos_connector_token="token",
            macos_connector_timeout=960,
            macos_tts_rate=175,
            macos_tts_sample_rate=24000,
        )

    def test_read_timeout_is_not_retryable(self) -> None:
        import requests

        with patch("requests.post", side_effect=requests.ReadTimeout("read timed out")):
            with self.assertRaises(RuntimeError) as ctx:
                _synthesize_macos_connector_wav("Текст", self._settings(), "Yuri")

        self.assertFalse(_is_retryable_tts_error("macos", ctx.exception))

    def test_connect_timeout_and_connection_errors_stay_retryable(self) -> None:
        import requests

        for exc in (requests.ConnectTimeout("connect timed out"), requests.ConnectionError("down")):
            with patch("requests.post", side_effect=exc):
                with self.assertRaises(RuntimeError) as ctx:
                    _synthesize_macos_connector_wav("Текст", self._settings(), "Yuri")
            self.assertTrue(_is_retryable_tts_error("macos", ctx.exception))

    def test_read_timeout_does_not_trigger_second_request(self) -> None:
        import requests

        calls = 0

        def post(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise requests.ReadTimeout("read timed out")

        with (
            patch("requests.post", side_effect=post),
            patch("stage1.gemini_tts.shutil.which", return_value=None),
            patch("stage1.gemini_tts.time.sleep") as sleep,
        ):
            audio, attempts, _errors, final_error, _dropped = _synthesize_with_retry(
                "macos", "Текст", self._settings(), "Yuri", 1.0, None, None
            )

        self.assertIsNone(audio)
        self.assertIsNotNone(final_error)
        self.assertEqual((calls, attempts), (1, 1))
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
