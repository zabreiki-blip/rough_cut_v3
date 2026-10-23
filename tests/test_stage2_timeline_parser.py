"""Tests for Stage 2 timeline_parser ZK ID contract."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from stage2.models import FullTimeline, TimelineSegment
from stage2.timeline_parser import (
    _merge_adjacent_sync_fragments,
    _trim_adjacent_padding,
    parse_normalized_timeline,
    parse_timeline,
)


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
                    {
                        "order": 1,
                        "type": "voiceover",
                        "normalized_text": "Закадр",
                        "versioning": {"change_status": "added"},
                    },
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

            self.assertEqual(
                [item.type for item in timeline.segments], ["zk", "snh", "host", "tech"]
            )
            self.assertEqual(timeline.segments[1].speaker, "Иван Иванов")
            self.assertEqual(timeline.segments[1].timecode, "01:02:03")
            self.assertEqual(timeline.segments[2].id, "host1")
            self.assertEqual(timeline.segments[0].revision_status, "added")

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

    def test_confirmed_match_pending_retime_review_is_not_admitted_to_build(self) -> None:
        """Audit 2026-10-21 #1: the kept manual range must wait for explicit re-confirmation."""
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            document = {
                "status": "confirmed",
                "blocks": [
                    {
                        "uid": "edited-uid",
                        "order": 1,
                        "type": "sync",
                        "normalized_text": "Ответ героя",
                    },
                    {
                        "uid": "plain-uid",
                        "order": 2,
                        "type": "sync",
                        "normalized_text": "Другой ответ",
                    },
                ],
            }
            matches = {
                "edited-uid": {
                    "source_id": "ivan_01",
                    "source_in_sec": 8.4,
                    "source_out_sec": 14.0,
                    "confidence": 0.9,
                    "status": "confirmed",
                    "metadata": {"retime_pending_review": True, "retime_review_for": "fp"},
                },
                "plain-uid": {
                    "source_id": "ivan_01",
                    "source_in_sec": 20.0,
                    "source_out_sec": 25.0,
                    "confidence": 0.9,
                    "status": "confirmed",
                    "metadata": {"retime_pending_review": False},
                },
            }

            timeline = parse_normalized_timeline(
                document, base, base, "TEST", sync_matches=matches, sync_sources={}
            )

            edited, plain = timeline.segments
            self.assertEqual(edited.match_status, "needs_review")
            self.assertNotEqual(edited.duration_sec, 5.6)
            self.assertEqual(plain.match_status, "confirmed")
            self.assertEqual(plain.duration_sec, 5.0)

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
                document,
                base,
                base,
                "TEST",
                sync_matches=matches,
            )

            self.assertEqual(
                [item.block_uid for item in timeline.segments],
                [
                    "teaser-block",
                    "full-block",
                ],
            )
            self.assertEqual(
                [item.source_in_sec for item in timeline.segments],
                [
                    120.0,
                    120.0,
                ],
            )
            self.assertEqual(
                [item.source_out_sec for item in timeline.segments],
                [
                    124.0,
                    132.0,
                ],
            )
            self.assertEqual([item.start_sec for item in timeline.segments], [0.0, 4.0])
            self.assertEqual([item.duration_sec for item in timeline.segments], [4.0, 12.0])

    def test_sync_segment_records_match_diagnostics(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            document = {
                "status": "confirmed",
                "blocks": [
                    {
                        "uid": "a",
                        "order": 1,
                        "type": "sync",
                        "speaker": "ИВАН",
                        "normalized_text": "Первый",
                    },
                    {
                        "uid": "b",
                        "order": 2,
                        "type": "sync",
                        "speaker": "ПЕТР",
                        "normalized_text": "Второй",
                    },
                ],
            }
            matches = {
                "a": {
                    "source_id": "ivan_01",
                    "source_in_sec": 1.0,
                    "source_out_sec": 4.0,
                    "confidence": 0.9,
                    "status": "confirmed",
                    "strategy": "fuzzy",
                    "metadata": {"manual_updated_at": "2026-10-20T10:00:00+00:00"},
                },
                "b": {
                    "status": "not_found",
                    "strategy": "no_candidates",
                    "metadata": {"unmatched_reason": "no_candidate"},
                },
            }

            first, second = parse_normalized_timeline(
                document, base, base, "TEST", sync_matches=matches, sync_sources={}
            ).segments

            self.assertTrue(first.manual_range)
            self.assertEqual(first.match_strategy, "fuzzy")
            self.assertEqual(first.unmatched_reason, "")
            self.assertFalse(second.manual_range)
            self.assertEqual(second.unmatched_reason, "no_candidate")
            self.assertEqual(second.match_strategy, "no_candidates")

    def test_merged_sync_fragment_keeps_merged_uids(self) -> None:
        def seg(uid: str, s_in: float, s_out: float) -> TimelineSegment:
            return TimelineSegment(
                id=uid,
                type="snh",
                text=uid,
                duration_sec=s_out - s_in,
                block_uid=uid,
                parent_block_uid="parent",
                source_id="x",
                source_in_sec=s_in,
                source_out_sec=s_out,
                speech_in_sec=s_in,
                speech_out_sec=s_out,
                match_status="auto_matched",
            )

        merged = _merge_adjacent_sync_fragments(
            [seg("p::fragment:1", 0, 2), seg("p::fragment:2", 2, 4), seg("p::fragment:3", 4, 6)]
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].merged_from, ["p::fragment:2", "p::fragment:3"])

    def test_one_sync_block_expands_to_independently_matched_fragments(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            document = {
                "status": "confirmed",
                "blocks": [
                    {
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
                    }
                ],
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
                document,
                base,
                base,
                "TEST",
                sync_matches=matches,
                sync_sources=sources,
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
            self.assertTrue(
                all(item.parent_block_uid == "irina-block" for item in timeline.segments)
            )
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
                "blocks": [
                    {
                        "uid": "expanded-block",
                        "order": 1,
                        "type": "sync",
                        "normalized_text": "Первая часть Вторая часть",
                        "sync_fragments": [
                            {"timecode_start": "00:10", "text": "Первая часть"},
                            {"timecode_start": "00:20", "text": "Вторая часть"},
                        ],
                    }
                ],
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
                document,
                base,
                base,
                "TEST",
                sync_matches=legacy_matches,
            )

            self.assertEqual([item.match_status for item in timeline.segments], ["", ""])
            self.assertEqual([item.duration_sec for item in timeline.segments], [10.0, 10.0])

    def test_life_block_becomes_non_speech_graphic_placeholder(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            timeline = parse_normalized_timeline(
                {
                    "status": "confirmed",
                    "blocks": [
                        {
                            "uid": "life-block",
                            "order": 1,
                            "type": "life",
                            "normalized_text": "01.10 Подойду к тебе, шепну на ушко",
                            "source_description": "концерт Фриске",
                            "timecode_start": "01.10",
                        }
                    ],
                },
                base,
                base,
                "TEST",
            )

            self.assertEqual(len(timeline.segments), 1)
            segment = timeline.segments[0]
            self.assertEqual(segment.id, "life1")
            self.assertEqual(segment.type, "life")
            self.assertEqual(segment.speaker, "ЛАЙФ")
            self.assertEqual(TimelineSegment.from_dict(segment.to_dict()).type, "life")
            self.assertEqual(segment.role, "концерт Фриске")
            self.assertEqual(segment.timecode, "01.10")
            self.assertEqual(segment.duration_sec, 10.0)
            self.assertTrue(segment.placeholder_path.endswith("life1_placeholder.png"))
            self.assertEqual(segment.spoken_text, "")


def _sync(seg_id: str, s_in: float, s_out: float, sp_in: float, sp_out: float, **kw):
    fields = dict(
        id=seg_id,
        type="snh",
        text=seg_id,
        duration_sec=s_out - s_in,
        source_id="src1",
        source_in_sec=s_in,
        source_out_sec=s_out,
        speech_in_sec=sp_in,
        speech_out_sec=sp_out,
        match_status="auto_matched",
    )
    fields.update(kw)
    return TimelineSegment(**fields)


def _frames(value: float, fps: int = 25) -> float:
    return value * fps


class TrimAdjacentPaddingTest(unittest.TestCase):
    def _brik_pair(self) -> list[TimelineSegment]:
        # snh36/snh37 from BRIK: padding 2.0 / 1.5 around speech, 1.735 s overlap.
        return [
            _sync("snh36", 610.8, 624.315, 612.8, 622.815),
            _sync("snh37", 622.58, 629.336, 624.58, 627.836),
        ]

    def test_brik_pair_has_no_overlap_and_keeps_speech(self) -> None:
        a, b = self._brik_pair()
        _trim_adjacent_padding([a, b], 25)
        self.assertEqual(a.source_out_sec, b.source_in_sec)
        self.assertAlmostEqual(
            _frames(a.source_out_sec), round(_frames(a.source_out_sec)), places=6
        )
        self.assertGreaterEqual(a.source_out_sec, a.speech_out_sec)
        self.assertLessEqual(b.source_in_sec, b.speech_in_sec)
        self.assertAlmostEqual(a.duration_sec, a.source_out_sec - a.source_in_sec)
        self.assertAlmostEqual(b.duration_sec, b.source_out_sec - b.source_in_sec)
        self.assertEqual(a.source_in_sec, 610.8)
        self.assertEqual(b.source_out_sec, 629.336)
        self.assertAlmostEqual(a.trim["trimmed_tail_sec"], 624.315 - a.source_out_sec, places=5)
        self.assertAlmostEqual(b.trim["trimmed_head_sec"], b.source_in_sec - 622.58, places=5)
        self.assertEqual(a.trim["with_segment_id"], "snh37")
        self.assertEqual(b.trim["with_segment_id"], "snh36")
        self.assertEqual(a.trim["rule"], "speech_gap_midpoint")
        self.assertFalse(a.trim["reverse_jump"])
        self.assertFalse(a.trim["speech_overlap"])

    def test_reverse_source_order_is_symmetric(self) -> None:
        a, b = self._brik_pair()
        _trim_adjacent_padding([b, a], 25)
        self.assertEqual(a.source_out_sec, b.source_in_sec)
        self.assertTrue(a.trim["reverse_jump"])
        self.assertTrue(b.trim["reverse_jump"])
        self.assertGreaterEqual(a.source_out_sec, a.speech_out_sec)
        self.assertLessEqual(b.source_in_sec, b.speech_in_sec)

    def test_speech_overlap_is_not_trimmed_but_flagged(self) -> None:
        a = _sync("snh1", 0.0, 12.0, 2.0, 10.0)
        b = _sync("snh2", 8.0, 20.0, 9.0, 18.0)
        _trim_adjacent_padding([a, b], 25)
        self.assertEqual((a.source_out_sec, b.source_in_sec), (12.0, 8.0))
        self.assertTrue(a.trim["speech_overlap"])
        self.assertTrue(b.trim["speech_overlap"])
        self.assertEqual(a.trim["trimmed_tail_sec"], 0.0)

    def test_different_sources_unchanged(self) -> None:
        a, b = self._brik_pair()
        b.source_id = "src2"
        _trim_adjacent_padding([a, b], 25)
        self.assertEqual((a.source_out_sec, b.source_in_sec), (624.315, 622.58))
        self.assertIsNone(a.trim)
        self.assertIsNone(b.trim)

    def test_zk_or_placeholder_between_breaks_adjacency(self) -> None:
        for between in (
            TimelineSegment(id="zk1", type="zk", text="z", duration_sec=3.0),
            TimelineSegment(
                id="snh9", type="snh", text="p", duration_sec=10.0, match_status="not_found"
            ),
        ):
            a, b = self._brik_pair()
            _trim_adjacent_padding([a, between, b], 25)
            self.assertEqual((a.source_out_sec, b.source_in_sec), (624.315, 622.58))
            self.assertIsNone(a.trim)

    def test_tech_and_zero_duration_do_not_break_adjacency(self) -> None:
        a, b = self._brik_pair()
        tech = TimelineSegment(id="tech1", type="tech", text="t", duration_sec=0.0)
        zero = TimelineSegment(id="zk1", type="zk", text="z", duration_sec=0.0)
        _trim_adjacent_padding([a, tech, zero, b], 25)
        self.assertEqual(a.source_out_sec, b.source_in_sec)

    def test_manual_late_is_kept_and_only_auto_early_is_cut(self) -> None:
        a, b = self._brik_pair()
        b.manual_range = True
        _trim_adjacent_padding([a, b], 25)
        self.assertEqual(b.source_in_sec, 622.58)
        self.assertEqual(a.source_out_sec, 622.815)  # speech end of the early fragment
        self.assertEqual(b.trim["trimmed_head_sec"], 0.0)
        # The manual range still covers the early speech tail: residual overlap is flagged.
        self.assertTrue(a.trim["overlap_kept_manual"])

    def test_manual_early_is_kept_and_only_auto_late_is_cut(self) -> None:
        a, b = self._brik_pair()
        a.manual_range = True
        _trim_adjacent_padding([a, b], 25)
        self.assertEqual(a.source_out_sec, 624.315)
        self.assertEqual(b.source_in_sec, 624.315)
        self.assertLessEqual(b.source_in_sec, b.speech_in_sec)

    def test_both_manual_keep_overlap(self) -> None:
        a, b = self._brik_pair()
        a.manual_range = b.manual_range = True
        _trim_adjacent_padding([a, b], 25)
        self.assertEqual((a.source_out_sec, b.source_in_sec), (624.315, 622.58))
        self.assertTrue(a.trim["overlap_kept_manual"])
        self.assertTrue(b.trim["overlap_kept_manual"])

    def test_no_overlap_no_change(self) -> None:
        a = _sync("snh1", 0.0, 5.0, 1.0, 4.0)
        b = _sync("snh2", 6.0, 10.0, 7.0, 9.0)
        _trim_adjacent_padding([a, b], 25)
        self.assertIsNone(a.trim)
        self.assertEqual((a.source_out_sec, b.source_in_sec), (5.0, 6.0))

    def test_edit_in_never_negative(self) -> None:
        a = _sync("snh1", 0.0, 5.0, 0.5, 4.0)
        b = _sync("snh2", 3.0, 9.0, 4.2, 8.0)
        _trim_adjacent_padding([a, b], 25)
        self.assertGreaterEqual(a.source_in_sec, 0.0)
        self.assertGreaterEqual(b.source_in_sec, 0.0)
        self.assertEqual(a.source_out_sec, b.source_in_sec)

    def test_gap_below_one_frame_still_never_cuts_speech(self) -> None:
        a = _sync("snh1", 0.0, 5.0, 1.0, 4.001)
        b = _sync("snh2", 3.0, 9.0, 4.003, 8.0)
        _trim_adjacent_padding([a, b], 25)
        self.assertGreaterEqual(a.source_out_sec, 4.001)
        self.assertLessEqual(b.source_in_sec, 4.003)
        self.assertEqual(a.source_out_sec, b.source_in_sec)

    def test_to_dict_round_trip_keeps_trim(self) -> None:
        a, b = self._brik_pair()
        _trim_adjacent_padding([a, b], 25)
        self.assertEqual(TimelineSegment.from_dict(a.to_dict()).trim, a.trim)
        legacy = a.to_dict()
        legacy.pop("trim")
        self.assertIsNone(TimelineSegment.from_dict(legacy).trim)


class TrimInParseNormalizedTimelineTest(unittest.TestCase):
    def _timeline(self, matches: dict, blocks: list[dict]) -> FullTimeline:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            return parse_normalized_timeline(
                {"status": "confirmed", "blocks": blocks},
                base,
                base,
                "TEST",
                sync_matches=matches,
            )

    @staticmethod
    def _blocks(*uids: str) -> list[dict]:
        return [
            {
                "uid": uid,
                "order": index,
                "type": "sync",
                "speaker": "ИВАН ПЕТРОВ",
                "normalized_text": f"00.0{index} Реплика номер {index} в сюжете сегодня.",
                "timecode_start": f"00.0{index}",
            }
            for index, uid in enumerate(uids, start=1)
        ]

    @staticmethod
    def _match(a_in, a_out, s_in, s_out):
        return {
            "status": "auto_matched",
            "source_id": "src1",
            "source_in_sec": a_in,
            "source_out_sec": a_out,
            "metadata": {"speech_in_sec": s_in, "speech_out_sec": s_out},
        }

    def test_parse_trims_and_versions_timeline(self) -> None:
        matches = {
            "u1": self._match(610.8, 624.315, 612.8, 622.815),
            "u2": self._match(622.58, 629.336, 624.58, 627.836),
        }
        timeline = self._timeline(matches, self._blocks("u1", "u2"))
        a, b = timeline.segments
        self.assertEqual(a.source_out_sec, b.source_in_sec)
        self.assertEqual(a.trim["with_segment_id"], b.id)
        self.assertAlmostEqual(b.start_sec, a.duration_sec)
        self.assertEqual(timeline.edit_policy_version, 1)
        payload = timeline.to_dict()
        self.assertEqual(payload["edit_policy_version"], 1)
        self.assertEqual(FullTimeline.from_dict(payload).edit_policy_version, 1)
        legacy = dict(payload)
        legacy.pop("edit_policy_version")
        self.assertEqual(FullTimeline.from_dict(legacy).edit_policy_version, 0)

    def test_merge_then_trim(self) -> None:
        # u1a+u1b are one parent block merged into one clip; u2 overlaps the merged clip.
        blocks = [
            {
                "uid": "u1",
                "order": 1,
                "type": "sync",
                "speaker": "ИВАН ПЕТРОВ",
                "normalized_text": "00.01 Первая фраза сюжета здесь. Вторая фраза сюжета тут.",
                "timecode_start": "00.01",
            },
            *[dict(b, order=2) for b in self._blocks("u2")],
        ]
        base = self._timeline({}, blocks)
        ids = [s.block_uid for s in base.segments]
        self.assertEqual(len(ids), 3)  # two fragments of u1 and u2 (no matches)
        matches = {
            ids[0]: self._match(0.0, 6.0, 1.0, 4.0),
            ids[1]: self._match(5.0, 12.0, 4.5, 10.0),
            ids[2]: self._match(11.0, 20.0, 12.0, 18.0),
        }
        timeline = self._timeline(matches, blocks)
        self.assertEqual(len(timeline.segments), 2)
        merged, tail = timeline.segments
        self.assertEqual(len(merged.merged_from), 1)
        self.assertEqual(merged.source_out_sec, tail.source_in_sec)
        self.assertGreaterEqual(merged.source_out_sec, 10.0)
        self.assertLessEqual(tail.source_in_sec, 12.0)


if __name__ == "__main__":
    unittest.main()
