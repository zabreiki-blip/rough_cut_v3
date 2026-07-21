from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import unittest

import api


class NormalizationApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        base = Path(self.temp.name).resolve()
        self.projects = base / "projects"
        self.memory = base / "data" / "normalization.sqlite3"
        self.projects.mkdir()
        self.projects_patch = patch.object(api, "PROJECTS_DIR", self.projects)
        self.root_patch = patch.object(api, "ROOT_DIR", base)
        self.memory_patch = patch.object(api, "NORMALIZATION_MEMORY_PATH", self.memory)
        self.projects_patch.start()
        self.root_patch.start()
        self.memory_patch.start()
        api.app.config.update(TESTING=True)
        self.client = api.app.test_client()

    def tearDown(self) -> None:
        self.memory_patch.stop()
        self.root_patch.stop()
        self.projects_patch.stop()
        self.temp.cleanup()

    def _source(self, project: str = "demo") -> Path:
        source = self.projects / project / "input" / "scenario.txt"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            "ДИКТОР: Первый закадровый блок.\nСНХ: Ответ героя.\n",
            encoding="utf-8",
        )
        return source

    def test_generate_save_confirm_and_get(self) -> None:
        source = self._source()
        generated_response = self.client.post(
            "/stage1/normalization/generate",
            json={
                "project": "demo",
                "input_path": str(source),
                "use_llm": False,
                "hint": "ДИКТОР означает закадр",
            },
        )
        self.assertEqual(generated_response.status_code, 200, generated_response.get_json())
        generated = generated_response.get_json()["result"]
        self.assertEqual(generated["status"], "draft")
        self.assertEqual(generated["revision"], 1)
        self.assertEqual([block["type"] for block in generated["blocks"]], ["voiceover", "sync"])

        generated["blocks"][0]["normalized_text"] = "Исправленный текст."
        saved_response = self.client.put(
            "/stage1/normalization",
            json={"project": "demo", "revision": 1, "blocks": generated["blocks"]},
        )
        self.assertEqual(saved_response.status_code, 200, saved_response.get_json())
        saved = saved_response.get_json()["result"]
        self.assertEqual(saved["revision"], 2)
        self.assertEqual(saved["blocks"][0]["source"], "manual")

        confirmed_response = self.client.post(
            "/stage1/normalization/confirm",
            json={"project": "demo", "revision": 2},
        )
        self.assertEqual(confirmed_response.status_code, 200, confirmed_response.get_json())
        confirmed = confirmed_response.get_json()["result"]
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertTrue((self.projects / "demo" / "work" / "scenario_prepared.json").exists())
        alternate = self.projects / "demo" / "work" / "alternate.json"
        alternate.write_text('{"items": []}', encoding="utf-8")
        bypass = self.client.post(
            "/stage1/tts",
            json={"project": "demo", "prepared_path": str(alternate), "dry_run": True},
        )
        self.assertEqual(bypass.status_code, 400)

        get_response = self.client.get("/stage1/normalization?project=demo")
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(get_response.get_json()["result"]["revision"], confirmed["revision"])

        timeline_response = self.client.post("/stage2/build", json={"project": "demo"})
        self.assertEqual(timeline_response.status_code, 200, timeline_response.get_json())
        timeline_result = timeline_response.get_json()["result"]
        self.assertEqual(timeline_result["zk"], 1)
        self.assertEqual(timeline_result["snh"], 1)
        self.assertEqual(self.client.get("/stage2/export/xml?project=demo").status_code, 200)

        confirmed["blocks"][0]["normalized_text"] = "Ещё одна редакция."
        saved_again = self.client.put(
            "/stage1/normalization",
            json={
                "project": "demo",
                "revision": confirmed["revision"],
                "blocks": confirmed["blocks"],
            },
        ).get_json()["result"]
        reconfirmed = self.client.post(
            "/stage1/normalization/confirm",
            json={"project": "demo", "revision": saved_again["revision"]},
        )
        self.assertEqual(reconfirmed.status_code, 200, reconfirmed.get_json())
        self.assertEqual(
            self.client.get("/stage2/export/xml?project=demo").status_code,
            409,
        )

    def test_revision_conflict_is_409(self) -> None:
        source = self._source()
        response = self.client.post(
            "/stage1/normalization/generate",
            json={"project": "demo", "input_path": str(source), "use_llm": False},
        )
        blocks = response.get_json()["result"]["blocks"]
        conflict = self.client.put(
            "/stage1/normalization",
            json={"project": "demo", "revision": 99, "blocks": blocks},
        )
        self.assertEqual(conflict.status_code, 409)

    def test_reset_restores_source_without_touching_outputs_or_wav(self) -> None:
        source = self._source()
        generated = self.client.post(
            "/stage1/normalization/generate",
            json={"project": "demo", "input_path": str(source), "use_llm": False},
        ).get_json()["result"]
        confirmed_response = self.client.post(
            "/stage1/normalization/confirm",
            json={"project": "demo", "revision": generated["revision"]},
        )
        confirmed = confirmed_response.get_json()["result"]
        project = self.projects / "demo"
        prepared = project / "work" / "scenario_prepared.json"
        raw = project / "work" / "scenario_raw.txt"
        wav = project / "speaker" / "zk1.wav"
        wav.parent.mkdir(parents=True, exist_ok=True)
        wav.write_bytes(b"existing wav sentinel")
        prepared_before = prepared.read_bytes()
        raw_before = raw.read_bytes()

        response = self.client.post(
            "/stage1/normalization/reset",
            json={"project": "demo", "revision": confirmed["revision"]},
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        reset = response.get_json()["result"]
        self.assertEqual(reset["status"], "draft")
        self.assertEqual(reset["revision"], confirmed["revision"] + 1)
        self.assertEqual([block["type"] for block in reset["blocks"]], ["unknown", "unknown"])
        self.assertEqual(prepared.read_bytes(), prepared_before)
        self.assertEqual(raw.read_bytes(), raw_before)
        self.assertEqual(wav.read_bytes(), b"existing wav sentinel")
        self.assertEqual(
            self.client.post("/stage1/tts", json={"project": "demo", "dry_run": True}).status_code,
            409,
        )
        stale = self.client.post(
            "/stage1/normalization/reset",
            json={"project": "demo", "revision": confirmed["revision"]},
        )
        self.assertEqual(stale.status_code, 409)

    def test_get_imports_legacy_outputs_as_unconfirmed_draft(self) -> None:
        source = self._source()
        work = source.parent.parent / "work"
        work.mkdir(parents=True)
        (work / "scenario_prepared.json").write_text(json.dumps({
            "source": str(source),
            "items": [{
                "id": "zk1", "text_original": "Старый текст",
                "text_for_tts": "Старый текст", "pause_ms": 300,
            }],
        }, ensure_ascii=False), encoding="utf-8")
        (work / "scenario_raw.txt").write_text("ЗК1: Старый текст\n", encoding="utf-8")

        response = self.client.get("/stage1/normalization?project=demo")
        self.assertEqual(response.status_code, 200, response.get_json())
        result = response.get_json()["result"]
        self.assertEqual(result["status"], "draft")
        self.assertEqual(result["blocks"][0]["type"], "voiceover")
        self.assertIn("Imported legacy", result["warnings"][0])

    def test_pipeline_is_gated_but_phrase_preview_is_not(self) -> None:
        gated = self.client.post("/stage1/tts", json={"project": "demo", "dry_run": True})
        self.assertEqual(gated.status_code, 409)
        self.assertEqual(gated.get_json()["error"], "normalization_confirmation_required")

        with patch.object(api, "synthesize_phrase", return_value={"file": "", "dry_run": True}):
            preview = self.client.post(
                "/stage1/tts-phrase",
                json={"project": "demo", "text": "Проверка", "dry_run": True},
            )
        self.assertEqual(preview.status_code, 200, preview.get_json())

    def test_confirmed_pipeline_is_gated_after_source_changes(self) -> None:
        source = self._source()
        generated = self.client.post(
            "/stage1/normalization/generate",
            json={"project": "demo", "input_path": str(source), "use_llm": False},
        ).get_json()["result"]
        confirmed = self.client.post(
            "/stage1/normalization/confirm",
            json={"project": "demo", "revision": generated["revision"]},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.get_json())

        source.write_text("ДИКТОР: Изменённый сценарий\n", encoding="utf-8")
        gated = self.client.post("/stage1/tts", json={"project": "demo", "dry_run": True})

        self.assertEqual(gated.status_code, 409)
        self.assertEqual(gated.get_json()["reason"], "source_changed")

    def test_tts_rejects_prepared_file_overwritten_after_confirmation(self) -> None:
        source = self._source()
        generated = self.client.post(
            "/stage1/normalization/generate",
            json={"project": "demo", "input_path": str(source), "use_llm": False},
        ).get_json()["result"]
        confirmed = self.client.post(
            "/stage1/normalization/confirm",
            json={"project": "demo", "revision": generated["revision"]},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.get_json())
        prepared = self.projects / "demo" / "work" / "scenario_prepared.json"
        prepared.write_text('{"items": []}', encoding="utf-8")

        gated = self.client.post("/stage1/tts", json={"project": "demo", "dry_run": True})

        self.assertEqual(gated.status_code, 409)
        self.assertEqual(gated.get_json()["error"], "normalization_projection_stale")

    def test_memory_crud(self) -> None:
        created_response = self.client.post(
            "/stage1/normalization/memory",
            json={"alias": "Голос автора", "block_type": "voiceover"},
        )
        self.assertEqual(created_response.status_code, 201, created_response.get_json())
        rule_id = created_response.get_json()["result"]["id"]

        listed = self.client.get("/stage1/normalization/memory?block_type=voiceover")
        self.assertEqual(len(listed.get_json()["result"]["rules"]), 1)

        updated = self.client.patch(
            f"/stage1/normalization/memory/{rule_id}", json={"status": "disabled"}
        )
        self.assertEqual(updated.status_code, 200, updated.get_json())
        self.assertEqual(updated.get_json()["result"]["status"], "disabled")

        deleted = self.client.delete(f"/stage1/normalization/memory/{rule_id}")
        self.assertEqual(deleted.status_code, 200, deleted.get_json())
        self.assertTrue(deleted.get_json()["result"])


if __name__ == "__main__":
    unittest.main()
