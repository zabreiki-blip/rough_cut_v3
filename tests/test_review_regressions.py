"""Regression tests for issues found in REVIEW_REPORT.md 2026-06-09."""
from __future__ import annotations

import json
import unittest.mock
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from stage2.timeline_parser import parse_timeline


class ZkRegexTest(unittest.TestCase):
    """P1: ZK regex must not match ЗАКАДРОВЫЙ and must consume decimal/letter suffixes."""

    def _segments(self, raw: str) -> list:
        with TemporaryDirectory() as tmp:
            return parse_timeline(raw, Path(tmp), Path(tmp), "TEST").segments

    def test_zakadroviy_is_not_a_zk_segment(self) -> None:
        segs = self._segments("ЗАКАДРОВЫЙ текст передачи.\n")
        zk = [s for s in segs if s.type == "zk"]
        self.assertEqual(zk, [], "ЗАКАДРОВЫЙ must not be parsed as a ZK segment")

    def test_zk4_1_text_has_no_suffix_bleed(self) -> None:
        segs = self._segments("ЗК4.1 Блок с точкой.\n")
        zk = [s for s in segs if s.type == "zk"]
        self.assertEqual(len(zk), 1)
        self.assertFalse(
            zk[0].text.startswith(".1"),
            f"Text must not start with '.1', got: {zk[0].text!r}",
        )
        self.assertEqual(zk[0].text.strip(), "Блок с точкой.")

    def test_zk38a_text_has_no_letter_bleed(self) -> None:
        segs = self._segments("ЗК38А Текст блока.\n")
        zk = [s for s in segs if s.type == "zk"]
        self.assertEqual(len(zk), 1)
        self.assertFalse(
            zk[0].text.startswith("А"),
            f"Text must not start with 'А', got: {zk[0].text!r}",
        )
        self.assertEqual(zk[0].text.strip(), "Текст блока.")


class ManualMarkersRawTextTest(unittest.TestCase):
    """P1: manual_markers strategy must produce canonical ЗКN: markers in raw_text."""

    def _analyze(self, input_text: str) -> tuple[list, str]:
        """Returns (blocks, raw_text) from analyze_script on input_text."""
        import tempfile
        from stage1.analyzer import analyze_script

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            inp = base / "scenario.txt"
            inp.write_text(input_text, encoding="utf-8")
            out = base / "prepared.json"
            raw = base / "raw.txt"
            analyze_script(inp, out, raw, use_llm=False)
            payload = json.loads(out.read_text(encoding="utf-8"))
            raw_text = raw.read_text(encoding="utf-8")
            return payload["items"], raw_text

    def test_vo_tag_format_produces_zk_marker_in_raw(self) -> None:
        blocks, raw = self._analyze("[[VO]]Закадровый текст диктора.[[/VO]]\n")
        self.assertTrue(any(b["id"].startswith("zk") for b in blocks))
        self.assertRegex(raw, r"ЗК\d+: Закадровый текст диктора\.")

    def test_vo_prefix_format_produces_zk_marker_in_raw(self) -> None:
        blocks, raw = self._analyze("VO: Текст для диктора.\n")
        self.assertTrue(any(b["id"].startswith("zk") for b in blocks))
        self.assertRegex(raw, r"ЗК\d+: Текст для диктора\.")

    def test_narrator_prefix_produces_zk_marker_in_raw(self) -> None:
        blocks, raw = self._analyze("NARRATOR: Нарратор говорит.\n")
        self.assertTrue(any(b["id"].startswith("zk") for b in blocks))
        self.assertRegex(raw, r"ЗК\d+: Нарратор говорит\.")

    def test_zk_prefix_no_double_marker(self) -> None:
        """ЗК: text must produce ЗК1: text, not ЗК: ЗК1: text."""
        blocks, raw = self._analyze("ЗК: Без дублирования маркера.\n")
        self.assertNotIn("ЗК: ЗК", raw, f"Double marker found in raw: {raw!r}")
        self.assertRegex(raw, r"ЗК\d+: Без дублирования маркера\.")

    def test_merged_manual_blocks_keep_one_canonical_timeline_segment(self) -> None:
        blocks, raw = self._analyze(
            "VO: Короткий первый блок.\n\nVO: Короткий второй блок.\n"
        )

        self.assertEqual(len(blocks), 1)
        self.assertIn("ЗК1: Короткий первый блок. Короткий второй блок.", raw)
        self.assertNotIn("VO:", raw)
        with TemporaryDirectory() as tmp:
            timeline = parse_timeline(raw, Path(tmp), Path(tmp), "TEST")
        self.assertEqual([segment.type for segment in timeline.segments], ["zk"])
        self.assertEqual(
            timeline.segments[0].text,
            "Короткий первый блок. Короткий второй блок.",
        )

    def test_duplicate_manual_blocks_use_distinct_source_ranges(self) -> None:
        blocks, raw = self._analyze(
            "VO: Одинаковый короткий текст.\n\nVO: Одинаковый короткий текст.\n"
        )

        self.assertEqual(len(blocks), 1)
        self.assertEqual(raw.count("ЗК1:"), 1)
        self.assertNotIn("VO:", raw)


