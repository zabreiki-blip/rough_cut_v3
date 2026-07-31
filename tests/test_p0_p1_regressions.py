"""Regression tests for P0/P1 bugs from audit report 2026-06-09."""
from __future__ import annotations

import json
import unittest.mock
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from stage1.gemini_tts import (
    TtsCancelledError,
    _atomic_write_bytes,
    _meta_path,
    _read_meta_fingerprint,
    _synthesis_fingerprint,
    _write_meta_fingerprint,
    _write_silence_wav,
    synthesize_phrase,
    synthesize_tts,
)
from stage1.validator import validate_stage1


def _make_prepared_json(path: Path, block_id: str = "zk1", text: str = "Тест") -> None:
    path.write_text(
        json.dumps({"items": [{"id": block_id, "text_for_tts": text}]}),
        encoding="utf-8",
    )


def _make_wav_bytes(duration_ms: int = 1000) -> bytes:
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(b"\x00\x00" * int(24000 * duration_ms / 1000))
    return buf.getvalue()


class AtomicTtsPublicationTest(unittest.TestCase):
    def _assert_no_temps(self, directory: Path) -> None:
        self.assertEqual(list(directory.glob(".*.tmp")), [])

    def test_atomic_helper_preserves_old_file_when_replace_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            target = directory / "zk1.wav"
            old_bytes = _make_wav_bytes(500)
            target.write_bytes(old_bytes)

            with unittest.mock.patch(
                "stage1.gemini_tts.os.replace", side_effect=OSError("publish failed")
            ):
                with self.assertRaisesRegex(OSError, "publish failed"):
                    _atomic_write_bytes(target, _make_wav_bytes(1000), validate_wav=True)

            self.assertEqual(target.read_bytes(), old_bytes)
            self._assert_no_temps(directory)

    def test_silence_and_meta_preserve_old_files_when_replace_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            wav = directory / "zk1.wav"
            old_wav = _make_wav_bytes(500)
            wav.write_bytes(old_wav)
            meta = _meta_path(wav)
            meta.write_text('{"fingerprint":"old"}', encoding="utf-8")

            with unittest.mock.patch(
                "stage1.gemini_tts.os.replace", side_effect=OSError("publish failed")
            ):
                with self.assertRaises(OSError):
                    _write_silence_wav(wav, duration_ms=1000)
                with self.assertRaises(OSError):
                    _write_meta_fingerprint(wav, "new")

            self.assertEqual(wav.read_bytes(), old_wav)
            self.assertEqual(meta.read_text(encoding="utf-8"), '{"fingerprint":"old"}')
            self._assert_no_temps(directory)

    def test_batch_preserves_old_wav_when_atomic_publish_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            prepared = directory / "prepared.json"
            _make_prepared_json(prepared)
            wav = directory / "zk1.wav"
            old_bytes = _make_wav_bytes(500)
            wav.write_bytes(old_bytes)

            with unittest.mock.patch(
                "stage1.gemini_tts._synthesize_wav_bytes", return_value=_make_wav_bytes(1000)
            ), unittest.mock.patch(
                "stage1.gemini_tts.os.replace", side_effect=OSError("publish failed")
            ):
                with self.assertRaisesRegex(OSError, "publish failed"):
                    synthesize_tts(
                        prepared, directory, provider="macos", voice_name="Yuri", gender="male"
                    )

            self.assertEqual(wav.read_bytes(), old_bytes)
            self._assert_no_temps(directory)

    def test_phrase_preserves_old_wav_when_atomic_publish_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            wav = directory / "preview.wav"
            old_bytes = _make_wav_bytes(500)
            wav.write_bytes(old_bytes)

            with unittest.mock.patch(
                "stage1.gemini_tts._synthesize_wav_bytes", return_value=_make_wav_bytes(1000)
            ), unittest.mock.patch(
                "stage1.gemini_tts.os.replace", side_effect=OSError("publish failed")
            ):
                with self.assertRaisesRegex(OSError, "publish failed"):
                    synthesize_phrase(
                        "Фраза",
                        directory,
                        provider="macos",
                        voice_name="Yuri",
                        gender="male",
                        file_name="preview.wav",
                    )

            self.assertEqual(wav.read_bytes(), old_bytes)
            self._assert_no_temps(directory)


