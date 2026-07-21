"""Tests for Stage 2 timeline_parser ZK ID contract."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from stage2.timeline_parser import parse_normalized_timeline, parse_timeline


class ZkIdContractTest(unittest.TestCase):
    def _parse(self, raw_text: str, speaker_dir: Path) -> list[str]:
        tl = parse_timeline(raw_text, speaker_dir, speaker_dir, "TEST")
        return [s.id for s in tl.segments if s.type == "zk"]

    def test_numbered_markers_use_sequential_counter_not_original_number(self) -> None:
        """ЗК10 in raw text must become zk1, not zk10 (Stage 1 renumbered it)."""
        with TemporaryDirectory() as tmp:
            raw = "ЗК10\nТекст первого блока.\n"
            ids = self._parse(raw, Path(tmp))
            self.assertEqual(ids, ["zk1"])

    def test_mixed_numbered_and_unnumbered_no_duplicate_ids(self) -> None:
        """ЗК1 followed by bare ЗК must give zk1, zk2 — not two zk1."""
        with TemporaryDirectory() as tmp:
            raw = "ЗК1\nПервый блок.\nЗК\nВторой блок.\n"
            ids = self._parse(raw, Path(tmp))
            self.assertEqual(ids, ["zk1", "zk2"])

    def test_canonical_markers_zk1_zk2_pass_through(self) -> None:
        """Already canonical ЗК1: ЗК2: markers produce zk1, zk2."""
        with TemporaryDirectory() as tmp:
            raw = "ЗК1: Первый.\nЗК2: Второй.\n"
            ids = self._parse(raw, Path(tmp))
            self.assertEqual(ids, ["zk1", "zk2"])

    def test_decimal_suffix_marker_uses_counter(self) -> None:
        """ЗК4.1 must not produce zk4 — uses counter position instead."""
        with TemporaryDirectory() as tmp:
            raw = "ЗК4.1 Блок с суффиксом.\n"
            ids = self._parse(raw, Path(tmp))
            self.assertEqual(ids, ["zk1"])

    def test_wav_resolved_from_canonical_counter_id(self) -> None:
        """ZK segment wav_path must point to zk{counter}.wav, not original number."""
        from stage1.gemini_tts import _write_silence_wav

        with TemporaryDirectory() as tmp:
            speaker_dir = Path(tmp)
            _write_silence_wav(speaker_dir / "zk1.wav", duration_ms=1000)

            raw = "ЗК10\nТекст первого блока.\n"
            tl = parse_timeline(raw, speaker_dir, speaker_dir, "TEST")
            zk_segs = [s for s in tl.segments if s.type == "zk"]
            self.assertEqual(len(zk_segs), 1)
            self.assertGreater(zk_segs[0].duration_sec, 0, "WAV must be found via canonical ID")


class NormalizedTimelineTest(unittest.TestCase):
    def test_confirmed_blocks_build_without_regex_reparse(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            document = {
                "status": "confirmed",
                "blocks": [
                    {"order": 1, "type": "voiceover", "normalized_text": "Закадр"},
                    {
                        "order": 2,
                        "type": "sync",
                        "normalized_text": "Ответ героя",
                        "original_label": "ГЕРОЙ",
                        "speaker": "Иван Иванов",
                        "role": "эксперт",
                        "timecode_start": "01:02:03",
                    },
                    {
                        "order": 3,
                        "type": "host",
                        "normalized_text": "Реплика ведущего",
                        "speaker": "Ведущий",
                    },
                    {"order": 4, "type": "technical", "normalized_text": "ОТБИВКА"},
                ],
            }

            timeline = parse_normalized_timeline(document, base, base, "TEST")

            self.assertEqual([item.type for item in timeline.segments], ["zk", "snh", "host", "tech"])
            self.assertEqual(timeline.segments[1].speaker, "Иван Иванов")
            self.assertEqual(timeline.segments[1].timecode, "01:02:03")
            self.assertEqual(timeline.segments[2].id, "host1")

    def test_draft_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                parse_normalized_timeline(
                    {"status": "draft", "blocks": []}, Path(tmp), Path(tmp), "TEST"
                )


if __name__ == "__main__":
    unittest.main()
