from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import threading
import time

import pytest

import api
import job_runtime
from stage1.config import validate_env, warning_variable_names


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(api, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(api, "PROJECTS_DIR", projects)
    monkeypatch.setattr(
        api,
        "NORMALIZATION_MEMORY_PATH",
        tmp_path / "data" / "normalization_memory.sqlite3",
    )
    monkeypatch.setitem(api.app.config, "TESTING", True)
    monkeypatch.delitem(api.app.config, "API_TOKEN", raising=False)
    return api.app.test_client()


@pytest.fixture()
def authed(client, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(api.app.config, "API_TOKEN", "secret-token")
    api._AUTH_THROTTLE.clear()
    yield client
    api._AUTH_THROTTLE.clear()


BEARER = {"Authorization": "Bearer secret-token"}


# --- S3: separate body limits for JSON and uploads -------------------------


def test_default_body_limit_is_small_and_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ROUGH_CUT_MAX_JSON_BODY_BYTES", raising=False)
    assert api._default_request_body_limit() == 16 * 1024 * 1024
    monkeypatch.setenv("ROUGH_CUT_MAX_JSON_BODY_BYTES", "2048")
    assert api._default_request_body_limit() == 2048
    assert api.app.config["MAX_CONTENT_LENGTH"] < api._request_body_limit()


def test_json_endpoint_is_limited_but_audio_upload_keeps_large_limit(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(api.app.config, "MAX_CONTENT_LENGTH", 1024)
    blob = "x" * 4096

    too_big = client.post("/stage1/analyze", json={"project": "demo", "pad": blob})
    assert too_big.status_code == 413

    upload = client.put(
        "/stage2/sync-sources/guide_01/audio",
        data={"project": "demo", "audio": (BytesIO(b"x" * 4096), "guide.wav")},
        content_type="multipart/form-data",
    )
    assert upload.status_code != 413


def test_audio_upload_above_upload_limit_is_413(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "_request_body_limit", lambda: 512)
    upload = client.put(
        "/stage2/sync-sources/guide_01/audio",
        data={"project": "demo", "audio": (BytesIO(b"x" * 4096), "guide.wav")},
        content_type="multipart/form-data",
    )
    assert upload.status_code == 413


# --- S4: only audio files can be streamed as guide audio -------------------


def _make_source(client, tmp_path: Path, guide_audio: str, content: bytes) -> None:
    created = client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "id": "guide_01",
            "premiere_sequence": "SYNC_GUIDE_01",
            "sequence_id": "premiere-guide_01",
            "duration_sec": 10.0,
            "guide_audio": guide_audio,
        },
    )
    assert created.status_code == 201, created.get_json()
    target = tmp_path / "projects" / "demo" / guide_audio
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def test_sync_audio_endpoint_does_not_serve_non_audio_project_files(client, tmp_path: Path) -> None:
    secret = json.dumps({"api_key": "leak"}).encode()
    _make_source(client, tmp_path, "work/openrouter_usage.json", secret)

    response = client.get("/stage2/sync-sources/guide_01/audio?project=demo")

    assert response.status_code in {400, 404}
    assert b"leak" not in response.data


def test_sync_audio_endpoint_serves_allowed_audio_suffix(client, tmp_path: Path) -> None:
    _make_source(client, tmp_path, "media/reviews/guide.mp3", b"ID3audio")

    response = client.get("/stage2/sync-sources/guide_01/audio?project=demo")

    assert response.status_code == 200
    assert response.data == b"ID3audio"


# --- S5: global concurrency slot for heavy jobs ----------------------------


def test_acquire_slot_waits_then_succeeds_and_honors_cancel() -> None:
    semaphore = threading.Semaphore(1)
    assert job_runtime.acquire_slot(semaphore, poll_seconds=0.01) is True
    cancel = threading.Event()
    threading.Timer(0.05, cancel.set).start()
    assert job_runtime.acquire_slot(semaphore, cancel, poll_seconds=0.01) is False
    semaphore.release()
    assert job_runtime.acquire_slot(semaphore, threading.Event(), poll_seconds=0.01) is True


def test_heavy_job_limit_env_default_is_two(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ROUGH_CUT_MAX_HEAVY_JOBS", raising=False)
    assert api._heavy_job_limit() == 2
    monkeypatch.setenv("ROUGH_CUT_MAX_HEAVY_JOBS", "5")
    assert api._heavy_job_limit() == 5
    monkeypatch.setenv("ROUGH_CUT_MAX_HEAVY_JOBS", "0")
    assert api._heavy_job_limit() == 2


def test_normalization_job_queues_behind_global_heavy_slot(
    client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    semaphore = threading.Semaphore(1)
    assert semaphore.acquire(blocking=False)
    monkeypatch.setattr(api, "_HEAVY_JOB_SEMAPHORE", semaphore)
    started = threading.Event()

    def fake_generate(*_args, **_kwargs):
        started.set()
        return {"ok": True}

    monkeypatch.setattr(api, "generate_normalization", fake_generate)
    job = api._create_normalization_job(
        "demo", input_path=tmp_path / "scenario.docx", use_llm=False, hint="", llm_preset="default"
    )
    assert api._reserve_normalization_project("demo")
    worker = threading.Thread(
        target=api._run_normalization_job,
        kwargs={
            "job_id": job["id"],
            "project": "demo",
            "input_path": tmp_path / "scenario.docx",
            "output_path": tmp_path / "out.json",
            "use_llm": False,
            "hint": "",
            "llm_preset": "default",
        },
        daemon=True,
    )
    worker.start()
    time.sleep(0.4)
    assert not started.is_set()
    assert api._get_normalization_job(job["id"])["status"] == "queued"

    semaphore.release()
    assert started.wait(5)
    worker.join(5)
    assert api._get_normalization_job(job["id"])["status"] == "completed"
    # The slot is returned after the job finished.
    assert semaphore.acquire(blocking=False)


# --- S8: /health does not leak raw env values without auth -----------------


def test_validate_env_warning_names_strip_values() -> None:
    warnings = validate_env({"MAX_ZK_BLOCKS": "secret-garbage", "TTS_PROVIDER": "bogus"})
    names = warning_variable_names(warnings)
    assert "MAX_ZK_BLOCKS" in names
    assert "TTS_PROVIDER" in names
    assert all("garbage" not in name and "bogus" not in name for name in names)


def test_health_hides_warning_details_without_auth(authed, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        api, "_CONFIG_WARNINGS", ["MAX_ZK_BLOCKS='sk-leaked-value' is not an integer"]
    )

    anonymous = authed.get("/health")
    assert anonymous.status_code == 200
    assert "sk-leaked-value" not in anonymous.get_data(as_text=True)
    payload = anonymous.get_json()
    assert payload["configuration_warnings"] == ["MAX_ZK_BLOCKS"]

    privileged = authed.get("/health", headers=BEARER)
    assert "sk-leaked-value" in privileged.get_data(as_text=True)


# --- S9: no absolute filesystem paths in error responses -------------------


def test_json_error_strips_absolute_project_paths(client, tmp_path: Path) -> None:
    with api.app.test_request_context():
        body, status = api._json_error(f"boom: '{tmp_path}/projects/demo/work/x.json' missing", 404)
    text = body.get_data(as_text=True)
    assert str(tmp_path) not in text
    assert "projects/demo/work/x.json" in text
    assert status == 404


def test_missing_normalization_input_does_not_leak_absolute_path(client, tmp_path: Path) -> None:
    response = client.post(
        "/stage1/normalization/generate",
        json={"project": "demo", "input_path": "projects/demo/input/missing.docx"},
    )
    assert response.status_code == 404
    error = response.get_json()["error"]
    assert str(tmp_path) not in error
    assert "missing.docx" in error


# --- S11: strict revision id ----------------------------------------------


@pytest.mark.parametrize("action", ["activate", "reconcile"])
@pytest.mark.parametrize("revision_id", ["rev_١٢٣", "rev_1²", "rev_", "rev_1x", "REV_1"])
def test_revision_id_must_be_ascii_digits(client, action: str, revision_id: str) -> None:
    response = client.post(f"/stage4/revisions/{revision_id}/{action}", json={"project": "demo"})
    assert response.status_code in {400, 404}
    if response.status_code == 400:
        assert response.get_json()["error"] == "invalid revision id"


def test_revision_id_pattern_rejects_trailing_newline() -> None:
    from api_stage4_routes import _REVISION_ID_RE

    assert _REVISION_ID_RE.fullmatch("rev_12")
    assert not _REVISION_ID_RE.fullmatch("rev_12\n")
    assert not _REVISION_ID_RE.fullmatch("rev_١٢")


# --- S2-lite: failed-auth throttle ----------------------------------------


def test_failed_auth_throttle_blocks_after_limit_with_retry_after(authed) -> None:
    for _ in range(api._AUTH_THROTTLE.max_failures):
        assert authed.get("/", headers={"Authorization": "Bearer wrong"}).status_code == 401

    blocked = authed.get("/", headers={"Authorization": "Bearer wrong"})
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    # Lockout also covers the attacker who finally guesses the token.
    assert authed.get("/", headers=BEARER).status_code == 429


def test_successful_auth_is_not_counted_or_limited(authed) -> None:
    for _ in range(api._AUTH_THROTTLE.max_failures * 3):
        assert authed.get("/", headers=BEARER).status_code == 200


def test_request_without_credentials_does_not_count_as_failure(authed) -> None:
    for _ in range(api._AUTH_THROTTLE.max_failures * 2):
        assert authed.get("/").status_code == 401
    assert authed.get("/", headers=BEARER).status_code == 200


def test_successful_auth_resets_failure_counter(authed) -> None:
    for _ in range(api._AUTH_THROTTLE.max_failures - 1):
        assert authed.get("/", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert authed.get("/", headers=BEARER).status_code == 200
    for _ in range(api._AUTH_THROTTLE.max_failures - 1):
        assert authed.get("/", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert authed.get("/", headers=BEARER).status_code == 200


def test_zero_max_failures_disables_throttle(authed, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api._AUTH_THROTTLE, "max_failures", 0)
    for _ in range(50):
        assert authed.get("/", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert len(api._AUTH_THROTTLE) == 0
    assert authed.get("/", headers=BEARER).status_code == 200


def test_throttle_zero_is_disabled_and_success_clears_only_own_key() -> None:
    throttle = api.FailedAuthThrottle(max_failures=0)
    for _ in range(5):
        throttle.record_failure("a")
    assert throttle.retry_after("a") == 0
    throttle = api.FailedAuthThrottle(max_failures=2)
    throttle.record_failure("a")
    throttle.record_failure("b")
    throttle.record_success("a")
    assert len(throttle) == 1


def test_non_negative_env_int_allows_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_TEST_N", "0")
    assert api._non_negative_env_int("X_TEST_N", 10) == 0
    monkeypatch.setenv("X_TEST_N", "-3")
    assert api._non_negative_env_int("X_TEST_N", 10) == 10
    monkeypatch.setenv("X_TEST_N", "abc")
    assert api._non_negative_env_int("X_TEST_N", 10) == 10


def test_redact_local_paths_respects_segment_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "app"
    projects = root / "projects"
    monkeypatch.setattr(api, "ROOT_DIR", root)
    monkeypatch.setattr(api, "PROJECTS_DIR", projects)
    base = str(root)
    redact = api._redact_local_paths
    sibling = f"{base}lication/x"
    assert redact(sibling) == sibling
    assert redact("see https://h/app/y") == "see https://h/app/y"
    assert redact(f"https://h{base}/y") == f"https://h{base}/y"
    assert redact(f"missing {base}") == "missing ."
    assert redact(f"cannot open '{base}'") == "cannot open '.'"
    assert redact(f"file {base}/data/a.txt: bad") == "file data/a.txt: bad"
    assert redact(f"file {projects}/p1/a.txt: bad") == "file projects/p1/a.txt: bad"
    assert redact(f"dir {projects}") == "dir projects"


def test_trim_terminal_jobs_orders_mixed_z_and_offset_timestamps() -> None:
    from job_runtime import trim_terminal_jobs

    jobs = {
        "newest": {"status": "completed", "finished_at": "2026-01-01T00:00:05+00:00"},
        "middle": {"status": "completed", "finished_at": "2026-01-01T00:00:03Z"},
        "oldest": {"status": "completed", "finished_at": "2026-01-01T00:00:01+00:00"},
        "garbage": {"status": "failed", "finished_at": "not-a-date"},
    }
    assert trim_terminal_jobs(jobs, max_jobs=3) == ["garbage"]
    assert trim_terminal_jobs(jobs, max_jobs=2) == ["oldest"]
    assert set(jobs) == {"newest", "middle"}


def test_throttle_window_expiry_and_bounded_size() -> None:
    now = [1000.0]
    throttle = api.FailedAuthThrottle(
        max_failures=2, window_seconds=60, max_entries=3, clock=lambda: now[0]
    )
    throttle.record_failure("a")
    throttle.record_failure("a")
    assert throttle.retry_after("a") >= 1
    now[0] += 61
    assert throttle.retry_after("a") == 0
    for index in range(10):
        throttle.record_failure(f"ip{index}")
    assert len(throttle) <= 3


def test_missing_token_configuration_still_fails_closed_before_throttle(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(api.app.config, "API_TOKEN", "")
    assert client.get("/", headers={"Authorization": "Bearer x"}).status_code == 503
