from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import stage1.normalizer as normalizer_module

from stage1.normalization_memory import NormalizationMemory, normalize_marker
from stage1.normalizer import (
    NormalizationValidationError,
    RevisionConflictError,
    confirm_normalization,
    generate_normalization,
    import_legacy_normalization,
    is_normalization_confirmed,
    load_normalization,
    preview_learning_candidates,
    reset_normalization,
    save_normalization,
)


class NormalizationMemoryTests(unittest.TestCase):
    def test_marker_normalization_and_manual_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "memory.sqlite3"
            with NormalizationMemory(db) as memory:
                self.assertEqual(normalize_marker(" Диктор-1: "), "диктор")
                created = memory.record_manual("Голос автора", "voiceover")
                self.assertEqual(created["status"], "active")
                resolved = memory.resolve_exact("голос автора:")
                self.assertIsNotNone(resolved)
                self.assertEqual(resolved["rule"]["target_type"], "voiceover")

    def test_llm_candidate_activates_after_distinct_confirmations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with NormalizationMemory(Path(tmp) / "memory.sqlite3") as memory:
                first = memory.record_llm_confirmation("Автор", "voiceover", "source-a")
                duplicate = memory.record_llm_confirmation("Автор", "voiceover", "source-a")
                second = memory.record_llm_confirmation("Автор", "voiceover", "source-b")
                duplicate_after_activation = memory.record_llm_confirmation(
                    "Автор", "voiceover", "source-b"
                )
                self.assertEqual(first["status"], "candidate")
                self.assertEqual(duplicate["confirmations"], 1)
                self.assertEqual(second["status"], "active")
                self.assertEqual(second["confirmations"], 2)
                self.assertEqual(duplicate_after_activation["status"], "active")

    def test_conflicting_manual_rules_disable_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with NormalizationMemory(Path(tmp) / "memory.sqlite3") as memory:
                memory.record_manual("Текст", "voiceover")
                memory.record_manual("Текст", "sync")
                result = memory.resolve_exact("ТЕКСТ")
                self.assertTrue(result["conflict"])
                self.assertEqual({row["status"] for row in memory.list_rules()}, {"conflict"})

    def test_fuzzy_alias_is_suggestion_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with NormalizationMemory(Path(tmp) / "memory.sqlite3") as memory:
                memory.record_manual("голос диктора", "voiceover")
                self.assertIsNone(memory.resolve_exact("голас диктора"))
                suggestion = memory.suggest("голас диктора")
                self.assertEqual(suggestion["rule"]["target_type"], "voiceover")

    def test_manual_rule_overrides_conflicting_llm_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with NormalizationMemory(Path(tmp) / "memory.sqlite3") as memory:
                memory.record_llm_confirmation("Автор", "sync", "source-a")
                memory.record_llm_confirmation("Автор", "sync", "source-b")
                memory.record_manual("Автор", "voiceover")

                resolved = memory.resolve_exact("автор")

                self.assertFalse(resolved["conflict"])
                self.assertEqual(resolved["rule"]["source"], "manual")
                self.assertEqual(resolved["rule"]["target_type"], "voiceover")

    def test_disabled_llm_rule_stays_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with NormalizationMemory(Path(tmp) / "memory.sqlite3") as memory:
                rule = memory.record_llm_confirmation("Автор", "voiceover", "source-a")
                memory.update_rule(rule["id"], status="disabled")
                repeated = memory.record_llm_confirmation("Автор", "voiceover", "source-b")

                self.assertEqual(repeated["status"], "disabled")
                self.assertIsNone(memory.resolve_exact("Автор"))


