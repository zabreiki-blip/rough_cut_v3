from __future__ import annotations

import functools
import io
import json
from pathlib import Path
from typing import Any
import wave

import pytest

import api
import api_voiceover_routes
from api_voiceover_jobs import reserve_voiceover_project
from atomic_io import atomic_write_json
from voiceover import publish as publish_module
from voiceover.storage import load_recorded_voiceover, register_recording_file

SENTENCES = [
    "Город просыпается рано утром",
    "Рабочие выходят на площадь",
    "Строители поднимают новый мост",
    "Вечером огни зажигаются над рекой",
]
BASE = "/stage1/recorded-voiceover"


def make_wav(path: Path, seconds: float = 60.0, rate: int = 8000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(b"\x01\x00" * int(seconds * rate))
    return path


def asr_payload(*sentences: str, start: float = 4.0) -> dict[str, Any]:
    words: list[dict[str, Any]] = []
    cursor = start
    for sentence in sentences:
        for token in sentence.split():
            words.append({"word": token, "start": cursor, "end": cursor + 0.4})
            cursor += 0.45
        cursor += 3.0
    return {"words": words, "language": "ru", "model": "fake", "backend": "fake"}


def scenario(*texts: str, revision: str = "rev_1") -> dict:
    return {
        "status": "confirmed",
        "scenario_revision_id": revision,
        "blocks": [
            {
                "uid": f"b{number}",
                "order": number,
                "type": "voiceover",
                "original_text": text,
                "normalized_text": text,
                "text_for_tts": text,
                "absorbed_by_uid": "",
            }
            for number, text in enumerate(texts, start=1)
        ],
    }


class Env:
    def __init__(self, paths: dict[str, Path], client: Any) -> None:
        self.paths = paths
        self.client = client
        self.doc: dict | None = scenario(*SENTENCES)
        self.words = asr_payload(*SENTENCES[:3])
        self.calls = 0

    def transcribe(self, audio_path: Path, **_options: Any) -> dict[str, Any]:
        self.calls += 1
        return self.words

    def call(self, method: str, path: str, **body: Any):
        body.setdefault("project", "demo")
        if method == "GET":
            return self.client.get(BASE + path, query_string=body)
        return self.client.open(BASE + path, method=method, json=body)

    def state(self) -> dict:
        return self.call("GET", "").get_json()["result"]

    def edit(self, method: str, path: str, **body: Any):
        """Mutation carrying the current revision and generation (as the UI does)."""
        body.setdefault("expected_revision_id", "rev_1")
        body.setdefault("expected_generation", self.state()["generation"])
        return self.call(method, path, **body)

    def register(self, name: str = "take.wav") -> Any:
        source = make_wav(self.paths["inbox_voiceover"] / name)
        return register_recording_file(self.paths, source)

    def run_job(self):
        response = self.call("POST", "/jobs/start", expected_revision_id="rev_1")
        assert response.status_code == 202, response.get_json()
        return self.state()["job"]

    def add_tts(self, *uids: str) -> None:
        items = []
        for uid in uids:
            asset = self.paths["voiceover_assets"] / uid / "tts.wav"
            make_wav(asset, seconds=1.0)
            items.append(
                {
                    "id": f"zk{uid[1:]}",
                    "block_uid": uid,
                    "fingerprint": f"fp-{uid}",
                    "asset": asset.relative_to(self.paths["root"]).as_posix(),
                }
            )
        atomic_write_json(
            self.paths["active_voiceover_manifest"],
            {"schema_version": 1, "scenario_revision_id": "rev_1", "items": items},
        )


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(api, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(api, "PROJECTS_DIR", projects)
    monkeypatch.setitem(api.app.config, "TESTING", True)
    monkeypatch.delitem(api.app.config, "API_TOKEN", raising=False)
    monkeypatch.delenv("SYNC_WHISPER_BACKEND", raising=False)
    monkeypatch.delenv("SYNC_WHISPER_MODEL", raising=False)
    runtime = api._VOICEOVER_RUNTIME
    for registry in (runtime.jobs, runtime.cancel_events, runtime.active_projects):
        registry.clear()  # process-wide state must not leak between tests
    result = Env(api._ensure_project("demo"), api.app.test_client())
    monkeypatch.setattr(
        api,
        "_confirmed_normalization",
        lambda project: (result.doc, "confirmed") if result.doc else (None, "draft"),
    )
    monkeypatch.setattr(api, "transcribe_audio", result.transcribe)
    monkeypatch.setattr(
        api_voiceover_routes, "_start_worker", lambda target, kwargs: target(**kwargs)
    )
    return result


def ranges_by_uid(env: Env) -> dict[str, dict]:
    return {item["block_uid"]: item for item in load_recorded_voiceover(env.paths)["ranges"]}


def test_unconfirmed_scenario_allows_reading_but_not_edits(env: Env) -> None:
    env.doc = None
    state = env.state()
    assert state["scenario_confirmed"] is False and state["blocks"] == []
    for method, path in (
        ("POST", "/jobs/start"),
        ("PATCH", "/ranges/b1"),
        ("PATCH", "/blocks/b1/voice-source"),
        ("POST", "/apply"),
    ):
        response = env.call(method, path, expected_revision_id="rev_1", expected_generation=0)
        assert response.status_code == 409
        assert response.get_json()["error"] == "normalization_confirmation_required"


def test_foreign_paths_and_foreign_jobs_are_not_reachable(env: Env, tmp_path: Path) -> None:
    api._ensure_project("other")
    env.register()
    for value in ("../../etc/passwd", "/etc/passwd", "ЗАГРУЗИТЬ/Закадр/../../../x.wav"):
        response = env.client.post(
            BASE + "/source", data={"project": "demo", "inbox_file_id": value}
        )
        assert response.status_code == 404
    assert env.call("GET", "/audio", recording_id="../x").status_code == 404
    assert env.call("GET", "/audio", recording_id="../x").status_code == 404
    job = env.run_job()
    foreign = env.client.get(f"{BASE}/jobs/{job['id']}", query_string={"project": "other"})
    assert foreign.status_code == 404
    cancel = env.client.post(f"{BASE}/jobs/{job['id']}/cancel", json={"project": "other"})
    assert cancel.status_code == 404


def test_oversized_upload_is_rejected_without_leftovers(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(api.app.config, "SYNC_AUDIO_MAX_BYTES", 1000)
    payload = make_wav(env.paths["input"] / "big.wav", seconds=2.0).read_bytes()
    response = env.client.post(
        BASE + "/source",
        data={"project": "demo", "audio": (io.BytesIO(payload), "big.wav")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 413
    assert not [p for p in env.paths["inbox_voiceover"].iterdir()]
    wrong = env.client.post(
        BASE + "/source",
        data={"project": "demo", "audio": (io.BytesIO(b"x"), "voice.ogg")},
        content_type="multipart/form-data",
    )
    assert wrong.status_code == 415


def test_upload_registers_recording_and_audio_supports_ranges(env: Env) -> None:
    payload = make_wav(env.paths["input"] / "src.wav", seconds=2.0).read_bytes()
    response = env.client.post(
        BASE + "/source",
        data={"project": "demo", "audio": (io.BytesIO(payload), "Закадр 1.wav")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200, response.get_json()
    recording = response.get_json()["result"]["recording"]
    assert (env.paths["inbox_voiceover"] / "Закадр 1.wav").is_file()
    assert [item["id"] for item in env.state()["recordings"]] == [recording["id"]]
    audio = env.client.get(
        BASE + "/audio",
        query_string={"project": "demo", "recording_id": recording["id"]},
        headers={"Range": "bytes=0-99"},
    )
    assert audio.status_code == 206 and len(audio.data) == 100


def test_job_proposals_then_stale_patch_is_409(env: Env) -> None:
    env.register()
    job = env.run_job()
    assert job["status"] == "completed"
    state = env.state()
    assert [b["range"]["status"] for b in state["blocks"][:3]] == ["needs_review"] * 3
    assert state["blocks"][3]["range"]["status"] == "missing"
    stale = state["generation"]
    assert env.edit("PATCH", "/ranges/b1", status="confirmed").status_code == 200
    # the same generation again: the markup moved on -> 409
    response = env.call(
        "PATCH",
        "/ranges/b2",
        status="confirmed",
        expected_revision_id="rev_1",
        expected_generation=stale,
    )
    assert response.status_code == 409
    wrong_revision = env.call(
        "PATCH",
        "/ranges/b2",
        status="confirmed",
        expected_revision_id="rev_0",
        expected_generation=env.state()["generation"],
    )
    assert wrong_revision.status_code == 409
    missing = env.call("PATCH", "/ranges/b2", status="confirmed", expected_revision_id="rev_1")
    assert missing.status_code == 400


def test_manual_range_and_take_selection_are_manual_decisions(env: Env) -> None:
    env.words = asr_payload(SENTENCES[0], SENTENCES[0], SENTENCES[1], SENTENCES[2])
    env.register()
    env.run_job()
    first = ranges_by_uid(env)["b1"]
    assert len(first["takes"]) == 2 and first["selected_take"] is None
    before = env.state()["generation"]

    response = env.edit("PATCH", "/ranges/b1", selected_take=0)
    assert response.status_code == 200
    result = response.get_json()["result"]
    chosen = result["range"]
    assert chosen["selected_take"] == 0 and chosen["origin"] == "manual"
    assert chosen["source_in_sec"] == first["takes"][0]["source_in_sec"]
    assert chosen["status"] == "needs_review"
    assert result["generation"] > before == env.state()["generation"] - 1

    assert env.edit("PATCH", "/ranges/b1", selected_take=5).status_code == 400
    both = env.edit("PATCH", "/ranges/b1", selected_take=1, source_in_sec=1.0, source_out_sec=2.0)
    assert both.status_code == 400

    edited = env.edit(
        "PATCH", "/ranges/b2", source_in_sec=20.0, source_out_sec=24.0, status="confirmed"
    ).get_json()["result"]["range"]
    assert edited["manual_range"] is True and edited["origin"] == "manual"
    assert edited["status"] == "confirmed" and edited["selected_take"] is None
    # overlap with a confirmed range of another block is refused
    assert env.edit("PATCH", "/ranges/b1", status="confirmed").status_code == 200
    clash = env.edit(
        "PATCH", "/ranges/b3", source_in_sec=21.0, source_out_sec=23.0, status="confirmed"
    )
    assert clash.status_code == 409
    outside = env.edit("PATCH", "/ranges/b3", source_in_sec=50.0, source_out_sec=70.0)
    assert outside.status_code == 400


def test_partial_publication_mixes_recorded_tts_and_none(env: Env) -> None:
    env.register()
    env.add_tts("b2", "b3")
    env.run_job()
    assert env.edit("PATCH", "/ranges/b1", status="confirmed").status_code == 200
    assert env.edit("PATCH", "/ranges/b2", status="confirmed").status_code == 200
    # manual override "use TTS" wins over the confirmed found range
    assert env.edit("PATCH", "/blocks/b2/voice-source", voice_source="tts").status_code == 200
    assert not env.paths["recorded_voiceover_published"].exists()  # draft only until apply

    response = env.edit("POST", "/apply")
    assert response.status_code == 200, response.get_json()
    result = response.get_json()["result"]
    by_uid = {item["block_uid"]: item for item in result["blocks"]}
    assert by_uid["b1"]["voice_source"] == "recorded"
    assert by_uid["b1"]["source_out_sec"] > by_uid["b1"]["source_in_sec"]
    assert (by_uid["b2"]["voice_source"], by_uid["b2"]["reason"]) == ("tts", "manual_tts")
    assert (by_uid["b3"]["voice_source"], by_uid["b3"]["reason"]) == ("tts", "not_confirmed")
    assert by_uid["b4"]["voice_source"] == "none"  # nothing found and no TTS WAV
    assert result["summary"] == {"recorded": 1, "tts": 2, "none": 1}

    published = json.loads(env.paths["recorded_voiceover_published"].read_text(encoding="utf-8"))
    assert published["published_sha256"] == result["published_sha256"]
    assert [item["id"] for item in published["recordings"]] == [by_uid["b1"]["recording_id"]]
    assert not list(env.paths["speaker"].rglob("zk*.wav"))  # no shared file in zkN aliases
    snapshot = env.paths["scenario_versions"] / "rev_1.recorded_voiceover.json"
    assert json.loads(snapshot.read_text(encoding="utf-8"))["blocks"] == published["blocks"]
    assert env.state()["published"]["is_current"] is True
    # a later decision change makes the published map stale for the timeline/build check
    assert env.edit("PATCH", "/blocks/b1/voice-source", voice_source="tts").status_code == 200
    assert env.state()["published"]["is_current"] is False


def test_partial_take_is_not_published_before_confirmation(env: Env) -> None:
    env.words = asr_payload("Город просыпается просыпается рано утром", *SENTENCES[1:3])
    env.register()
    env.run_job()
    block = ranges_by_uid(env)["b1"]
    assert "частичный" in block["note"] and block["status"] == "needs_review"
    first = env.edit("POST", "/apply").get_json()["result"]["blocks"][0]
    assert first["voice_source"] == "none" and first["reason"] == "not_confirmed"

    env.edit("PATCH", "/ranges/b1", status="confirmed")
    confirmed = env.edit("POST", "/apply").get_json()["result"]["blocks"][0]
    assert confirmed["voice_source"] == "recorded" and confirmed["partial"] is True


def test_changed_text_loses_binding_and_unchanged_uid_keeps_it(env: Env) -> None:
    env.register()
    env.add_tts("b2")
    env.run_job()
    for uid in ("b1", "b2"):
        assert env.edit("PATCH", f"/ranges/{uid}", status="confirmed").status_code == 200
    kept = ranges_by_uid(env)["b1"]
    # block b2 is rewritten after the recording; b1 is unchanged
    env.doc = scenario(SENTENCES[0], "Совсем другой текст про площадь", SENTENCES[2], SENTENCES[3])
    state = env.state()
    assert state["blocks"][1]["text_changed"] is True
    blocks = {i["block_uid"]: i for i in env.edit("POST", "/apply").get_json()["result"]["blocks"]}
    assert blocks["b1"]["voice_source"] == "recorded"
    assert (blocks["b2"]["voice_source"], blocks["b2"]["reason"]) == ("tts", "text_changed")
    # recognition again: the confirmed range of the unchanged block survives as is
    env.run_job()
    after = ranges_by_uid(env)
    assert after["b1"] == kept
    assert after["b2"]["status"] != "confirmed"


def test_tts_job_and_recognition_exclude_each_other(env: Env) -> None:
    env.register()
    assert api._reserve_tts_project("demo")
    try:
        assert env.call("POST", "/jobs/start", expected_revision_id="rev_1").status_code == 409
        assert env.edit("POST", "/apply").status_code == 409
    finally:
        api._release_tts_project("demo")
    assert reserve_voiceover_project(api, "demo")
    try:
        assert api._reserve_tts_project("demo") is False
        assert env.edit("POST", "/apply").status_code == 409
        assert env.edit("PATCH", "/ranges/b1", status="confirmed").status_code == 409
    finally:
        api_voiceover_routes.release_voiceover_project(api, "demo")
    assert api._reserve_tts_project("demo")
    api._release_tts_project("demo")


def test_cancel_before_commit_leaves_draft_untouched(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.register()
    pending: list[tuple[Any, dict]] = []
    monkeypatch.setattr(api_voiceover_routes, "_start_worker", lambda t, k: pending.append((t, k)))
    started = env.call("POST", "/jobs/start", expected_revision_id="rev_1").get_json()["result"]
    assert env.call("POST", "/jobs/start", expected_revision_id="rev_1").status_code == 409
    cancel = env.client.post(f"{BASE}/jobs/{started['job_id']}/cancel", json={"project": "demo"})
    assert cancel.status_code == 202 and cancel.get_json()["result"]["status"] == "cancelling"
    target, kwargs = pending[0]
    target(**kwargs)
    final = env.client.get(started["status_url"]).get_json()["result"]
    assert final["status"] == "cancelled"
    assert load_recorded_voiceover(env.paths)["ranges"] == []
    assert (
        env.client.post(
            f"{BASE}/jobs/{started['job_id']}/cancel", json={"project": "demo"}
        ).status_code
        == 409
    )
    assert api._reserve_tts_project("demo")  # the project is released
    api._release_tts_project("demo")


def test_job_orphaned_by_restart_is_reported_as_interrupted(env: Env) -> None:
    atomic_write_json(
        env.paths["recorded_voiceover_job_status"],
        {"id": "deadbeef", "project": "demo", "status": "running", "message": "Recognizing"},
    )
    response = env.client.get(f"{BASE}/jobs/deadbeef", query_string={"project": "demo"})
    assert response.get_json()["result"]["status"] == "interrupted"
    assert env.state()["job"]["status"] == "interrupted"
    stored = json.loads(env.paths["recorded_voiceover_job_status"].read_text(encoding="utf-8"))
    assert stored["status"] == "interrupted"


def test_failed_publication_keeps_previous_state(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.register()
    env.run_job()
    env.edit("PATCH", "/ranges/b1", status="confirmed")
    assert env.edit("POST", "/apply").status_code == 200
    published_before = env.paths["recorded_voiceover_published"].read_bytes()
    snapshot = env.paths["scenario_versions"] / "rev_1.recorded_voiceover.json"
    snapshot_before = snapshot.read_bytes()

    env.edit("PATCH", "/ranges/b2", status="confirmed")
    real = publish_module.atomic_write_json

    def fail_on_snapshot(path: Path, payload: dict) -> None:
        if Path(path) == snapshot:
            raise OSError("disk full")
        real(path, payload)

    monkeypatch.setattr(publish_module, "atomic_write_json", fail_on_snapshot)
    assert env.edit("POST", "/apply").status_code == 500
    monkeypatch.undo()
    assert env.paths["recorded_voiceover_published"].read_bytes() == published_before
    assert snapshot.read_bytes() == snapshot_before
    assert not env.paths["recorded_voiceover_publish_intent"].exists()
    assert api._reserve_tts_project("demo")  # the reservation was released
    api._release_tts_project("demo")


def test_crashed_publication_is_rolled_forward(env: Env) -> None:
    env.register()
    env.run_job()
    env.edit("PATCH", "/ranges/b1", status="confirmed")
    document = env.doc
    atomic_write_json(env.paths["normalized"], document)  # the revision the journal belongs to
    data = load_recorded_voiceover(env.paths)
    published = publish_module.build_published_map(data, document, set())
    atomic_write_json(
        env.paths["recorded_voiceover_publish_intent"],
        {"schema_version": 1, "published": published},
    )
    assert env.state()["published"]["published_sha256"] == published["published_sha256"]
    assert not env.paths["recorded_voiceover_publish_intent"].exists()


def test_scan_registers_dropped_recording_and_starts_local_alignment(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import api_voiceover_jobs

    monkeypatch.setattr(
        api.voiceover_jobs,
        "trigger_voiceover_autodetect",
        functools.partial(api_voiceover_jobs.trigger_voiceover_autodetect, run_async=False),
    )
    empty = env.call("POST", "/scan").get_json()["result"]
    assert empty["job"] is None  # nothing dropped: TTS as before
    make_wav(env.paths["inbox_voiceover"] / "Закадр.wav")
    result = env.call("POST", "/scan", expected_revision_id="rev_1").get_json()["result"]
    assert result["job"]["asr_backend"] == "auto"
    assert env.calls == 1
    assert [b["range"]["status"] for b in result["state"]["blocks"][:3]] == ["needs_review"] * 3
    assert env.call("POST", "/scan").get_json()["result"]["job"] is None  # registered once
