from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from stage1.normalizer import generate_normalization


class ScenarioTitleDetectionTests(unittest.TestCase):
    """Regression tests for the RVK_COSTEL/RVK_ULINOVSK false-positive
    voiceover case: an unmarked, all-caps scenario/film title at the very
    start of the script must be classified as technical (like production
    credits), not voiceover.
    """

    def _classify_first_block(self, text: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            source.write_text(f"{text}\nПланы окрестностей – коптер\n", encoding="utf-8")
            blocks = generate_normalization(
                source, root / "out.json", memory_path=root / "memory.sqlite3",
                use_llm=False,
            )["blocks"]
            return blocks[0]

    def test_costel_title_is_technical(self) -> None:
        block = self._classify_first_block(
            "БЕЛАРУСЬ. САРЬЯ. ХРАМ ВО ИМЯ ЛЮБВИ ИГНАТИЯ ЛОПАТИНСКОГО"
        )
        self.assertEqual(block["type"], "technical")

    def test_ulinovsk_title_is_technical(self) -> None:
        block = self._classify_first_block("УЛЬЯНОВСК. ДОМ НИКОЛАЯ ШАТРОВА")
        self.assertEqual(block["type"], "technical")

    def test_all_caps_text_not_at_start_is_not_forced_technical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            source.write_text(
                "ЗАКАДР: Первый абзац закадрового текста.\n"
                "ВНИМАНИЕ ОПАСНОСТЬ ОБВАЛА КРЫШИ\n",
                encoding="utf-8",
            )
            blocks = generate_normalization(
                source, root / "out.json", memory_path=root / "memory.sqlite3",
                use_llm=False,
            )["blocks"]
            self.assertEqual(blocks[0]["type"], "voiceover")
            # Unmarked, all-caps, but not the first block: the title
            # heuristic must not fire here. It continues the preceding
            # voiceover section instead (existing continuation behavior).
            self.assertEqual(blocks[1]["type"], "voiceover")

    def test_ordinary_mixed_case_opening_is_not_forced_technical(self) -> None:
        block = self._classify_first_block(
            "Этот валун — памятная метка на месте, где раньше стоял дом"
        )
        self.assertNotEqual(block["type"], "technical")

    def test_all_caps_opening_with_personal_verb_is_not_forced_technical(self) -> None:
        # A finite verb form means this is spoken narration, not a title,
        # even though it is all-caps and unmarked at the very start.
        block = self._classify_first_block("ЛЮДИ ПРИЕЗЖАЮТ СЮДА ИЗДАЛЕКА")
        self.assertNotEqual(block["type"], "technical")

    def test_very_long_all_caps_run_is_not_forced_technical(self) -> None:
        # A single run-on "word" of 7000 caps letters (no spaces) is not a
        # title, even though it matches the all-caps shape: the title
        # heuristic must be bounded by length, not just word count.
        block = self._classify_first_block("А" * 7000)
        self.assertNotEqual(block["type"], "technical")

    def test_long_all_caps_phrase_is_not_forced_technical(self) -> None:
        # A long all-caps narration paragraph (well beyond a real title's
        # word count) must not be misclassified as a title either.
        phrase = " ".join(["СЛОВО"] * 55)
        self.assertGreater(len(phrase), 300)
        block = self._classify_first_block(phrase)
        self.assertNotEqual(block["type"], "technical")

    def test_marked_voiceover_opening_stays_voiceover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            source.write_text("ЗАКАДР: БЕЛАРУСЬ. САРЬЯ. ХРАМ\n", encoding="utf-8")
            block = generate_normalization(
                source, root / "out.json", memory_path=root / "memory.sqlite3",
                use_llm=False,
            )["blocks"][0]
            self.assertEqual(block["type"], "voiceover")
            self.assertEqual(block["original_label"], "ЗАКАДР")


if __name__ == "__main__":
    unittest.main()
