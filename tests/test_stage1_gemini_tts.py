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
import wave

from stage1.gemini_tts import (
    _apply_tempo,
    _atomic_write_bytes,
    _is_valid_wav,
    _is_retryable_tts_error,
    _pick_voice,
    _synthesis_fingerprint,
    _synthesize_openrouter_wav,
    _synthesize_with_retry,
    _write_silence_wav,
    synthesize_phrase,
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
            audio, attempts, errors, final_error, style_degraded = _synthesize_with_retry(
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
        self.assertTrue(style_degraded)

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


GEMINI_MODEL = "google/gemini-3.1-flash-tts-preview"
STYLE = "документальное кино"


def _empty_audio_error() -> RuntimeError:
    exc = RuntimeError("Provider returned an empty audio stream after returning HTTP 200")
    exc.status_code = 502  # type: ignore[attr-defined]
    return exc


def _styled_fails_plain_succeeds(wav_bytes: bytes, calls: list[str]):
    def synthesize(*_args, **kwargs):
        style = kwargs.get("tts_style")
        calls.append(style)
        if style:
            raise _empty_audio_error()
        return wav_bytes

    return synthesize


def _openrouter_settings() -> SimpleNamespace:
    return SimpleNamespace(
        tts_provider="openrouter",
        tts_gender="female",
        openrouter_api_key="key",
        openrouter_tts_model=GEMINI_MODEL,
        openrouter_tts_sample_rate=24000,
        openrouter_gemini_tts_voice="Aoede",
        openrouter_gemini_tts_voice_female="Aoede",
        openrouter_gemini_tts_voice_male="Puck",
    )


def _silence_bytes(duration_ms: int = 100) -> bytes:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "t.wav"
        _write_silence_wav(path, duration_ms=duration_ms)
        return path.read_bytes()


class _StyleTtsHelpers:
    def _run_tts(self, base: Path, prepared: Path, calls: list[str], wav: bytes) -> dict:
        with (
            patch("stage1.gemini_tts.get_settings", return_value=_openrouter_settings()),
            patch(
                "stage1.gemini_tts._synthesize_wav_bytes",
                side_effect=_styled_fails_plain_succeeds(wav, calls),
            ),
            patch("stage1.gemini_tts._build_tts_client", return_value=(None, None)),
            patch("stage1.gemini_tts.time.sleep"),
        ):
            return synthesize_tts(
                prepared,
                base,
                provider="openrouter",
                tts_model=GEMINI_MODEL,
                tts_style=STYLE,
            )

    def _prepare(self, base: Path) -> Path:
        prepared = base / "prepared.json"
        prepared.write_text(
            json.dumps({"items": [{"id": "zk1", "block_uid": "uid1", "text_for_tts": "Текст"}]}),
            encoding="utf-8",
        )
        return prepared

    def _fingerprint(self, style: str) -> str:
        return _synthesis_fingerprint(
            "Текст",
            "openrouter",
            "Aoede",
            "female",
            1.0,
            GEMINI_MODEL,
            24000,
            0,
            style,
        )


class StyleDegradationCacheTests(_StyleTtsHelpers, unittest.TestCase):
    def test_degraded_audio_is_not_cached_under_styled_fingerprint(self) -> None:
        wav = _silence_bytes()
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            prepared = self._prepare(base)
            calls: list[str] = []
            result = self._run_tts(base, prepared, calls, wav)

            assets = base / "assets" / "uid1"
            styled = assets / f"{self._fingerprint(STYLE)}.wav"
            plain = assets / f"{self._fingerprint('')}.wav"
            self.assertEqual(calls, [STYLE, ""])
            self.assertFalse(styled.exists())
            self.assertTrue(plain.is_file())
            (entry,) = result["voiceover_assets"]
            self.assertEqual(entry["asset"], str(plain))
            self.assertEqual(entry["fingerprint"], self._fingerprint(""))
            self.assertEqual(entry["style"], "")
            self.assertTrue(entry["degraded"])
            self.assertEqual(result["style_degraded_ids"], ["zk1"])
            alias_meta = json.loads((base / "zk1.wav.meta").read_text(encoding="utf-8"))
            self.assertEqual(alias_meta["fingerprint"], self._fingerprint(""))

    def test_rerun_with_style_retries_styled_synthesis(self) -> None:
        wav = _silence_bytes()
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            prepared = self._prepare(base)
            first_calls: list[str] = []
            self._run_tts(base, prepared, first_calls, wav)
            second_calls: list[str] = []
            result = self._run_tts(base, prepared, second_calls, wav)

            self.assertEqual(second_calls[0], STYLE)
            self.assertEqual(result["files_count"], 1)
            self.assertEqual(result["skipped_count"], 0)

    def test_non_degraded_item_keeps_styled_fingerprint_and_schema(self) -> None:
        wav = _silence_bytes()
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            prepared = self._prepare(base)
            with (
                patch("stage1.gemini_tts.get_settings", return_value=_openrouter_settings()),
                patch("stage1.gemini_tts._synthesize_wav_bytes", return_value=wav),
                patch("stage1.gemini_tts._build_tts_client", return_value=(None, None)),
            ):
                result = synthesize_tts(
                    prepared, base, provider="openrouter", tts_model=GEMINI_MODEL, tts_style=STYLE
                )
            (entry,) = result["voiceover_assets"]
            self.assertEqual(entry["fingerprint"], self._fingerprint(STYLE))
            self.assertNotIn("degraded", entry)
            self.assertEqual(result["style_degraded_ids"], [])

    def test_synthesize_phrase_reports_style_degradation(self) -> None:
        wav = _silence_bytes()
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            calls: list[str] = []
            with (
                patch("stage1.gemini_tts.get_settings", return_value=_openrouter_settings()),
                patch(
                    "stage1.gemini_tts._synthesize_wav_bytes",
                    side_effect=_styled_fails_plain_succeeds(wav, calls),
                ),
                patch("stage1.gemini_tts._build_tts_client", return_value=(None, None)),
                patch("stage1.gemini_tts.time.sleep"),
            ):
                first = synthesize_phrase(
                    "Текст", base, provider="openrouter", tts_model=GEMINI_MODEL, tts_style=STYLE
                )
                second = synthesize_phrase(
                    "Текст", base, provider="openrouter", tts_model=GEMINI_MODEL, tts_style=STYLE
                )

            self.assertEqual(calls, [STYLE, "", STYLE, ""])
            for result in (first, second):
                self.assertEqual(result["style"], "")
                self.assertTrue(result["style_degraded"])
                self.assertTrue(Path(result["file"]).is_file())


def _wav_header(data_size: int, *, nchannels: int = 1, rate: int = 24000, width: int = 2) -> bytes:
    import struct

    return (
        b"RIFF"
        + struct.pack("<I", 36 + data_size)
        + b"WAVEfmt "
        + struct.pack(
            "<IHHIIHH",
            16,
            1,
            nchannels,
            rate,
            rate * nchannels * width,
            nchannels * width,
            width * 8,
        )
        + b"data"
        + struct.pack("<I", data_size)
    )


class WavValidationTests(unittest.TestCase):
    def test_header_only_wav_is_invalid(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.wav"
            path.write_bytes(_wav_header(0))
            self.assertEqual(path.stat().st_size, 44)
            self.assertFalse(_is_valid_wav(path))

    def test_header_declaring_data_but_truncated_is_invalid(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "truncated.wav"
            path.write_bytes(_wav_header(48000) + b"\x00\x00" * 100)
            self.assertFalse(_is_valid_wav(path))

    def test_truncated_real_wav_is_invalid(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "cut.wav"
            _write_silence_wav(path, duration_ms=500)
            data = path.read_bytes()
            path.write_bytes(data[: len(data) // 2])
            self.assertFalse(_is_valid_wav(path))

    def test_valid_silence_wav_is_valid(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ok.wav"
            _write_silence_wav(path, duration_ms=300)
            self.assertTrue(_is_valid_wav(path))

    def test_atomic_write_rejects_broken_wav_without_creating_target(self) -> None:
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "out" / "broken.wav"
            with self.assertRaises(ValueError):
                _atomic_write_bytes(target, _wav_header(0), validate_wav=True)
            self.assertFalse(target.exists())
            self.assertEqual(list(target.parent.iterdir()), [])

    def test_broken_existing_file_is_not_a_cache_hit(self) -> None:
        from stage1 import gemini_tts as tts

        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            settings = SimpleNamespace(
                openrouter_tts_model="model", openrouter_tts_sample_rate=24000
            )
            args = ([("zk1", "uid", "text")], base, "openrouter", "voice", "female", 1.0, settings)
            planned = tts._plan_tts_items(*args, False, None)
            asset = planned[0][5]
            asset.parent.mkdir(parents=True)
            asset.write_bytes(_wav_header(0))
            replanned = tts._plan_tts_items(*args, False, None)
            self.assertEqual(replanned[0][-1], "synthesize")


def _stream_wav(pcm: bytes, *, rate: int = 24000) -> bytes:
    """WAV as emitted by a streaming encoder: unknown sizes are 0xFFFFFFFF."""
    import struct

    header = _wav_header(0, rate=rate)
    header = (
        header[:4] + struct.pack("<I", 0xFFFFFFFF) + header[8:40] + struct.pack("<I", 0xFFFFFFFF)
    )
    return header + pcm


def _macos_settings() -> SimpleNamespace:
    return SimpleNamespace(
        tts_provider="macos",
        tts_gender="female",
        macos_tts_voice="Milena",
        macos_tts_voice_female="Milena",
        macos_tts_voice_male="Yuri",
        macos_tts_rate=170,
        macos_tts_sample_rate=24000,
        macos_connector_url="http://connector.invalid/tts",
        macos_connector_token="",
        macos_connector_timeout=5,
    )


class StreamWavNormalizationTests(unittest.TestCase):
    PCM = b"\x01\x00\x02\x00" * 400

    def test_raw_stream_wav_is_deliberately_invalid(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "stream.wav"
            path.write_bytes(_stream_wav(self.PCM))
            self.assertFalse(_is_valid_wav(path))

    def test_connector_normalizes_stream_wav(self) -> None:
        from stage1.gemini_tts import _synthesize_macos_connector_wav

        response = SimpleNamespace(status_code=200, content=_stream_wav(self.PCM), text="")
        with patch("requests.post", return_value=response):
            data = _synthesize_macos_connector_wav("Текст", _macos_settings(), "Milena")
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "norm.wav"
            path.write_bytes(data)
            self.assertTrue(_is_valid_wav(path))
            with wave.open(str(path), "rb") as wf:
                self.assertEqual(wf.getframerate(), 24000)
                self.assertEqual(wf.readframes(wf.getnframes()), self.PCM)

    def test_connector_keeps_well_formed_wav_untouched(self) -> None:
        from stage1.gemini_tts import _synthesize_macos_connector_wav

        good = _silence_bytes()
        response = SimpleNamespace(status_code=200, content=good, text="")
        with patch("requests.post", return_value=response):
            data = _synthesize_macos_connector_wav("Текст", _macos_settings(), "Milena")
        self.assertEqual(data, good)

    def test_synthesize_tts_publishes_stream_wav_from_connector(self) -> None:
        response = SimpleNamespace(status_code=200, content=_stream_wav(self.PCM), text="")
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            prepared = base / "prepared.json"
            prepared.write_text(
                json.dumps(
                    {"items": [{"id": "zk1", "block_uid": "uid1", "text_for_tts": "Текст"}]}
                ),
                encoding="utf-8",
            )
            with (
                patch("stage1.gemini_tts.get_settings", return_value=_macos_settings()),
                patch("stage1.gemini_tts.shutil.which", return_value=None),
                patch("requests.post", return_value=response),
            ):
                result = synthesize_tts(prepared, base, provider="macos")
            self.assertEqual(result["files_count"], 1)
            self.assertTrue(_is_valid_wav(base / "zk1.wav"))
            (entry,) = result["voiceover_assets"]
            self.assertTrue(_is_valid_wav(Path(entry["asset"])))


class DegradedPublicationTests(_StyleTtsHelpers, unittest.TestCase):
    def test_repeat_degradation_refreshes_alias_with_new_audio(self) -> None:
        wav_a = _silence_bytes(100)
        wav_b = _silence_bytes(200)
        self.assertNotEqual(wav_a, wav_b)
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            prepared = self._prepare(base)
            self._run_tts(base, prepared, [], wav_a)
            plain = base / "assets" / "uid1" / f"{self._fingerprint('')}.wav"
            self.assertEqual((base / "zk1.wav").read_bytes(), wav_a)
            self._run_tts(base, prepared, [], wav_b)
            self.assertEqual(plain.read_bytes(), wav_b)
            self.assertEqual((base / "zk1.wav").read_bytes(), plain.read_bytes())

    def test_mixed_blocks_degrade_independently(self) -> None:
        wav = _silence_bytes()
        calls: list[tuple[str, str]] = []

        def synthesize(_provider, text, *_args, **kwargs):
            style = kwargs.get("tts_style")
            calls.append((text, style))
            if style and text == "Плохой":
                raise _empty_audio_error()
            return wav

        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            prepared = base / "prepared.json"
            prepared.write_text(
                json.dumps(
                    {
                        "items": [
                            {"id": "zk1", "block_uid": "uid1", "text_for_tts": "Хороший"},
                            {"id": "zk2", "block_uid": "uid2", "text_for_tts": "Плохой"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch("stage1.gemini_tts.get_settings", return_value=_openrouter_settings()),
                patch("stage1.gemini_tts._synthesize_wav_bytes", side_effect=synthesize),
                patch("stage1.gemini_tts._build_tts_client", return_value=(None, None)),
                patch("stage1.gemini_tts.time.sleep"),
            ):
                result = synthesize_tts(
                    prepared, base, provider="openrouter", tts_model=GEMINI_MODEL, tts_style=STYLE
                )
            by_id = {entry["id"]: entry for entry in result["voiceover_assets"]}
            self.assertEqual(result["style_degraded_ids"], ["zk2"])
            self.assertNotIn("degraded", by_id["zk1"])
            self.assertTrue(by_id["zk2"]["degraded"])
            good_fp = _synthesis_fingerprint(
                "Хороший", "openrouter", "Aoede", "female", 1.0, GEMINI_MODEL, 24000, 0, STYLE
            )
            bad_plain_fp = _synthesis_fingerprint(
                "Плохой", "openrouter", "Aoede", "female", 1.0, GEMINI_MODEL, 24000, 0, ""
            )
            self.assertEqual(by_id["zk1"]["fingerprint"], good_fp)
            self.assertEqual(by_id["zk2"]["fingerprint"], bad_plain_fp)
            meta1 = json.loads((base / "zk1.wav.meta").read_text(encoding="utf-8"))
            meta2 = json.loads((base / "zk2.wav.meta").read_text(encoding="utf-8"))
            self.assertEqual(meta1["fingerprint"], good_fp)
            self.assertEqual(meta2["fingerprint"], bad_plain_fp)

    def test_progress_event_flags_degraded_item(self) -> None:
        wav = _silence_bytes()
        events: list[dict] = []
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            prepared = self._prepare(base)
            with (
                patch("stage1.gemini_tts.get_settings", return_value=_openrouter_settings()),
                patch(
                    "stage1.gemini_tts._synthesize_wav_bytes",
                    side_effect=_styled_fails_plain_succeeds(wav, []),
                ),
                patch("stage1.gemini_tts._build_tts_client", return_value=(None, None)),
                patch("stage1.gemini_tts.time.sleep"),
            ):
                synthesize_tts(
                    prepared,
                    base,
                    provider="openrouter",
                    tts_model=GEMINI_MODEL,
                    tts_style=STYLE,
                    progress_callback=events.append,
                )
        (generated,) = [e for e in events if e["event"] == "item_generated"]
        self.assertTrue(generated["style_degraded"])

    def test_run_without_style_after_degraded_run_is_plain_cache_hit(self) -> None:
        wav = _silence_bytes()
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            prepared = self._prepare(base)
            self._run_tts(base, prepared, [], wav)
            calls: list[str] = []
            with (
                patch("stage1.gemini_tts.get_settings", return_value=_openrouter_settings()),
                patch(
                    "stage1.gemini_tts._synthesize_wav_bytes",
                    side_effect=_styled_fails_plain_succeeds(wav, calls),
                ),
                patch("stage1.gemini_tts._build_tts_client", return_value=(None, None)),
            ):
                result = synthesize_tts(
                    prepared, base, provider="openrouter", tts_model=GEMINI_MODEL, tts_style=""
                )
            self.assertEqual(calls, [])
            self.assertEqual(result["files_count"], 0)
            self.assertEqual(result["skipped_count"], 1)
            (entry,) = result["voiceover_assets"]
            self.assertEqual(entry["fingerprint"], self._fingerprint(""))
            self.assertNotIn("degraded", entry)
            self.assertEqual(result["style_degraded_ids"], [])


class PhraseDryRunSchemaTests(unittest.TestCase):
    def test_dry_run_phrase_reports_style_degraded_false(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch("stage1.gemini_tts.get_settings", return_value=_openrouter_settings()):
                first = synthesize_phrase(
                    "Текст", base, provider="openrouter", dry_run=True, tts_style=STYLE
                )
                second = synthesize_phrase(
                    "Текст", base, provider="openrouter", dry_run=True, tts_style=STYLE
                )
            self.assertTrue(second.get("skipped"))
            for result in (first, second):
                self.assertIn("style_degraded", result)
                self.assertFalse(result["style_degraded"])


if __name__ == "__main__":
    unittest.main()
