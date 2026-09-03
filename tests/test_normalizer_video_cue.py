from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from stage1.normalizer import generate_normalization


class VideoCueDetectionTests(unittest.TestCase):
    """Regression tests for the RVK_COSTEL false-positive voiceover cases.

    Unmarked paragraphs that describe footage to show on screen (a "video
    cue") must be classified as technical, not voiceover, even when they
    directly follow a voiceover block without their own marker.
    """

    VIDEO_CUE_TEXTS = (
        "Фамильные портреты Лопатинских",
        "Мраморный бюст Марии",
        "Гранитный камень с надписью",
    )

    VOICEOVER_CONTINUATION_TEXTS = (
        "Игнатий появился на свет в 1822-ом году в семье Иосифа и Дороты Лопатинских",
        "Стать хозяйкой богатейших лопатинских угодий мечтали многие девицы",
        "Одухотворённый взгляд, который явно выдавал восхищение его обладательницы",
    )

    ALREADY_CORRECT_TECHNICAL_TEXTS = (
        "Планы окрестностей Сарьи и храма – коптер",
        "Портреты Игнатия Лопатинского",
        "Планы храма",
        "Планы шпилей храма",
    )

    def _classify_after_voiceover(self, candidate_text: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            source.write_text(
                f"ДИКТОР: Первый абзац закадрового текста.\n{candidate_text}\n",
                encoding="utf-8",
            )
            blocks = generate_normalization(
                source,
                root / "out.json",
                memory_path=root / "memory.sqlite3",
                use_llm=False,
            )["blocks"]
            self.assertEqual(blocks[0]["type"], "voiceover")
            return blocks[1]["type"]

    def test_video_cue_descriptions_are_technical_after_voiceover(self) -> None:
        for text in self.VIDEO_CUE_TEXTS:
            with self.subTest(text=text):
                self.assertEqual(self._classify_after_voiceover(text), "technical")

    def test_narrative_continuations_stay_voiceover(self) -> None:
        for text in self.VOICEOVER_CONTINUATION_TEXTS:
            with self.subTest(text=text):
                self.assertEqual(self._classify_after_voiceover(text), "voiceover")

    def test_already_correct_shot_lists_stay_technical(self) -> None:
        for text in self.ALREADY_CORRECT_TECHNICAL_TEXTS:
            with self.subTest(text=text):
                self.assertEqual(self._classify_after_voiceover(text), "technical")

    def test_video_cue_is_technical_without_preceding_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            source.write_text("Мраморный бюст Марии\n", encoding="utf-8")
            block = generate_normalization(
                source,
                root / "out.json",
                memory_path=root / "memory.sqlite3",
                use_llm=False,
            )["blocks"][0]
            self.assertEqual(block["type"], "technical")

    def test_marked_voiceover_with_short_text_stays_voiceover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            source.write_text("ЗАКАДР: Мраморный бюст Марии\n", encoding="utf-8")
            block = generate_normalization(
                source,
                root / "out.json",
                memory_path=root / "memory.sqlite3",
                use_llm=False,
            )["blocks"][0]
            self.assertEqual(block["type"], "voiceover")
            self.assertEqual(block["original_label"], "ЗАКАДР")


if __name__ == "__main__":
    unittest.main()
