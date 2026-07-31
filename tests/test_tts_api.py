from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import api as api_module


class _DormantThread:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    def start(self):
        return None


class TtsCoordinationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.projects_patch = patch.object(
            api_module, "PROJECTS_DIR", Path(self.tmp.name) / "projects"
        )
        self.projects_patch.start()
        self.addCleanup(self.projects_patch.stop)
        api_module.app.config.update(TESTING=True)
        api_module.app.config.pop("API_TOKEN", None)
        with api_module._TTS_JOBS_LOCK:
            api_module._TTS_JOBS.clear()
            api_module._TTS_ACTIVE_PROJECTS.clear()
            api_module._TTS_CANCEL_EVENTS.clear()
        self.client = api_module.app.test_client()

    def _job_payload(self) -> dict:
        return {"project": "demo", "dry_run": True}

    def test_queued_job_reserves_project_atomically(self) -> None:
        with (
            patch.object(api_module, "_normalization_required", return_value=None),
            patch.object(api_module, "_prepared_projection_required", return_value=None),
            patch.object(api_module, "_confirmed_prepared_path", return_value=Path("prepared.json")),
            patch.object(api_module.threading, "Thread", _DormantThread),
        ):
            first = self.client.post("/stage1/tts-jobs/start", json=self._job_payload())
            second = self.client.post("/stage1/tts-jobs/start", json=self._job_payload())

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(len(api_module._TTS_JOBS), 1)

    def test_sync_and_phrase_tts_reject_reserved_project(self) -> None:
        self.assertTrue(api_module._reserve_tts_project("demo"))
        with (
            patch.object(api_module, "_normalization_required", return_value=None),
            patch.object(api_module, "_prepared_projection_required", return_value=None),
        ):
            sync = self.client.post("/stage1/tts", json={"project": "demo", "dry_run": True})
        phrase = self.client.post(
            "/stage1/tts-phrase",
            json={"project": "demo", "text": "test", "dry_run": True},
        )
        self.assertEqual(sync.status_code, 409)
        self.assertEqual(phrase.status_code, 409)

    def test_trim_keeps_active_jobs_and_expires_terminal_jobs(self) -> None:
        old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        with api_module._TTS_JOBS_LOCK:
            api_module._TTS_JOBS.update(
                {
                    "queued": {"status": "queued", "created_at": old},
                    "running": {"status": "running", "created_at": old},
                    "done": {"status": "completed", "created_at": old, "finished_at": old},
                }
            )
            api_module._trim_tts_jobs_locked()
        self.assertIn("queued", api_module._TTS_JOBS)
        self.assertIn("running", api_module._TTS_JOBS)
        self.assertNotIn("done", api_module._TTS_JOBS)

    def test_latest_status_survives_restart_as_interrupted_checkpoint(self) -> None:
        created = api_module._create_tts_job(
            "demo",
            prepared_path=Path("/projects/demo/work/scenario_prepared.json"),
            speaker_dir=Path("/projects/demo/speaker"),
            provider="openrouter",
            voice="alloy",
            gender="female",
            dry_run=True,
            speed=1.15,
        )
        updated = api_module._update_tts_job(
            created["id"],
            status="running",
            event="item_generated",
            done=2,
            total=5,
            generated=2,
            current_id="zk3",
        )
        self.assertIsNotNone(updated)

        status_path = (
            Path(self.tmp.name) / "projects" / "demo" / "work" / "tts_job_status.json"
        )
        self.assertTrue(status_path.is_file())

        with api_module._TTS_JOBS_LOCK:
            api_module._TTS_JOBS.clear()
            api_module._TTS_ACTIVE_PROJECTS.clear()
            api_module._TTS_CANCEL_EVENTS.clear()

        response = self.client.get("/stage1/tts-jobs/status?project=demo")
        self.assertEqual(response.status_code, 200, response.get_json())
        restored = response.get_json()["result"]
        self.assertEqual(restored["id"], created["id"])
        self.assertEqual(restored["status"], "interrupted")
        self.assertEqual(restored["event"], "interrupted")
        self.assertEqual(restored["done"], 2)
        self.assertEqual(restored["total"], 5)
        self.assertEqual(restored["current_id"], "")
        self.assertEqual(
            restored["prepared_path"],
            "/projects/demo/work/scenario_prepared.json",
        )
        self.assertEqual(restored["speaker_dir"], "/projects/demo/speaker")
        self.assertEqual(restored["provider"], "openrouter")
        self.assertEqual(restored["voice"], "alloy")
        self.assertEqual(restored["gender"], "female")
        self.assertTrue(restored["dry_run"])
        self.assertEqual(restored["speed"], 1.15)

    def test_start_job_records_exact_resume_controls(self) -> None:
        prepared = Path(self.tmp.name) / "projects" / "demo" / "work" / "scenario_prepared.json"
        speaker = Path(self.tmp.name) / "projects" / "demo" / "speaker-custom"
        payload = {
            "project": "demo",
            "prepared_path": str(prepared),
            "speaker_dir": str(speaker),
            "provider": "gemini",
            "voice_name": "Kore",
            "gender": "female",
            "dry_run": True,
            "speed": 1.25,
        }
        with (
            patch.object(api_module, "_normalization_required", return_value=None),
            patch.object(api_module, "_prepared_projection_required", return_value=None),
            patch.object(api_module, "_confirmed_prepared_path", return_value=prepared),
            patch.object(api_module, "_abs_path", return_value=speaker),
            patch.object(api_module.threading, "Thread", _DormantThread),
        ):
            response = self.client.post("/stage1/tts-jobs/start", json=payload)

        self.assertEqual(response.status_code, 200, response.get_json())
        job_id = response.get_json()["result"]["job_id"]
        job = api_module._get_tts_job(job_id)
        self.assertIsNotNone(job)
        self.assertEqual(job["prepared_path"], str(prepared))
        self.assertEqual(job["speaker_dir"], str(speaker))
        self.assertEqual(job["provider"], "gemini")
        self.assertEqual(job["voice"], "Kore")
        self.assertEqual(job["gender"], "female")
        self.assertTrue(job["dry_run"])
        self.assertEqual(job["speed"], 1.25)

    def test_cancel_endpoint_sets_durable_cancelling_checkpoint(self) -> None:
        created = api_module._create_tts_job("demo")

        response = self.client.post(f"/stage1/tts-jobs/{created['id']}/cancel")

        self.assertEqual(response.status_code, 202)
        result = response.get_json()["result"]
        self.assertEqual(result["status"], "cancelling")
        self.assertEqual(result["event"], "cancelling")
        with api_module._TTS_JOBS_LOCK:
            self.assertTrue(api_module._TTS_CANCEL_EVENTS[created["id"]].is_set())

        status_path = (
            Path(self.tmp.name) / "projects" / "demo" / "work" / "tts_job_status.json"
        )
        persisted = __import__("json").loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["id"], created["id"])
        self.assertEqual(persisted["status"], "cancelling")

    def test_cancelled_queued_worker_releases_project_without_synthesis(self) -> None:
        self.assertTrue(api_module._reserve_tts_project("demo"))
        created = api_module._create_tts_job("demo")
        with api_module._TTS_JOBS_LOCK:
            api_module._TTS_CANCEL_EVENTS[created["id"]].set()

        with patch.object(api_module, "synthesize_tts") as synthesize:
            api_module._run_tts_job(
                job_id=created["id"],
                project="demo",
                prepared_path=Path("unused-prepared.json"),
                speaker_dir=Path("unused-speaker"),
                provider=None,
                voice_name=None,
                gender=None,
                dry_run=True,
            )

        synthesize.assert_not_called()
        state = api_module._get_tts_job(created["id"])
        self.assertIsNotNone(state)
        self.assertEqual(state["status"], "cancelled")
        with api_module._TTS_JOBS_LOCK:
            self.assertNotIn("demo", api_module._TTS_ACTIVE_PROJECTS)
            self.assertNotIn(created["id"], api_module._TTS_CANCEL_EVENTS)


if __name__ == "__main__":
    unittest.main()
