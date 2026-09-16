from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from stage1 import normalizer
from stage1.normalization_memory import NormalizationMemory, normalize_marker
from stage1.normalizer import (
    _VIDEO_CUE_KEYWORDS,
    confirm_normalization,
    generate_normalization,
    save_normalization,
)


def _builtin_video_cue_markers() -> set[str]:
    return {normalize_marker(word) for word in _VIDEO_CUE_KEYWORDS}


class ManualCorrectionLearningTests(unittest.TestCase):
    """A manual correction of an *unlabelled* block must teach the learnable
    video-cue lexicon (scope='video_cue'), the same lexicon
    `_seed_video_cue_rules`/`_video_cue_keyword_pattern` already read from,
    so the same unrecognized text is auto-classified next time -- without
    ever growing the lexicon from ordinary narration.
    """

    def _confirm_manual_technical(self, root: Path, text: str) -> Path:
        source = root / "scenario.txt"
        normalized = root / "normalized.json"
        prepared = root / "prepared.json"
        raw = root / "raw.txt"
        memory_path = root / "memory.sqlite3"
        source.write_text(f"{text}\n", encoding="utf-8")

        draft = generate_normalization(source, normalized, memory_path=memory_path, use_llm=False)
        self.assertEqual(draft["blocks"][0]["type"], "unknown")
        self.assertEqual(draft["blocks"][0]["original_label"], "")

        draft["blocks"][0]["type"] = "technical"
        saved = save_normalization(normalized, draft, expected_revision=1)
        self.assertEqual(saved["blocks"][0]["source"], "manual")

        confirm_normalization(
            normalized,
            prepared,
            raw,
            memory_path=memory_path,
            expected_revision=saved["revision"],
        )
        return memory_path

    def test_manual_reclassification_learns_keyword_and_is_reused_without_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory_path = self._confirm_manual_technical(root, "Сольные номера")

            with NormalizationMemory(memory_path) as memory:
                active_markers = {
                    rule["normalized_marker"]
                    for rule in memory.list_rules(scope="video_cue")
                    if rule["status"] == "active"
                }
            self.assertTrue(
                {"сольные", "номера"} & active_markers,
                f"expected a learned marker in {active_markers}",
            )

            source2 = root / "scenario2.txt"
            source2.write_text("Архивные номера выступлений\n", encoding="utf-8")
            result = generate_normalization(
                source2, root / "out2.json", memory_path=memory_path, use_llm=False
            )
            self.assertEqual(result["blocks"][0]["type"], "technical")
            self.assertEqual(result["blocks"][0]["source"], "builtin_rule")

    def test_long_prose_paragraph_does_not_learn_a_keyword(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory_path = self._confirm_manual_technical(
                root,
                "Разные детские воспоминания о том дальнем прекрасном крае",
            )
            with NormalizationMemory(memory_path) as memory:
                markers = {
                    rule["normalized_marker"] for rule in memory.list_rules(scope="video_cue")
                }
            self.assertEqual(markers, _builtin_video_cue_markers())

    def test_url_and_bullet_lines_do_not_learn_keywords(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            normalized = root / "normalized.json"
            prepared = root / "prepared.json"
            raw = root / "raw.txt"
            memory_path = root / "memory.sqlite3"
            source.write_text(
                "Ссылка на видеоматериал есть тут: https://example.com/footage.mp4\n"
                "• Архивные кадры фестиваля\n",
                encoding="utf-8",
            )

            draft = generate_normalization(
                source, normalized, memory_path=memory_path, use_llm=False
            )
            self.assertEqual(draft["blocks"][0]["original_label"], "")
            self.assertEqual(draft["blocks"][1]["original_label"], "")
            self.assertEqual(draft["blocks"][0]["type"], "technical")
            self.assertEqual(draft["blocks"][1]["type"], "technical")
            # Both blocks are already builtin_rule technical; force them into
            # the manual-correction path the same way an operator's edit would
            # (source flips to "manual" only when save_normalization detects
            # an actual change, which a same-value edit here would not do).
            for block in draft["blocks"]:
                block["source"] = "manual"
            normalized.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")

            confirm_normalization(
                normalized, prepared, raw, memory_path=memory_path, expected_revision=1
            )

            with NormalizationMemory(memory_path) as memory:
                markers = {
                    rule["normalized_marker"] for rule in memory.list_rules(scope="video_cue")
                }
            self.assertEqual(markers, _builtin_video_cue_markers())

    def test_labeled_block_manual_technical_change_does_not_learn_video_cue_keywords(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            normalized = root / "normalized.json"
            prepared = root / "prepared.json"
            raw = root / "raw.txt"
            memory_path = root / "memory.sqlite3"
            source.write_text("МАРИЯ: Сольные номера прошли отлично\n", encoding="utf-8")

            draft = generate_normalization(
                source, normalized, memory_path=memory_path, use_llm=False
            )
            self.assertEqual(draft["blocks"][0]["original_label"], "МАРИЯ")

            draft["blocks"][0]["type"] = "technical"
            saved = save_normalization(normalized, draft, expected_revision=1)
            self.assertEqual(saved["blocks"][0]["source"], "manual")

            confirm_normalization(
                normalized,
                prepared,
                raw,
                memory_path=memory_path,
                expected_revision=saved["revision"],
            )

            with NormalizationMemory(memory_path) as memory:
                markers = {
                    rule["normalized_marker"] for rule in memory.list_rules(scope="video_cue")
                }
            self.assertEqual(markers, _builtin_video_cue_markers())

    def test_learning_a_video_cue_keyword_invalidates_the_seed_cache(self) -> None:
        """A manual correction that teaches a brand-new video-cue keyword must
        invalidate the process-local `_seed_video_cue_rules` cache for this
        memory db (the `learned_video_cue_keyword` flag in
        `_learn_from_confirmation`), the same way deleting a rule already
        does -- otherwise a later `_seed_video_cue_rules` call for this db is
        silently skipped by stale "already seeded" bookkeeping. Exercised
        end-to-end: normalize an unrelated scenario first (so the seed cache
        for this specific memory db is already populated), confirm a manual
        reclassification that teaches a new keyword, then normalize a second
        scenario containing that keyword -- all in the same process, without
        restarting and without an LLM.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory_path = root / "memory.sqlite3"

            seed_source = root / "seed.txt"
            seed_source.write_text("Обычный текст без меток\n", encoding="utf-8")
            generate_normalization(
                seed_source, root / "seed.json", memory_path=memory_path, use_llm=False
            )

            with mock.patch.object(
                normalizer,
                "invalidate_video_cue_seed_cache",
                wraps=normalizer.invalidate_video_cue_seed_cache,
            ) as invalidate_spy:
                memory_path = self._confirm_manual_technical(root, "Сольные номера")
                invalidate_spy.assert_called_once_with(memory_path)

            source2 = root / "scenario2.txt"
            source2.write_text("Архивные номера выступлений\n", encoding="utf-8")
            result = generate_normalization(
                source2, root / "out2.json", memory_path=memory_path, use_llm=False
            )
            self.assertEqual(result["blocks"][0]["type"], "technical")
            self.assertEqual(result["blocks"][0]["source"], "builtin_rule")


if __name__ == "__main__":
    unittest.main()