class DotTimecodeSnhTest(unittest.TestCase):
    """Dot-separated timecodes (00.42.51) must be excluded from ZK blocks in fallback mode."""

    def test_snh_line_with_dot_timecode_not_in_zk_blocks(self) -> None:
        import tempfile, json as _json
        from stage1.analyzer import analyze_script

        snh_line = (
            "ГАЛИНА СТЕПАНОВА, Генеральный директор УСАДЬБЫ МАРЬИНО: 00.42.51. "
            "Здесь время останавливается, замедляется... 00.43.14"
        )
        scenario = f"Закадровый текст диктора.\n\n{snh_line}\n"

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            inp = base / "s.txt"
            inp.write_text(scenario, encoding="utf-8")
            out = base / "prepared.json"
            raw = base / "raw.txt"
            analyze_script(inp, out, raw, use_llm=False)
            items = _json.loads(out.read_text(encoding="utf-8"))["items"]

        texts = [it["text_for_tts"] for it in items]
        for text in texts:
            self.assertNotIn(
                "ГАЛИНА СТЕПАНОВА", text,
                f"SNH speaker name must not appear in ZK blocks: {text!r}",
            )
            self.assertNotIn(
                "00.42.51", text,
                f"Timecode must not appear in ZK blocks: {text!r}",
            )

    def test_is_sync_or_interview_dot_timecode(self) -> None:
        from stage1.analyzer import _is_sync_or_interview
        line = "ГАЛИНА СТЕПАНОВА, Генеральный директор: 00.42.51. Текст."
        self.assertTrue(_is_sync_or_interview(line))

    def test_is_sync_or_interview_colon_timecode_unchanged(self) -> None:
        from stage1.analyzer import _is_sync_or_interview
        line = "ИВАН ПЕТРОВ, эксперт: 00:42:51 Текст с двоеточием."
        self.assertTrue(_is_sync_or_interview(line))


class SynthesizePhraseTest(unittest.TestCase):
    """P2: synthesize_phrase dry_run must not overwrite existing valid WAV."""

    def test_existing_wav_preserved_in_phrase_dry_run(self) -> None:
        from stage1.gemini_tts import _write_silence_wav, synthesize_phrase

        with TemporaryDirectory() as tmp:
            speaker_dir = Path(tmp)
            wav = speaker_dir / "preview.wav"
            _write_silence_wav(wav, duration_ms=2000)
            original_mtime = wav.stat().st_mtime_ns

            result = synthesize_phrase(
                text="Тест фразы",
                speaker_dir=speaker_dir,
                file_name="preview",
                dry_run=True,
            )

            self.assertEqual(wav.stat().st_mtime_ns, original_mtime)
            self.assertTrue(result.get("skipped"), "Result must report skipped=True")


if __name__ == "__main__":
    unittest.main()
