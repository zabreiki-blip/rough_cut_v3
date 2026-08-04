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


class SnhSyntaxContractTest(unittest.TestCase):
    def test_all_snh_branches_share_segment_and_continuation_contract(self) -> None:
        cases = [
            (
                "prefixed",
                "СНХ ИВАН ИВАНОВ, эксперт, 00:01:02 Первая строка.\n",
                "ИВАН ИВАНОВ",
                "эксперт",
                "00:01:02",
            ),
            (
                "classic",
                "ПЕТР ПЕТРОВ, редактор: 00:02:03 Первая строка.\n",
                "ПЕТР ПЕТРОВ",
                "редактор",
                "00:02:03",
            ),
            (
                "flex",
                "АННА СИДОРОВА (ведущая) 03:04 Первая строка.\n",
                "АННА СИДОРОВА",
                "ведущая",
                "03:04",
            ),
            (
                "name-only",
                "ОЛЕГ СМИРНОВ (оператор)\n04.05 Первая строка.\n",
                "ОЛЕГ СМИРНОВ",
                "оператор",
                "04.05",
            ),
        ]

        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            for branch, prefix, speaker, role, timecode in cases:
                with self.subTest(branch=branch):
                    timeline = parse_timeline(
                        prefix + "Вторая строка.\nЗК1: Следующий сегмент.\n",
                        base,
                        base,
                        "TEST",
                    )

                    self.assertEqual([item.type for item in timeline.segments], ["snh", "zk"])
                    segment = timeline.segments[0]
                    self.assertEqual(segment.id, "snh1")
                    self.assertEqual(segment.text, "Первая строка. Вторая строка.")
                    self.assertEqual(segment.speaker, speaker)
                    self.assertEqual(segment.role, role)
                    self.assertEqual(segment.timecode, timecode)
                    self.assertEqual(segment.duration_sec, 10.0)
                    self.assertEqual(
                        segment.placeholder_path,
                        str(base / "snh1_placeholder.png"),
                    )


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

    def test_confirmed_sync_match_uses_real_range_and_sequence(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            document = {
                "status": "confirmed",
                "blocks": [
                    {
                        "uid": "stable-sync-uid",
                        "order": 1,
                        "type": "sync",
                        "normalized_text": "Ответ героя",
                        "speaker": "Иван Иванов",
                        "timecode_start": "01:02",
                        "timecode_end": "01:09",
                    },
                    {
                        "uid": "fallback-sync-uid",
                        "order": 2,
                        "type": "sync",
                        "normalized_text": "Не найдено",
                        "speaker": "Иван Иванов",
                    },
                ],
            }
            matches = {
                "stable-sync-uid": {
                    "source_id": "ivan_01",
                    "source_in_sec": 61.5,
                    "source_out_sec": 69.25,
                    "confidence": 0.93,
                    "status": "confirmed",
                },
                "fallback-sync-uid": {
                    "source_id": "ivan_01",
                    "source_in_sec": 80.0,
                    "source_out_sec": 90.0,
                    "confidence": 0.55,
                    "status": "needs_review",
                },
            }
            sources = {
                "ivan_01": {
                    "premiere_sequence": "SYNC_IVAN_01",
                    "sequence_id": "premiere-id",
                },
            }

            timeline = parse_normalized_timeline(
                document,
                base,
                base,
                "TEST",
                sync_matches=matches,
                sync_sources=sources,
            )

            first, second = timeline.segments
            self.assertEqual(first.block_uid, "stable-sync-uid")
            self.assertEqual(first.timecode_end, "01:09")
            self.assertEqual(first.duration_sec, 7.75)
            self.assertEqual(first.sequence_name, "SYNC_IVAN_01")
            self.assertEqual(first.sequence_id, "premiere-id")
            self.assertEqual(first.start_sec, 0.0)
            self.assertEqual(second.duration_sec, 10.0)
            self.assertEqual(second.start_sec, 7.75)

    def test_teaser_and_full_sync_reuse_overlapping_source_range(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            document = {
                "status": "confirmed",
                "blocks": [
                    {
                        "uid": "teaser-block",
                        "order": 1,
                        "type": "sync",
                        "normalized_text": "Короткий анонс ответа героя",
                    },
                    {
                        "uid": "full-block",
                        "order": 2,
                        "type": "sync",
                        "normalized_text": "Полный развёрнутый ответ героя",
                    },
                ],
            }
            matches = {
                "teaser-block": {
                    "source_id": "interview-1",
                    "source_in_sec": 120.0,
                    "source_out_sec": 124.0,
                    "confidence": 0.98,
                    "status": "confirmed",
                },
                "full-block": {
                    "source_id": "interview-1",
                    "source_in_sec": 120.0,
                    "source_out_sec": 132.0,
                    "confidence": 0.98,
                    "status": "confirmed",
                },
            }

            timeline = parse_normalized_timeline(
                document, base, base, "TEST", sync_matches=matches,
            )

            self.assertEqual([item.block_uid for item in timeline.segments], [
                "teaser-block", "full-block",
            ])
            self.assertEqual([item.source_in_sec for item in timeline.segments], [
                120.0, 120.0,
            ])
            self.assertEqual([item.source_out_sec for item in timeline.segments], [
                124.0, 132.0,
            ])
            self.assertEqual([item.start_sec for item in timeline.segments], [0.0, 4.0])
            self.assertEqual([item.duration_sec for item in timeline.segments], [4.0, 12.0])

    def test_one_sync_block_expands_to_independently_matched_fragments(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            document = {
                "status": "confirmed",
                "blocks": [{
                    "uid": "irina-block",
                    "order": 1,
                    "type": "sync",
                    "speaker": "ИРИНА КОТОВА",
                    "role": "ИСТОРИК",
                    "normalized_text": "Первая цитата Вторая цитата Третья цитата",
                    "sync_fragments": [
                        {"timecode_start": "01:22:29", "text": "Первая цитата"},
                        {"timecode_start": "01:23:36", "text": "Вторая цитата"},
                        {"timecode_start": "01:24:11", "text": "Третья цитата"},
                    ],
                }],
            }
            matches = {
                f"irina-block::fragment:{index}": {
                    "source_id": "irina_01",
                    "source_in_sec": float(index * 10),
                    "source_out_sec": float(index * 10 + index),
                    "confidence": 0.95,
                    "status": "confirmed",
                }
                for index in (1, 2, 3)
            }
            sources = {
                "irina_01": {
                    "premiere_sequence": "SYNC_IRINA_01",
                    "sequence_id": "premiere-irina",
                }
            }

            timeline = parse_normalized_timeline(
                document, base, base, "TEST",
                sync_matches=matches, sync_sources=sources,
            )

            self.assertEqual([item.id for item in timeline.segments], ["snh1", "snh2", "snh3"])
            self.assertEqual(
                [item.block_uid for item in timeline.segments],
                [
                    "irina-block::fragment:1",
                    "irina-block::fragment:2",
                    "irina-block::fragment:3",
                ],
            )
            self.assertTrue(all(item.parent_block_uid == "irina-block" for item in timeline.segments))
            self.assertEqual(
                [item.timecode for item in timeline.segments],
                ["01:22:29", "01:23:36", "01:24:11"],
            )
            self.assertEqual([item.duration_sec for item in timeline.segments], [1.0, 2.0, 3.0])
            self.assertEqual([item.start_sec for item in timeline.segments], [0.0, 1.0, 3.0])

    def test_multi_fragment_block_does_not_reuse_ambiguous_legacy_ordinal_match(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            document = {
                "status": "confirmed",
                "blocks": [{
                    "uid": "expanded-block",
                    "order": 1,
                    "type": "sync",
                    "normalized_text": "Первая часть Вторая часть",
                    "sync_fragments": [
                        {"timecode_start": "00:10", "text": "Первая часть"},
                        {"timecode_start": "00:20", "text": "Вторая часть"},
                    ],
                }],
            }
            legacy_matches = {
                "snh2": {
                    "source_id": "legacy-source",
                    "source_in_sec": 20.0,
                    "source_out_sec": 25.0,
                    "confidence": 1.0,
                    "status": "confirmed",
                }
            }

            timeline = parse_normalized_timeline(
                document, base, base, "TEST", sync_matches=legacy_matches,
            )

            self.assertEqual([item.match_status for item in timeline.segments], ["", ""])
            self.assertEqual([item.duration_sec for item in timeline.segments], [10.0, 10.0])


if __name__ == "__main__":
    unittest.main()