class NormalizerTests(unittest.TestCase):
    def test_docx_soft_line_break_stays_inside_voiceover_block(self) -> None:
        from docx import Document
        from docx.enum.text import WD_BREAK

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.docx"
            document = Document()
            paragraph = document.add_paragraph("ДИКТОР: Первая строка")
            paragraph.add_run().add_break(WD_BREAK.LINE)
            paragraph.add_run("Вторая строка")
            document.save(source)

            result = generate_normalization(
                source, root / "out.json", memory_path=root / "memory.sqlite3", use_llm=False
            )

            self.assertEqual(len(result["blocks"]), 1)
            self.assertEqual(result["blocks"][0]["type"], "voiceover")
            self.assertEqual(result["blocks"][0]["normalized_text"], "Первая строка\nВторая строка")

    def test_docx_new_paragraph_continues_voiceover_without_repeating_marker(self) -> None:
        from docx import Document

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.docx"
            document = Document()
            document.add_paragraph("ДИКТОР: Первый абзац")
            document.add_paragraph("Второй абзац того же закадра")
            document.add_paragraph("СНХ: Ответ героя")
            document.save(source)

            result = generate_normalization(
                source, root / "out.json", memory_path=root / "memory.sqlite3",
                use_llm=False,
            )

            self.assertEqual(
                [block["type"] for block in result["blocks"]],
                ["voiceover", "voiceover", "sync"],
            )
            self.assertEqual(
                result["blocks"][1]["text_for_tts"],
                "Второй абзац того же закадра",
            )

    def test_unlabelled_paragraph_is_voiceover_continuation_without_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            source.write_text("ДИКТОР: Первый абзац\nПродолжение закадра\n", encoding="utf-8")

            result = generate_normalization(
                source, root / "out.json", memory_path=root / "memory.sqlite3",
                use_llm=False,
            )
            self.assertEqual([block["type"] for block in result["blocks"]], ["voiceover", "voiceover"])
            self.assertEqual(result["blocks"][1]["continuation_of_type"], "voiceover")
            self.assertEqual(result["blocks"][1]["text_for_tts"], "Продолжение закадра")

    def test_generate_preserves_every_paragraph_and_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            output = root / "normalized.json"
            source.write_text(
                "ДИКТОР: Первый закадр\nСНХ: Ответ эксперта\n(Показать карту)\nНеясный абзац\n",
                encoding="utf-8",
            )
            result = generate_normalization(
                source, output, memory_path=root / "memory.sqlite3", use_llm=False
            )
            self.assertEqual(len(result["blocks"]), 4)
            self.assertEqual([item["order"] for item in result["blocks"]], [1, 2, 3, 4])
            self.assertEqual(result["blocks"][0]["type"], "voiceover")
            self.assertEqual(result["blocks"][1]["type"], "sync")
            self.assertEqual(result["blocks"][2]["type"], "technical")
            self.assertEqual(result["blocks"][3]["type"], "unknown")
            self.assertEqual(result["blocks"][0]["original_text"], "ДИКТОР: Первый закадр")

    def test_memory_applies_across_scenarios(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "memory.sqlite3"
            with NormalizationMemory(db) as memory:
                memory.record_manual("Голос автора", "voiceover")
            source = root / "scenario.txt"
            source.write_text("ГОЛОС АВТОРА: Текст\n", encoding="utf-8")
            result = generate_normalization(source, root / "out.json", memory_path=db, use_llm=False)
            self.assertEqual(result["blocks"][0]["type"], "voiceover")
            self.assertEqual(result["blocks"][0]["source"], "memory")

    def test_text_only_edit_does_not_create_learning_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            normalized = root / "normalized.json"
            db = root / "memory.sqlite3"
            source.write_text("ДИКТОР: Исходный текст\n", encoding="utf-8")
            draft = generate_normalization(source, normalized, memory_path=db, use_llm=False)
            draft["blocks"][0]["normalized_text"] = "Исправленный текст"
            saved = save_normalization(normalized, draft, expected_revision=1)
            self.assertEqual(saved["blocks"][0]["source"], "builtin_rule")
            self.assertEqual(
                preview_learning_candidates(normalized, memory_path=db, expected_revision=2), []
            )

    def test_two_accepted_corrections_activate_learned_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "memory.sqlite3"
            for index in (1, 2):
                source = root / f"scenario-{index}.txt"
                normalized = root / f"normalized-{index}.json"
                source.write_text(f"ГОЛОС ХРОНИКИ: Текст {index}\n", encoding="utf-8")
                draft = generate_normalization(source, normalized, memory_path=db, use_llm=False)
                draft["blocks"][0]["type"] = "voiceover"
                draft["blocks"][0]["text_for_tts"] = f"Текст {index}"
                saved = save_normalization(normalized, draft, expected_revision=1)
                candidates = preview_learning_candidates(
                    normalized, memory_path=db, expected_revision=saved["revision"]
                )
                self.assertEqual(len(candidates), 1)
                confirm_normalization(
                    normalized, root / f"prepared-{index}.json", root / f"raw-{index}.txt",
                    memory_path=db, expected_revision=saved["revision"],
                    learning_decisions=[{
                        "candidate_id": candidates[0]["id"], "action": "remember",
                    }],
                )

            with NormalizationMemory(db) as memory:
                resolved = memory.resolve_exact("Голос хроники")
                self.assertIsNotNone(resolved)
                self.assertEqual(resolved["rule"]["source"], "correction")
                self.assertEqual(resolved["rule"]["confirmations"], 2)

    def test_never_decision_suppresses_future_learning_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "memory.sqlite3"

            def corrected_draft(index: int):
                source = root / f"never-{index}.txt"
                normalized = root / f"never-{index}.json"
                source.write_text(f"ГОЛОС ЭПОХИ: Текст {index}\n", encoding="utf-8")
                draft = generate_normalization(source, normalized, memory_path=db, use_llm=False)
                draft["blocks"][0]["type"] = "voiceover"
                draft["blocks"][0]["text_for_tts"] = f"Текст {index}"
                saved = save_normalization(normalized, draft, expected_revision=1)
                return normalized, saved

            normalized, saved = corrected_draft(1)
            candidates = preview_learning_candidates(
                normalized, memory_path=db, expected_revision=saved["revision"]
            )
            confirm_normalization(
                normalized, root / "never-prepared.json", root / "never-raw.txt",
                memory_path=db, expected_revision=saved["revision"],
                learning_decisions=[{
                    "candidate_id": candidates[0]["id"], "action": "never",
                }],
            )
            second_path, second = corrected_draft(2)
            self.assertEqual(
                preview_learning_candidates(
                    second_path, memory_path=db, expected_revision=second["revision"]
                ),
                [],
            )

    def test_sync_metadata_is_normalized_from_known_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            source.write_text(
                "СНХ: Иван Иванов, эксперт, 01.02.03 Ответ героя\n",
                encoding="utf-8",
            )

            result = generate_normalization(
                source, root / "out.json", memory_path=root / "memory.sqlite3", use_llm=False
            )
            block = result["blocks"][0]

            self.assertEqual(block["type"], "sync")
            self.assertEqual(block["speaker"], "Иван Иванов")
            self.assertEqual(block["role"], "эксперт")
            self.assertEqual(block["timecode_start"], "01.02.03")
            self.assertEqual(block["normalized_text"], "Ответ героя")
            self.assertEqual(block["sync_fragments"], [{
                "text": "Ответ героя",
                "timecode_start": "01.02.03",
            }])

    def test_sync_speaker_block_can_contain_multiple_timecoded_fragments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            source.write_text(
                "ИРИНА КОТОВА, ИСТОРИК: 01:22:29. Шодэ приехал к нам из Одессы, где его "
                "семья жила с середины 19-ого века. Образование он получил во Франции "
                "художественное. 01:23:36. Был он человеком очень восприимчивым к разным "
                "стилям. 01:24:11. Он строил особняки многофасадные.\n",
                encoding="utf-8",
            )

            result = generate_normalization(
                source, root / "out.json", memory_path=root / "memory.sqlite3", use_llm=False
            )
            block = result["blocks"][0]

            self.assertEqual(block["type"], "sync")
            self.assertEqual(block["speaker"], "ИРИНА КОТОВА")
            self.assertEqual(block["role"], "ИСТОРИК")
            self.assertEqual(block["timecode_start"], "01:22:29")
            self.assertEqual(block["timecode_end"], "")
            self.assertEqual(block["sync_fragments"], [
                {
                    "text": (
                        "Шодэ приехал к нам из Одессы, где его семья жила с середины "
                        "19-ого века. Образование он получил во Франции художественное."
                    ),
                    "timecode_start": "01:22:29",
                },
                {
                    "text": "Был он человеком очень восприимчивым к разным стилям.",
                    "timecode_start": "01:23:36",
                },
                {
                    "text": "Он строил особняки многофасадные.",
                    "timecode_start": "01:24:11",
                },
            ])

    def test_sync_fragments_survive_docx_soft_line_breaks(self) -> None:
        from docx import Document
        from docx.enum.text import WD_BREAK

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.docx"
            document = Document()
            paragraph = document.add_paragraph(
                "ИРИНА КОТОВА, ИСТОРИК: 01:22:29. Первый фрагмент"
            )
            paragraph.add_run().add_break(WD_BREAK.LINE)
            paragraph.add_run("01:23:36. Второй фрагмент")
            paragraph.add_run().add_break(WD_BREAK.LINE)
            paragraph.add_run("01:24:11. Третий фрагмент")
            document.save(source)

            result = generate_normalization(
                source, root / "out.json", memory_path=root / "memory.sqlite3", use_llm=False
            )

            self.assertEqual(len(result["blocks"]), 1)
            self.assertEqual(
                [item["timecode_start"] for item in result["blocks"][0]["sync_fragments"]],
                ["01:22:29", "01:23:36", "01:24:11"],
            )

    def test_long_formal_speaker_role_is_sync_without_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            source.write_text(
                "ЕЛЕНА САДОВНИК, СТАРШИЙ НАУЧНЫЙ СОТРУДНИК МУЗЕЯ "
                "СИМБИРСКОГО КУПЕЧЕСТВА: 00:24:54. Образование он получил дома.\n",
                encoding="utf-8",
            )

            block = generate_normalization(
                source, root / "out.json", memory_path=root / "memory.sqlite3", use_llm=False
            )["blocks"][0]

            self.assertEqual(block["type"], "sync")
            self.assertEqual(block["speaker"], "ЕЛЕНА САДОВНИК")
            self.assertEqual(block["timecode_start"], "00:24:54")

    def test_name_only_and_continuation_note_speaker_forms_are_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            source.write_text(
                "ЕЛЕНА САДОВНИК: 00:31:16. Семейная история.\n"
                "ИРИНА КОТОВА: (Продолжение синхрона) 01:25:03. Продолжение ответа.\n",
                encoding="utf-8",
            )

            blocks = generate_normalization(
                source, root / "out.json", memory_path=root / "memory.sqlite3", use_llm=False
            )["blocks"]

            self.assertEqual([block["type"] for block in blocks], ["sync", "sync"])
            self.assertEqual([block["speaker"] for block in blocks], ["ЕЛЕНА САДОВНИК", "ИРИНА КОТОВА"])
            self.assertEqual([block["timecode_start"] for block in blocks], ["00:31:16", "01:25:03"])

    def test_unprefixed_shot_plan_is_technical_not_voiceover_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            source.write_text(
                "ЗАКАДР: Основной текст.\nПланы дома Шатрова\nПортреты архитектора Шодэ\n",
                encoding="utf-8",
            )

            blocks = generate_normalization(
                source, root / "out.json", memory_path=root / "memory.sqlite3", use_llm=False
            )["blocks"]

            self.assertEqual([block["type"] for block in blocks], [
                "voiceover", "technical", "technical",
            ])

    def test_sync_fragment_explicit_range_has_optional_end_timecode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            source.write_text(
                "СНХ: Иван Иванов, эксперт, 01:02:03–01:02:08 Ответ героя\n",
                encoding="utf-8",
            )

            result = generate_normalization(
                source, root / "out.json", memory_path=root / "memory.sqlite3", use_llm=False
            )
            block = result["blocks"][0]

            self.assertEqual(block["timecode_start"], "01:02:03")
            self.assertEqual(block["timecode_end"], "01:02:08")
            self.assertEqual(block["sync_fragments"], [{
                "text": "Ответ героя",
                "timecode_start": "01:02:03",
                "timecode_end": "01:02:08",
            }])

    def test_llm_classifies_only_unresolved_without_drops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            source.write_text("ДИКТОР: Готово\nАвтор: Новый текст\n", encoding="utf-8")

            def classifier(items, _hint):
                self.assertEqual(len(items), 1)
                return [{
                    "uid": items[0]["uid"], "type": "voiceover", "confidence": .8,
                    "label_is_marker": True,
                }]

            result = generate_normalization(
                source, root / "out.json", memory_path=root / "memory.sqlite3",
                classifier=classifier,
            )
            self.assertEqual(len(result["blocks"]), 2)
            self.assertEqual(result["blocks"][1]["source"], "llm")
            self.assertEqual(result["blocks"][1]["text_for_tts"], "Новый текст")

    def test_prose_prefix_is_not_stripped_without_marker_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            source.write_text("Москва: сегодня прошёл форум\n", encoding="utf-8")

            def classifier(items, _hint):
                return [{
                    "uid": items[0]["uid"], "type": "voiceover", "confidence": .9,
                    "label_is_marker": False,
                }]

            result = generate_normalization(
                source, root / "out.json", memory_path=root / "memory.sqlite3",
                classifier=classifier,
            )

            self.assertEqual(result["blocks"][0]["normalized_text"], "Москва: сегодня прошёл форум")
            self.assertEqual(result["blocks"][0]["text_for_tts"], "Москва: сегодня прошёл форум")
            self.assertEqual(result["blocks"][0]["original_label"], "")

    def test_manual_type_change_does_not_learn_or_strip_a_prose_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            normalized = root / "normalized.json"
            prepared = root / "prepared.json"
            raw = root / "raw.txt"
            memory_path = root / "memory.sqlite3"
            source.write_text("Москва: сегодня прошёл форум\n", encoding="utf-8")

            draft = generate_normalization(
                source, normalized, memory_path=memory_path, use_llm=False
            )
            draft["blocks"][0]["type"] = "voiceover"
            draft["blocks"][0]["text_for_tts"] = "Москва: сегодня прошёл форум"
            saved = save_normalization(normalized, draft, expected_revision=1)
            confirm_normalization(
                normalized,
                prepared,
                raw,
                memory_path=memory_path,
                expected_revision=saved["revision"],
            )

            self.assertIn("Москва: сегодня прошёл форум", raw.read_text(encoding="utf-8"))
            with NormalizationMemory(memory_path) as memory:
                self.assertIsNone(memory.resolve_exact("Москва"))

    def test_timecode_prefix_is_not_split_as_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            source.write_text("00:01:02 Иван говорит\n", encoding="utf-8")
            result = generate_normalization(
                source, root / "out.json", memory_path=root / "memory.sqlite3", use_llm=False
            )
            self.assertEqual(result["blocks"][0]["original_label"], "")
            self.assertEqual(result["blocks"][0]["normalized_text"], "00:01:02 Иван говорит")

    def test_low_confidence_llm_result_requires_manual_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            source.write_text("Авторский блок\n", encoding="utf-8")

            def classifier(items, _hint):
                return [{"uid": items[0]["uid"], "type": "voiceover", "confidence": .1}]

            result = generate_normalization(
                source, root / "out.json", memory_path=root / "memory.sqlite3",
                classifier=classifier,
            )
            self.assertEqual(result["blocks"][0]["type"], "unknown")
            self.assertTrue(result["blocks"][0]["warnings"])

    def test_large_scenario_is_classified_in_batches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            source.write_text("\n".join(["А" * 7000, "Б" * 7000, "В" * 7000]), encoding="utf-8")
            calls: list[int] = []

            def classifier(items, _hint):
                calls.append(len(items))
                return [
                    {"uid": item["uid"], "type": "other", "confidence": 0.7}
                    for item in items
                ]

            result = generate_normalization(
                source,
                root / "out.json",
                memory_path=root / "memory.sqlite3",
                classifier=classifier,
            )

            self.assertGreater(len(calls), 1)
            self.assertEqual(len(result["blocks"]), 3)
            self.assertEqual([item["type"] for item in result["blocks"]], ["other"] * 3)

    def test_default_llm_classifier_handles_multiple_compact_batches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            source.write_text(
                "\n".join(f"Абзац {index} " + "текст " * 90 for index in range(80)),
                encoding="utf-8",
            )

            def classifier(items, _hint):
                self.assertTrue(items)
                self.assertNotIn("normalized_text", items[0])
                return [
                    {"uid": item["uid"], "type": "other", "confidence": 0.9}
                    for item in items
                ]

            with patch.object(
                normalizer_module, "_openrouter_classifier", side_effect=classifier
            ) as mocked:
                result = generate_normalization(
                    source, root / "out.json", memory_path=root / "memory.sqlite3"
                )

            self.assertGreater(mocked.call_count, 1)
            self.assertEqual(len(result["blocks"]), 80)
            self.assertTrue(all(item["type"] == "other" for item in result["blocks"]))

    def test_save_revision_and_confirm_projections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            normalized = root / "normalized.json"
            prepared = root / "prepared.json"
            raw = root / "raw.txt"
            db = root / "memory.sqlite3"
            source.write_text("Необычный голос: Текст\nСНХ: Ответ\n", encoding="utf-8")
            draft = generate_normalization(source, normalized, memory_path=db, use_llm=False)
            draft["blocks"][0]["type"] = "voiceover"
            draft["blocks"][0]["text_for_tts"] = "Текст"
            saved = save_normalization(normalized, draft, expected_revision=1)
            self.assertEqual(saved["revision"], 2)
            self.assertEqual(saved["blocks"][0]["source"], "manual")
            with self.assertRaises(RevisionConflictError):
                save_normalization(normalized, saved, expected_revision=1)

            candidates = preview_learning_candidates(
                normalized, memory_path=db, expected_revision=2
            )
            self.assertEqual(len(candidates), 1)
            confirmed = confirm_normalization(
                normalized, prepared, raw, memory_path=db, expected_revision=2,
                learning_decisions=[{
                    "candidate_id": candidates[0]["id"], "action": "remember",
                }],
            )
            self.assertEqual(confirmed["status"], "confirmed")
            self.assertTrue(is_normalization_confirmed(normalized))
            payload = json.loads(prepared.read_text(encoding="utf-8"))
            self.assertEqual(payload["items"][0]["id"], "zk1")
            self.assertEqual(payload["normalization_revision"], confirmed["revision"])
            self.assertIn("ЗК1: Текст", raw.read_text(encoding="utf-8"))
            with NormalizationMemory(db) as memory:
                rules = [r for r in memory.list_rules() if r["source"] == "correction"]
                self.assertEqual(len(rules), 1)
                self.assertEqual(rules[0]["target_type"], "voiceover")
                self.assertEqual(rules[0]["status"], "candidate")

    def test_reset_clears_blocks_and_is_revision_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            normalized = root / "normalized.json"
            source.write_text("ДИКТОР: Исходный текст\nСНХ: Исходный ответ\n", encoding="utf-8")
            draft = generate_normalization(
                source, normalized, memory_path=root / "memory.sqlite3", use_llm=False
            )
            draft["blocks"].reverse()
            for index, block in enumerate(draft["blocks"], start=1):
                block["order"] = index
                block["normalized_text"] = "Ручная правка"
            saved = save_normalization(normalized, draft, expected_revision=1)

            reset = reset_normalization(
                normalized, expected_revision=saved["revision"], source_root=root
            )

            self.assertEqual(reset["revision"], saved["revision"] + 1)
            self.assertEqual(reset["status"], "draft")
            self.assertEqual(reset["hint"], "")
            self.assertEqual(reset["blocks"], [])
            with self.assertRaises(RevisionConflictError):
                reset_normalization(normalized, expected_revision=saved["revision"], source_root=root)

    def test_unknown_blocks_prevent_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            source.write_text("Непонятный абзац\n", encoding="utf-8")
            normalized = root / "normalized.json"
            generate_normalization(source, normalized, memory_path=root / "db.sqlite3", use_llm=False)
            with self.assertRaises(NormalizationValidationError):
                confirm_normalization(
                    normalized, root / "prepared.json", root / "raw.txt",
                    memory_path=root / "db.sqlite3",
                )
            self.assertFalse((root / "prepared.json").exists())

    def test_failed_publication_does_not_update_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            normalized = root / "normalized.json"
            prepared = root / "prepared.json"
            raw = root / "raw.txt"
            db = root / "memory.sqlite3"
            source.write_text("НЕОБЫЧНЫЙ ГОЛОС: Текст\n", encoding="utf-8")
            draft = generate_normalization(source, normalized, memory_path=db, use_llm=False)
            draft["blocks"][0]["type"] = "voiceover"
            draft["blocks"][0]["text_for_tts"] = "Текст"
            saved = save_normalization(normalized, draft, expected_revision=1)
            candidates = preview_learning_candidates(
                normalized, memory_path=db, expected_revision=saved["revision"]
            )
            original_write = normalizer_module._atomic_write_text

            def fail_raw(path, content):
                if Path(path) == raw:
                    raise OSError("simulated raw projection failure")
                return original_write(path, content)

            with patch("stage1.normalizer._atomic_write_text", side_effect=fail_raw):
                with self.assertRaises(OSError):
                    confirm_normalization(
                        normalized, prepared, raw, memory_path=db, expected_revision=2,
                        learning_decisions=[{
                            "candidate_id": candidates[0]["id"], "action": "remember",
                        }],
                    )

            with NormalizationMemory(db) as memory:
                self.assertIsNone(memory.resolve_exact("НЕОБЫЧНЫЙ ГОЛОС"))

    def test_legacy_import_preserves_voiceover_sync_host_and_technical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            source.write_text("source", encoding="utf-8")
            prepared = root / "prepared.json"
            prepared.write_text(json.dumps({
                "source": str(source),
                "items": [{"text_original": "Закадр", "text_for_tts": "Закадр"}],
            }), encoding="utf-8")
            raw = root / "raw.txt"
            raw.write_text(
                "ЗК1: Закадр\n\n"
                "СНХ Иван Иванов, эксперт, 01:02:03 Ответ\n\n"
                "ВЕДУЩИЙ: Реплика\n\nОТБИВКА\n",
                encoding="utf-8",
            )

            result = import_legacy_normalization(prepared, raw, root / "normalized.json", source_path=source)

            self.assertEqual(
                [block["type"] for block in result["blocks"]],
                ["voiceover", "sync", "host", "technical"],
            )

    def test_legacy_import_without_original_source_can_be_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = root / "prepared.json"
            raw = root / "raw.txt"
            normalized = root / "normalized.json"
            prepared.write_text(json.dumps({
                "source": str(root / "missing.docx"),
                "items": [{"text_original": "Текст", "text_for_tts": "Текст"}],
            }), encoding="utf-8")
            raw.write_text("ЗК1: Текст\n", encoding="utf-8")
            imported = import_legacy_normalization(prepared, raw, normalized)

            confirmed = confirm_normalization(
                normalized,
                root / "confirmed-prepared.json",
                root / "confirmed-raw.txt",
                memory_path=root / "memory.sqlite3",
                expected_revision=imported["revision"],
            )

            self.assertEqual(confirmed["status"], "confirmed")

    def test_parallel_saves_with_same_revision_do_not_both_succeed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.txt"
            normalized = root / "normalized.json"
            source.write_text("ДИКТОР: Текст\n", encoding="utf-8")
            draft = generate_normalization(
                source, normalized, memory_path=root / "memory.sqlite3", use_llm=False
            )

            def save_once(suffix):
                payload = json.loads(json.dumps(draft))
                payload["blocks"][0]["normalized_text"] += suffix
                try:
                    save_normalization(normalized, payload, expected_revision=1)
                    return "saved"
                except RevisionConflictError:
                    return "conflict"

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(save_once, [" A", " B"]))

            self.assertEqual(sorted(outcomes), ["conflict", "saved"])

    def test_docx_table_cells_are_included_in_order(self) -> None:
        from docx import Document

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scenario.docx"
            document = Document()
            document.add_paragraph("ДИКТОР: До таблицы")
            table = document.add_table(rows=1, cols=2)
            table.cell(0, 0).text = "СНХ: Ответ"
            table.cell(0, 1).text = "ОТБИВКА"
            document.add_paragraph("ДИКТОР: После таблицы")
            document.save(source)

            result = generate_normalization(
                source, root / "out.json", memory_path=root / "memory.sqlite3", use_llm=False
            )

            self.assertEqual(len(result["blocks"]), 4)
            self.assertEqual(
                [block["type"] for block in result["blocks"]],
                ["voiceover", "sync", "technical", "voiceover"],
            )


if __name__ == "__main__":
    unittest.main()
