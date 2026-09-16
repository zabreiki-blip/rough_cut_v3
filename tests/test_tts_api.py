from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import wave
from unittest.mock import patch

import api as api_module
from atomic_io import atomic_write_json
from project_service import ensure_project
from stage4.revision_assets import revision_speaker_alias_dir
from stage4.versioning import ensure_baseline, inspect_revision_state


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
            patch.object(
                api_module, "_confirmed_prepared_path", return_value=Path("prepared.json")
            ),
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

    def test_non_durable_progress_update_skips_disk_checkpoint(self) -> None:
        created = api_module._create_tts_job("demo")
        with patch.object(api_module, "_persist_tts_job") as persist:
            updated = api_module._update_tts_job(
                created["id"],
                persist=False,
                done=3,
            )

        self.assertIsNotNone(updated)
        self.assertEqual(updated["done"], 3)
        self.assertEqual(api_module._get_tts_job(created["id"])["done"], 3)
        persist.assert_not_called()

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

        status_path = Path(self.tmp.name) / "projects" / "demo" / "work" / "tts_job_status.json"
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
            "provider": "openrouter",
            "tts_model": "google/gemini-3.1-flash-tts-preview",
            "tts_style": "Серьёзное документальное кино",
            "voice_name": "Aoede",
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
        self.assertEqual(job["provider"], "openrouter")
        self.assertEqual(job["tts_model"], "google/gemini-3.1-flash-tts-preview")
        self.assertEqual(job["tts_style"], "Серьёзное документальное кино")
        self.assertEqual(job["voice"], "Aoede")
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

        status_path = Path(self.tmp.name) / "projects" / "demo" / "work" / "tts_job_status.json"
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


def _write_wav(path: Path, marker: int) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(marker.to_bytes(2, "little", signed=True) * 80)
    return path.read_bytes()


class TtsManifestSyncTests(unittest.TestCase):
    """A TTS batch that resynthesizes one block and is then cancelled or
    fails partway through must not leave stage4.versioning.inspect_revision_state
    treating that legitimate, in-progress work as project corruption.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.projects_patch = patch.object(
            api_module, "PROJECTS_DIR", Path(self.tmp.name) / "projects"
        )
        self.projects_patch.start()
        self.addCleanup(self.projects_patch.stop)
        with api_module._TTS_JOBS_LOCK:
            api_module._TTS_JOBS.clear()
            api_module._TTS_ACTIVE_PROJECTS.clear()
            api_module._TTS_CANCEL_EVENTS.clear()

        self.paths = ensure_project("demo", api_module.PROJECTS_DIR)
        source = self.paths["input"] / "scenario.docx"
        source.write_bytes(b"revision one")
        atomic_write_json(
            self.paths["normalized"],
            {
                "source": str(source),
                "source_hash": hashlib.sha256(b"revision one").hexdigest(),
                "status": "confirmed",
                "blocks": [
                    {
                        "uid": "voice-stable",
                        "normalized_text": "Старый текст",
                        "original_text": "Старый текст",
                        "type": "voiceover",
                        "speaker": "Закадровый голос",
                        "original_label": "ЗК",
                        "text_for_tts": "Старый текст",
                    }
                ],
            },
        )
        atomic_write_json(
            self.paths["prepared"],
            {
                "source": str(source),
                "scenario_revision_id": "",
                "items": [
                    {
                        "id": "zk1",
                        "block_uid": "voice-stable",
                        "text_original": "Старый текст",
                        "text_for_tts": "Старый текст",
                    }
                ],
            },
        )
        _write_wav(self.paths["speaker"] / "zk1.wav", 101)
        atomic_write_json(
            self.paths["speaker"] / "zk1.wav.meta", {"fingerprint": "legacy-fingerprint"}
        )
        ensure_baseline("demo", self.paths)
        # ensure_baseline stamped prepared.json's scenario_revision_id; keep
        # the on-disk prepared.json (as a real TTS job would read it) in
        # sync with the active revision it belongs to.
        prepared = json.loads(self.paths["prepared"].read_text(encoding="utf-8"))
        prepared["scenario_revision_id"] = "rev_0001"
        atomic_write_json(self.paths["prepared"], prepared)
        self.assertEqual(inspect_revision_state(self.paths)["status"], "ok")

    def test_job_failure_after_partial_resynthesis_does_not_block_revision_switching(
        self,
    ) -> None:
        new_audio = b"\x00" * 8

        def fake_synthesize_tts(
            *,
            prepared_path,
            speaker_dir,
            provider,
            voice_name,
            gender,
            dry_run,
            progress_callback,
            cancel_check,
            speed=1.0,
            usage_callback=None,
            tts_model=None,
            tts_style="",
        ):
            # Resynthesize zk1 exactly like stage1.gemini_tts does: write the
            # content-addressed asset, materialize the mutable alias and its
            # sidecar, then report success for that one item...
            asset = speaker_dir / "assets" / "voice-stable" / "new-fingerprint.wav"
            asset.parent.mkdir(parents=True, exist_ok=True)
            asset.write_bytes(new_audio)
            (speaker_dir / "zk1.wav").write_bytes(new_audio)
            atomic_write_json(speaker_dir / "zk1.wav.meta", {"fingerprint": "new-fingerprint"})
            progress_callback(
                {
                    "event": "item_generated",
                    "id": "zk1",
                    "total": 2,
                    "completed": 1,
                    "generated": 1,
                    "skipped": 0,
                }
            )
            # ...then blow up before the batch (and save_tts_manifest) can
            # finish, e.g. a network failure on the next item.
            raise RuntimeError("simulated failure on next item")

        self.assertTrue(api_module._reserve_tts_project("demo"))
        job = api_module._create_tts_job("demo")
        with patch.object(api_module, "synthesize_tts", side_effect=fake_synthesize_tts):
            api_module._run_tts_job(
                job_id=job["id"],
                project="demo",
                prepared_path=self.paths["prepared"],
                speaker_dir=revision_speaker_alias_dir(self.paths, "rev_0001"),
                provider=None,
                voice_name=None,
                gender=None,
                dry_run=False,
            )

        state = api_module._get_tts_job(job["id"])
        self.assertEqual(state["status"], "failed")

        manifest = json.loads(self.paths["active_voiceover_manifest"].read_text(encoding="utf-8"))
        entry = next(item for item in manifest["items"] if item["id"] == "zk1")
        self.assertEqual(entry["fingerprint"], "new-fingerprint")

        result = inspect_revision_state(self.paths)
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["blocking"])


if __name__ == "__main__":
    unittest.main()