class DryRunPreservesExistingWavTest(unittest.TestCase):
    """P0: dry_run must not overwrite a valid existing WAV."""

    def test_existing_valid_wav_not_overwritten(self) -> None:
        with TemporaryDirectory() as tmp:
            speaker_dir = Path(tmp)
            prepared = speaker_dir / "prepared.json"
            _make_prepared_json(prepared)

            existing = speaker_dir / "zk1.wav"
            _write_silence_wav(existing, duration_ms=2000)
            original_size = existing.stat().st_size
            original_mtime = existing.stat().st_mtime_ns

            synthesize_tts(prepared, speaker_dir, dry_run=True)

            self.assertEqual(existing.stat().st_size, original_size)
            self.assertEqual(existing.stat().st_mtime_ns, original_mtime)

    def test_missing_wav_is_created_in_dry_run(self) -> None:
        with TemporaryDirectory() as tmp:
            speaker_dir = Path(tmp)
            prepared = speaker_dir / "prepared.json"
            _make_prepared_json(prepared)

            result = synthesize_tts(prepared, speaker_dir, dry_run=True)

            self.assertEqual(result["files_count"], 1)
            self.assertTrue((speaker_dir / "zk1.wav").exists())


class TtsCancellationTest(unittest.TestCase):
    def test_cancellation_after_provider_call_is_never_retried(self) -> None:
        with TemporaryDirectory() as tmp:
            speaker_dir = Path(tmp)
            prepared = speaker_dir / "prepared.json"
            _make_prepared_json(prepared)
            cancelled = False
            progress_events: list[str] = []

            def synthesize_once(*_args, **_kwargs):
                nonlocal cancelled
                cancelled = True
                return _make_wav_bytes(1000)

            with unittest.mock.patch(
                "stage1.gemini_tts.shutil.which", return_value="/usr/bin/say"
            ), unittest.mock.patch(
                "stage1.gemini_tts._synthesize_wav_bytes", side_effect=synthesize_once
            ) as synthesize, unittest.mock.patch(
                "stage1.gemini_tts.time.sleep"
            ) as sleep:
                with self.assertRaises(TtsCancelledError):
                    synthesize_tts(
                        prepared,
                        speaker_dir,
                        provider="macos",
                        voice_name="Yuri",
                        gender="male",
                        cancel_check=lambda: cancelled,
                        progress_callback=lambda item: progress_events.append(item["event"]),
                    )

            synthesize.assert_called_once()
            sleep.assert_not_called()
            self.assertNotIn("item_retrying", progress_events)


