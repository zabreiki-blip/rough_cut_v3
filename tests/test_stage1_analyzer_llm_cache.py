from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from stage1.analyzer import analyze_script
from stage1.models import NarrationBlock


def _write_scenario(input_path: Path, text: str) -> None:
    input_path.write_text(text, encoding="utf-8")


class Stage1AnalyzerLLMCacheTests(unittest.TestCase):
    def test_second_call_reuses_cache_and_skips_llm(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            input_path = tmp / "scenario.txt"
            output_path = tmp / "scenario_prepared.json"
            raw_output_path = tmp / "scenario_raw.txt"
            narrator_text = "Авторский текст для диктора достаточно длинный для теста."
            _write_scenario(input_path, narrator_text)

            calls = []

            def fake_analyze(settings, chunk):
                calls.append(chunk)
                return [
                    NarrationBlock(id="zk1", text_original=chunk, text_for_tts=chunk)
                ]

            with (
                patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}),
                patch("stage1.analyzer.analyze_with_openrouter", side_effect=fake_analyze),
            ):
                first = analyze_script(
                    input_path=input_path,
                    output_path=output_path,
                    raw_output_path=raw_output_path,
                    use_llm=True,
                )
                second = analyze_script(
                    input_path=input_path,
                    output_path=output_path,
                    raw_output_path=raw_output_path,
                    use_llm=True,
                )

            self.assertEqual(len(calls), 1, "second run must reuse the cached chunk result")
            self.assertTrue(first["used_llm"])
            self.assertTrue(second["used_llm"])
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["items"][0]["text_for_tts"], narrator_text)

    def test_model_change_invalidates_cache(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            input_path = tmp / "scenario.txt"
            output_path = tmp / "scenario_prepared.json"
            raw_output_path = tmp / "scenario_raw.txt"
            narrator_text = "Авторский текст для диктора достаточно длинный для теста."
            _write_scenario(input_path, narrator_text)

            calls = []

            def fake_analyze(settings, chunk):
                calls.append(chunk)
                return [
                    NarrationBlock(id="zk1", text_original=chunk, text_for_tts=chunk)
                ]

            with (
                patch.dict(
                    os.environ,
                    {"OPENROUTER_API_KEY": "test-key", "OPENROUTER_MODEL": "model-a"},
                ),
                patch("stage1.analyzer.analyze_with_openrouter", side_effect=fake_analyze),
            ):
                analyze_script(
                    input_path=input_path,
                    output_path=output_path,
                    raw_output_path=raw_output_path,
                    use_llm=True,
                )

            with (
                patch.dict(
                    os.environ,
                    {"OPENROUTER_API_KEY": "test-key", "OPENROUTER_MODEL": "model-b"},
                ),
                patch("stage1.analyzer.analyze_with_openrouter", side_effect=fake_analyze),
            ):
                analyze_script(
                    input_path=input_path,
                    output_path=output_path,
                    raw_output_path=raw_output_path,
                    use_llm=True,
                )

            self.assertEqual(len(calls), 2, "changing the model must invalidate the cache")

    def test_changing_one_chunk_keeps_other_chunk_cached(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            input_path = tmp / "scenario.txt"
            output_path = tmp / "scenario_prepared.json"
            raw_output_path = tmp / "scenario_raw.txt"
            _write_scenario(input_path, "source text")

            calls = []

            def fake_analyze(settings, chunk):
                calls.append(chunk)
                return [
                    NarrationBlock(id="zk1", text_original=chunk, text_for_tts=chunk)
                ]

            chunks_v1 = ["chunk one", "chunk two"]
            chunks_v2 = ["chunk one CHANGED", "chunk two"]

            with (
                patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}),
                patch("stage1.analyzer._chunk_for_llm", return_value=chunks_v1),
                patch("stage1.analyzer.analyze_with_openrouter", side_effect=fake_analyze),
            ):
                analyze_script(
                    input_path=input_path,
                    output_path=output_path,
                    raw_output_path=raw_output_path,
                    use_llm=True,
                )

            self.assertEqual(sorted(calls), sorted(chunks_v1))
            calls.clear()

            with (
                patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}),
                patch("stage1.analyzer._chunk_for_llm", return_value=chunks_v2),
                patch("stage1.analyzer.analyze_with_openrouter", side_effect=fake_analyze),
            ):
                analyze_script(
                    input_path=input_path,
                    output_path=output_path,
                    raw_output_path=raw_output_path,
                    use_llm=True,
                )

            self.assertEqual(
                calls,
                ["chunk one CHANGED"],
                "only the modified chunk should hit the LLM again",
            )

    def test_failed_chunk_is_not_cached(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            input_path = tmp / "scenario.txt"
            output_path = tmp / "scenario_prepared.json"
            raw_output_path = tmp / "scenario_raw.txt"
            narrator_text = "Первый авторский абзац достаточно длинный для fallback-проверки."
            _write_scenario(input_path, narrator_text)

            calls = []

            def failing_analyze(settings, chunk):
                calls.append(chunk)
                raise RuntimeError("temporary LLM error")

            with (
                patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}),
                patch("stage1.analyzer.analyze_with_openrouter", side_effect=failing_analyze),
            ):
                first = analyze_script(
                    input_path=input_path,
                    output_path=output_path,
                    raw_output_path=raw_output_path,
                    use_llm=True,
                )
                second = analyze_script(
                    input_path=input_path,
                    output_path=output_path,
                    raw_output_path=raw_output_path,
                    use_llm=True,
                )

            self.assertEqual(len(calls), 2, "failed chunks must not be cached")
            self.assertFalse(first["used_llm"])
            self.assertFalse(second["used_llm"])

    def test_corrupted_cache_file_does_not_break_analysis(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            input_path = tmp / "scenario.txt"
            output_path = tmp / "scenario_prepared.json"
            raw_output_path = tmp / "scenario_raw.txt"
            narrator_text = "Авторский текст для диктора достаточно длинный для теста."
            _write_scenario(input_path, narrator_text)

            cache_path = tmp / "work" / "analysis_llm_cache.json"
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text("{not valid json", encoding="utf-8")

            with (
                patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}),
                patch(
                    "stage1.analyzer.analyze_with_openrouter",
                    return_value=[
                        NarrationBlock(
                            id="zk1",
                            text_original=narrator_text,
                            text_for_tts=narrator_text,
                        )
                    ],
                ),
            ):
                result = analyze_script(
                    input_path=input_path,
                    output_path=output_path,
                    raw_output_path=raw_output_path,
                    use_llm=True,
                )

            self.assertTrue(result["used_llm"])
            self.assertEqual(result["extraction_method"], "llm")

            # An empty cache payload should be treated the same way (cache miss).
            cache_path.write_text("", encoding="utf-8")
            with (
                patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}),
                patch(
                    "stage1.analyzer.analyze_with_openrouter",
                    return_value=[
                        NarrationBlock(
                            id="zk1",
                            text_original=narrator_text,
                            text_for_tts=narrator_text,
                        )
                    ],
                ),
            ):
                result2 = analyze_script(
                    input_path=input_path,
                    output_path=output_path,
                    raw_output_path=raw_output_path,
                    use_llm=True,
                )

            self.assertTrue(result2["used_llm"])


if __name__ == "__main__":
    unittest.main()
