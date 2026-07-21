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


if __name__ == "__main__":
    unittest.main()