class CorruptedWavFailsValidationTest(unittest.TestCase):
    """P1: corrupted/unreadable WAV must produce ok=False and block manifest write."""

    def test_corrupted_wav_is_an_error(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            prepared = base / "prepared.json"
            _make_prepared_json(prepared)

            speaker_dir = base / "speaker"
            speaker_dir.mkdir()
            manifest = base / "manifest.json"

            corrupted = speaker_dir / "zk1.wav"
            corrupted.write_bytes(b"not a wav file")

            result = validate_stage1(prepared, speaker_dir, manifest)

            self.assertFalse(result["ok"])
            self.assertTrue(
                any("zk1.wav" in e for e in result["errors"]),
                f"Expected error about zk1.wav, got: {result['errors']}",
            )
            self.assertFalse(manifest.exists(), "Manifest must not be written for invalid WAV")

    def test_valid_wav_passes_validation(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            prepared = base / "prepared.json"
            _make_prepared_json(prepared, text="Достаточно длинный текст для теста озвучки")

            speaker_dir = base / "speaker"
            speaker_dir.mkdir()
            manifest = base / "manifest.json"

            valid_wav = speaker_dir / "zk1.wav"
            _write_silence_wav(valid_wav, duration_ms=2000)

            result = validate_stage1(prepared, speaker_dir, manifest)

            self.assertFalse(
                any("Invalid or unreadable" in e for e in result["errors"]),
                f"Valid WAV should not produce unreadable error: {result['errors']}",
            )


class StaleWavRegenerationTest(unittest.TestCase):
    """P1: WAV with changed text/params must be regenerated, not silently skipped."""

    _FAKE_WAV_BYTES: bytes = b""  # filled in setUpClass

    @classmethod
    def setUpClass(cls) -> None:
        import io, wave as _wave
        buf = io.BytesIO()
        with _wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(24000)
            wf.writeframes(b"\x00\x00" * 24000)
        cls._FAKE_WAV_BYTES = buf.getvalue()

    def _prepared(self, speaker_dir: Path, text: str) -> Path:
        p = speaker_dir / "prepared.json"
        p.write_text(
            __import__("json").dumps({"items": [{"id": "zk1", "text_for_tts": text}]}),
            encoding="utf-8",
        )
        return p

    def test_wav_with_matching_fingerprint_is_skipped(self) -> None:
        with TemporaryDirectory() as tmp:
            speaker_dir = Path(tmp)
            wav = speaker_dir / "zk1.wav"
            _write_silence_wav(wav, duration_ms=1000)
            fp = _synthesis_fingerprint("Тест", "macos", "Yuri", "male", 1.0, "", 24000, 175)
            _write_meta_fingerprint(wav, fp)
            original_mtime = wav.stat().st_mtime_ns

            prepared = self._prepared(speaker_dir, "Тест")
            with unittest.mock.patch(
                "stage1.gemini_tts._synthesize_wav_bytes",
                return_value=self._FAKE_WAV_BYTES,
            ) as mock_synth:
                result = synthesize_tts(
                    prepared, speaker_dir,
                    provider="macos", voice_name="Yuri", gender="male", speed=1.0,
                )

            mock_synth.assert_not_called()
            self.assertEqual(result["skipped_count"], 1)
            self.assertEqual(wav.stat().st_mtime_ns, original_mtime)

    def test_wav_without_fingerprint_is_regenerated(self) -> None:
        """Existing WAV with no .meta file must be re-synthesized (stale by definition)."""
        with TemporaryDirectory() as tmp:
            speaker_dir = Path(tmp)
            wav = speaker_dir / "zk1.wav"
            _write_silence_wav(wav, duration_ms=1000)
            # No meta file — fingerprint unknown

            prepared = self._prepared(speaker_dir, "Текст")
            with unittest.mock.patch(
                "stage1.gemini_tts._synthesize_wav_bytes",
                return_value=self._FAKE_WAV_BYTES,
            ) as mock_synth:
                result = synthesize_tts(
                    prepared, speaker_dir,
                    provider="macos", voice_name="Yuri", gender="male", speed=1.0,
                )

            mock_synth.assert_called_once()
            self.assertEqual(result["files_count"], 1, "Missing meta must trigger regeneration")

    def test_wav_with_changed_text_is_regenerated(self) -> None:
        with TemporaryDirectory() as tmp:
            speaker_dir = Path(tmp)
            wav = speaker_dir / "zk1.wav"
            _write_silence_wav(wav, duration_ms=1000)
            old_fp = _synthesis_fingerprint("Старый текст", "macos", "Yuri", "male", 1.0)
            _write_meta_fingerprint(wav, old_fp)

            prepared = self._prepared(speaker_dir, "Новый текст")
            with unittest.mock.patch(
                "stage1.gemini_tts._synthesize_wav_bytes",
                return_value=self._FAKE_WAV_BYTES,
            ) as mock_synth:
                result = synthesize_tts(
                    prepared, speaker_dir,
                    provider="macos", voice_name="Yuri", gender="male", speed=1.0,
                )

            mock_synth.assert_called_once()
            self.assertEqual(result["files_count"], 1, "Changed text must trigger regeneration")

    def test_after_regeneration_new_fingerprint_is_written(self) -> None:
        with TemporaryDirectory() as tmp:
            speaker_dir = Path(tmp)
            wav = speaker_dir / "zk1.wav"

            prepared = self._prepared(speaker_dir, "Текст")
            with unittest.mock.patch(
                "stage1.gemini_tts._synthesize_wav_bytes",
                return_value=self._FAKE_WAV_BYTES,
            ):
                synthesize_tts(
                    prepared, speaker_dir,
                    provider="macos", voice_name="Yuri", gender="male", speed=1.0,
                )

            expected_fp = _synthesis_fingerprint("Текст", "macos", "Yuri", "male", 1.0, "", 24000, 175)
            self.assertEqual(_read_meta_fingerprint(wav), expected_fp)

    def test_fingerprint_differs_by_provider_voice_speed(self) -> None:
        fp1 = _synthesis_fingerprint("Текст", "gemini", "en-US-Wavenet-A", "male", 1.0)
        fp2 = _synthesis_fingerprint("Текст", "gemini", "en-US-Wavenet-A", "male", 1.2)
        fp3 = _synthesis_fingerprint("Текст", "openrouter", "en-US-Wavenet-A", "male", 1.0)
        self.assertNotEqual(fp1, fp2, "Different speed must produce different fingerprint")
        self.assertNotEqual(fp1, fp3, "Different provider must produce different fingerprint")


if __name__ == "__main__":
    unittest.main()
