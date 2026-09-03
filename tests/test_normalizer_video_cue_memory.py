from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from stage1.normalization_memory import NormalizationMemory, normalize_marker
from stage1.normalizer import (
    _VIDEO_CUE_KEYWORDS,
    _llm_rules_signature,
    generate_normalization,
)


class VideoCueMemoryTests(unittest.TestCase):
    """The video-cue keyword lexicon must be learnable via memory while
    keeping the built-in seed as the base/default behavior.
    """

    def _classify(self, memory_path: Path, text: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            source.write_text(f"{text}\n", encoding="utf-8")
            blocks = generate_normalization(
                source, root / "out.json", memory_path=memory_path, use_llm=False
            )["blocks"]
            return blocks[0]["type"]

    def _classify_after_voiceover(self, memory_path: Path, text: str) -> str:
        # An unmarked, non-video-cue paragraph is only classified as
        # "voiceover" through continuation from a preceding voiceover block
        # (see _annotate_continuation_context); a lone first paragraph with
        # use_llm=False and no matching rule stays "unknown". This mirrors
        # test_normalizer_video_cue.py's `_classify_after_voiceover` helper.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            source.write_text(
                f"ДИКТОР: Первый абзац закадрового текста.\n{text}\n",
                encoding="utf-8",
            )
            blocks = generate_normalization(
                source, root / "out.json", memory_path=memory_path, use_llm=False
            )["blocks"]
            self.assertEqual(blocks[0]["type"], "voiceover")
            return blocks[1]["type"]

    def test_learned_keyword_not_in_seed_makes_block_technical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory_path = Path(tmp) / "memory.sqlite3"
            with NormalizationMemory(memory_path) as memory:
                memory.record_manual("фреска", "technical", scope="video_cue")
            self.assertEqual(
                self._classify(memory_path, "Старинная фреска на стене"), "technical"
            )

    def test_disabled_seed_keyword_stops_matching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory_path = Path(tmp) / "memory.sqlite3"
            with NormalizationMemory(memory_path) as memory:
                # Seed the memory first (idempotent), then disable "бюст".
                rule = memory.record_manual("бюст", "technical", scope="video_cue")
                memory.update_rule(rule["id"], status="disabled")
            self.assertEqual(
                self._classify_after_voiceover(memory_path, "Мраморный бюст Марии"),
                "voiceover",
            )

    def test_marker_scope_rule_does_not_affect_video_cue_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory_path = Path(tmp) / "memory.sqlite3"
            with NormalizationMemory(memory_path) as memory:
                memory.record_manual("фреска", "voiceover", scope="marker")
            # "фреска" is only a marker-scope rule here, not a video_cue rule,
            # so the unmarked paragraph must not be forced to technical.
            self.assertEqual(
                self._classify_after_voiceover(memory_path, "Старинная фреска на стене"),
                "voiceover",
            )

    def test_empty_memory_matches_current_builtin_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory_path = Path(tmp) / "memory.sqlite3"
            self.assertEqual(
                self._classify(memory_path, "Мраморный бюст Марии"), "technical"
            )
            self.assertEqual(
                self._classify_after_voiceover(
                    memory_path,
                    "Игнатий появился на свет в 1822-ом году в семье Иосифа "
                    "и Дороты Лопатинских",
                ),
                "voiceover",
            )

    def test_seeding_is_idempotent_across_two_initializations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory_path = Path(tmp) / "memory.sqlite3"
            source = Path(tmp) / "scenario.txt"
            source.write_text("Мраморный бюст Марии\n", encoding="utf-8")

            generate_normalization(
                source, Path(tmp) / "out1.json", memory_path=memory_path, use_llm=False
            )
            generate_normalization(
                source, Path(tmp) / "out2.json", memory_path=memory_path, use_llm=False
            )

            with NormalizationMemory(memory_path) as memory:
                rules = memory.list_rules(scope="video_cue")

            markers = [rule["marker"] for rule in rules]
            self.assertEqual(len(markers), len(set(markers)))
            # normalize_marker() folds "ё" to "е" (like any other marker), so
            # two keywords that only differ by that letter (e.g. "съёмка" and
            # "съемка") intentionally seed a single shared rule; the expected
            # row count is therefore the number of *distinct normalized*
            # markers, not the raw keyword tuple length.
            expected_marker_count = len({
                normalize_marker(keyword) for keyword in _VIDEO_CUE_KEYWORDS
            })
            self.assertEqual(len(rules), expected_marker_count)

    def test_video_cue_rule_change_invalidates_llm_cache_signature(self) -> None:
        # The LLM result cache is keyed by _llm_rules_signature(); if it did
        # not account for scope='video_cue' rules, teaching/disabling a
        # video-cue keyword would silently keep serving stale cached LLM
        # classifications for paragraphs affected by that keyword.
        with tempfile.TemporaryDirectory() as tmp:
            memory_path = Path(tmp) / "memory.sqlite3"
            with NormalizationMemory(memory_path) as memory:
                baseline_signature = _llm_rules_signature(memory)
                rule = memory.record_manual("фреска", "technical", scope="video_cue")
                after_add_signature = _llm_rules_signature(memory)
                memory.update_rule(rule["id"], status="disabled")
                after_disable_signature = _llm_rules_signature(memory)

        self.assertNotEqual(baseline_signature, after_add_signature)
        self.assertNotEqual(after_add_signature, after_disable_signature)


if __name__ == "__main__":
    unittest.main()
