from pathlib import Path
from types import SimpleNamespace
import threading
import time
import uuid
import wave

from flask import Flask
import pytest

import api_final_review_jobs
from api_final_review_jobs import (
    create_final_review_job,
    load_persisted_final_review_job,
    record_final_review_iteration,
    release_final_review_project,
    run_final_review_job,
    update_final_review_job,
)
import api_final_review_routes
from api_final_review_routes import (
    _apply,
    _project_audio_path,
    _stable_wav,
    _start,
    _status,
    _wav_duration,
    register_final_review_routes,
)
from job_runtime import get_job, release_project, reserve_project, update_job
from stage2.models import FullTimeline
from stage2.whisper_process import WhisperCancelledError, WhisperTimeoutError


def _error(message: str, status: int):
    return {"error": message}, status


def test_final_review_start_rejects_missing_timeline_before_audio_work(tmp_path: Path) -> None:
    app = Flask(__name__)
    root = tmp_path / "projects/demo"
    work = root / "work"
    work.mkdir(parents=True)
    services = SimpleNamespace(
        _resolve_project_name=lambda _value: "demo",
        _ensure_project=lambda _project: {
            "root": root,
            "work": work,
            "final_review_audio": work / "final_review.wav",
        },
        _normalization_required=lambda _project: None,
        _json_error=_error,
        FullTimeline=FullTimeline,
        _read_json_safe=lambda _path: {},
    )
    with app.test_request_context(json={"project": "demo"}):
        response, status = _start(services)
    assert status == 404
    assert response["error"].startswith("full_timeline.json not found")


def test_final_review_apply_rejects_report_from_another_job(tmp_path: Path) -> None:
    app = Flask(__name__)
    report_path = tmp_path / "final_review_report.json"
    services = SimpleNamespace(
        _get_final_review_job=lambda _job_id: {
            "id": "job-new",
            "project": "demo",
            "status": "completed",
            "source_sequence": {"sequenceId": "sequence-1"},
            "assembly_manifest_id": "manifest-1",
        },
        _resolve_project_name=lambda _value: "demo",
        _project_paths=lambda _project: {"final_review_report": report_path},
        _read_json_safe=lambda _path: {
            "job_id": "job-old",
            "project": "demo",
            "source_sequence": {"sequenceId": "sequence-1"},
            "assembly_manifest_id": "manifest-1",
        },
        _json_error=_error,
    )
    with app.test_request_context(json={"project": "demo"}):
        response, status = _apply(services, "job-new")
    assert status == 409
    assert response["error"] == "final review report does not belong to this job"


def test_final_review_reads_wav_duration_and_rejects_invalid_file(tmp_path: Path) -> None:
    import wave

    wav_path = tmp_path / "review.wav"
    with wave.open(str(wav_path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(48_000)
        target.writeframes(b"\0\0" * 96_000)
    assert _wav_duration(wav_path) == 2.0

    wav_path.write_bytes(b"partial")
    with pytest.raises(ValueError, match="invalid final review WAV"):
        _wav_duration(wav_path)


def test_final_review_audio_path_must_be_project_local_wav(tmp_path: Path) -> None:
    root = tmp_path / "demo"
    root.mkdir()
    paths = {"root": root, "final_review_audio": root / "review.wav"}

    assert (
        _project_audio_path(SimpleNamespace(), paths, "nested/review.wav")
        == (root / "nested/review.wav").resolve()
    )
    with pytest.raises(ValueError, match="WAV inside the project"):
        _project_audio_path(SimpleNamespace(), paths, "../outside.wav")
    with pytest.raises(ValueError, match="WAV inside the project"):
        _project_audio_path(SimpleNamespace(), paths, "review.mp3")


def test_stable_wav_accepts_a_complete_unchanged_file(tmp_path: Path, monkeypatch) -> None:
    wav_path = tmp_path / "review.wav"
    wav_path.write_bytes(b"x" * 45)
    monkeypatch.setattr(api_final_review_routes.time, "sleep", lambda _seconds: None)
    assert _stable_wav(wav_path) is True


def test_final_review_status_reports_missing_and_existing_jobs() -> None:
    app = Flask(__name__)
    with app.app_context():
        missing, missing_status = _status(
            SimpleNamespace(_get_final_review_job=lambda _job_id: None, _json_error=_error),
            "missing",
        )
        found = _status(
            SimpleNamespace(
                _get_final_review_job=lambda _job_id: {"id": "job-1", "status": "running"},
                _json_error=_error,
            ),
            "job-1",
        )

    assert missing_status == 404
    assert found.status_code == 200
    assert found.get_json()["result"]["id"] == "job-1"


def test_final_review_status_restores_matching_checkpoint_after_restart() -> None:
    app = Flask(__name__)
    checkpoint = {"id": "job-1", "project": "demo", "status": "interrupted"}
    with app.test_request_context("/stage2/final-review/jobs/job-1?project=demo"):
        response = _status(
            SimpleNamespace(
                _get_final_review_job=lambda _job_id: None,
                _resolve_project_name=lambda value: value,
                _load_persisted_final_review_job=lambda project: (
                    checkpoint if project == "demo" else None
                ),
                _json_error=_error,
            ),
            "job-1",
        )

    assert response.status_code == 200
    assert response.get_json()["result"] == checkpoint


def test_final_review_checkpoint_marks_orphaned_worker_interrupted(tmp_path: Path) -> None:
    status_path = tmp_path / "final_review_job_status.json"
    services = SimpleNamespace(
        _project_paths=lambda _project: {"final_review_job_status": status_path},
        _read_json_safe=lambda _path: {"id": "job-1", "project": "demo", "status": "running"},
    )

    restored = load_persisted_final_review_job(services, "demo")

    assert restored is not None
    assert restored["status"] == "interrupted"
    assert "restart" in restored["message"]


def test_final_review_start_rejects_stale_duration(tmp_path: Path, monkeypatch) -> None:
    app = Flask(__name__)
    root = tmp_path / "demo"
    work = root / "work"
    work.mkdir(parents=True)
    (work / "full_timeline.json").write_text("{}", encoding="utf-8")
    audio = work / "review.wav"
    audio.write_bytes(b"ready")
    monkeypatch.setattr(api_final_review_routes, "_stable_wav", lambda _path: True)
    monkeypatch.setattr(api_final_review_routes, "_wav_duration", lambda _path: 12.0)
    services = SimpleNamespace(
        _resolve_project_name=lambda _value: "demo",
        _ensure_project=lambda _project: {
            "root": root,
            "work": work,
            "normalized": root / "normalized.json",
            "final_review_audio": audio,
        },
        _normalization_required=lambda _project: None,
        _json_error=_error,
        FullTimeline=SimpleNamespace(
            from_dict=lambda _payload: SimpleNamespace(total_duration_sec=10.0)
        ),
        _read_json_safe=lambda _path: {},
    )

    with app.test_request_context(json={"project": "demo"}):
        response, status = _start(services)

    assert status == 409
    assert response["error"].startswith("stale_snapshot")


def test_final_review_start_creates_job_and_worker(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(api_final_review_routes, "resolve_backend", lambda _backend: "cloud-audio", raising=False)
    app = Flask(__name__)
    root = tmp_path / "demo"
    work = root / "work"
    work.mkdir(parents=True)
    timeline_path = work / "full_timeline.json"
    timeline_path.write_text("{}", encoding="utf-8")
    audio = work / "review.wav"
    audio.write_bytes(b"ready")
    started: list[dict] = []

    class ImmediateThread:
        def __init__(self, *, target, kwargs, daemon):
            started.append({"target": target, "kwargs": kwargs, "daemon": daemon})

        def start(self):
            return None

    monkeypatch.setattr(api_final_review_routes, "_stable_wav", lambda _path: True)
    monkeypatch.setattr(api_final_review_routes, "_wav_duration", lambda _path: 10.0)
    services = SimpleNamespace(
        _resolve_project_name=lambda _value: "demo",
        _ensure_project=lambda _project: {
            "root": root,
            "work": work,
            "normalized": root / "normalized.json",
            "assembly_manifest": work / "assembly_manifest.json",
            "final_review_audio": audio,
        },
        _normalization_required=lambda _project: None,
        _json_error=_error,
        FullTimeline=SimpleNamespace(
            from_dict=lambda _payload: SimpleNamespace(total_duration_sec=10.0)
        ),
        _read_json_safe=lambda _path: {},
        _reserve_final_review_project=lambda _project: True,
        build_assembly_manifest=lambda **_kwargs: {"manifest_id": "manifest-1"},
        attach_scenario_revision=lambda _manifest, _path: None,
        _atomic_write_json_file=lambda _path, _payload: None,
        _create_final_review_job=lambda _project, _audio, _payload: {
            "id": "job-1",
            "status": "queued",
        },
        _run_final_review_job=lambda **_kwargs: None,
        _release_final_review_project=lambda _project: None,
        threading=SimpleNamespace(Thread=ImmediateThread),
    )

    with app.test_request_context(json={"project": "demo"}):
        response, status = _start(services)

    assert status == 202
    assert response.get_json()["result"]["job_id"] == "job-1"
    assert started[0]["kwargs"]["source_sequence"] == {}


@pytest.mark.parametrize(
    ("mutation", "expected_error", "expected_status"),
    [
        ("normalization", "normalization required", 409),
        ("invalid_timeline", "invalid full_timeline.json", 400),
        ("missing_audio", "final review WAV was not found", 404),
        ("unstable_audio", "still being exported", 409),
        ("invalid_duration", "could not convert string to float", 400),
        ("busy", "another mutating job is active", 409),
    ],
)
def test_final_review_start_guard_branches(
    tmp_path: Path,
    monkeypatch,
    mutation: str,
    expected_error: str,
    expected_status: int,
) -> None:
    app = Flask(__name__)
    root = tmp_path / mutation
    work = root / "work"
    work.mkdir(parents=True)
    timeline_path = work / "full_timeline.json"
    timeline_path.write_text("{}", encoding="utf-8")
    audio = work / "review.wav"
    if mutation != "missing_audio":
        audio.write_bytes(b"ready")
    monkeypatch.setattr(
        api_final_review_routes,
        "_stable_wav",
        lambda _path: mutation != "unstable_audio",
    )
    monkeypatch.setattr(api_final_review_routes, "_wav_duration", lambda _path: 10.0)

    def parse_timeline(_payload):
        if mutation == "invalid_timeline":
            raise ValueError("bad timeline")
        duration = "invalid" if mutation == "invalid_duration" else 10.0
        return SimpleNamespace(total_duration_sec=duration)

    services = SimpleNamespace(
        _resolve_project_name=lambda _value: "demo",
        _ensure_project=lambda _project: {
            "root": root,
            "work": work,
            "normalized": root / "normalized.json",
            "final_review_audio": audio,
        },
        _normalization_required=lambda _project: (
            _error("normalization required", 409) if mutation == "normalization" else None
        ),
        _json_error=_error,
        FullTimeline=SimpleNamespace(from_dict=parse_timeline),
        _read_json_safe=lambda _path: {},
        _reserve_final_review_project=lambda _project: mutation != "busy",
    )

    with app.test_request_context(json={"project": "demo"}):
        response, status = _start(services)

    assert status == expected_status
    assert expected_error in response["error"]


def test_final_review_apply_records_matching_report(tmp_path: Path) -> None:
    app = Flask(__name__)
    recorded: list[tuple] = []
    job = {
        "id": "job-1",
        "project": "demo",
        "status": "completed",
        "source_sequence": {"sequenceId": "sequence-1"},
        "assembly_manifest_id": "manifest-1",
    }
    services = SimpleNamespace(
        _get_final_review_job=lambda _job_id: job,
        _resolve_project_name=lambda _value: "demo",
        _project_paths=lambda _project: {"final_review_report": tmp_path / "report.json"},
        _read_json_safe=lambda _path: {
            "job_id": "job-1",
            "project": "demo",
            "source_sequence": {"sequenceId": "sequence-1"},
            "assembly_manifest_id": "manifest-1",
            "corrections": [{"block_uid": "b1"}],
        },
        _record_final_review_iteration=lambda *args: (
            recorded.append(args) or {"id": "iteration_0001"}
        ),
        _json_error=_error,
    )

    with app.test_request_context(json={"project": "demo"}):
        response = _apply(services, "job-1")

    assert response.status_code == 200
    assert response.get_json()["result"]["iteration"]["id"] == "iteration_0001"
    assert recorded[0][2] == [{"block_uid": "b1"}]


def test_final_review_apply_restores_completed_checkpoint_after_restart(tmp_path: Path) -> None:
    app = Flask(__name__)
    recorded: list[tuple] = []
    job = {
        "id": "job-1",
        "project": "demo",
        "status": "completed",
        "source_sequence": {"sequenceId": "sequence-1"},
        "assembly_manifest_id": "manifest-1",
    }
    services = SimpleNamespace(
        _get_final_review_job=lambda _job_id: None,
        _load_persisted_final_review_job=lambda project: job if project == "demo" else None,
        _resolve_project_name=lambda value: value,
        _project_paths=lambda _project: {"final_review_report": tmp_path / "report.json"},
        _read_json_safe=lambda _path: {
            "job_id": "job-1",
            "project": "demo",
            "source_sequence": {"sequenceId": "sequence-1"},
            "assembly_manifest_id": "manifest-1",
            "corrections": [],
        },
        _record_final_review_iteration=lambda *args: (
            recorded.append(args) or {"id": "iteration_0001"}
        ),
        _json_error=_error,
    )

    with app.test_request_context(json={"project": "demo"}):
        response = _apply(services, "job-1")

    assert response.status_code == 200
    assert response.get_json()["result"]["iteration"]["id"] == "iteration_0001"


@pytest.mark.parametrize(
    ("case", "expected_error"),
    [
        ("missing", "job not found"),
        ("running", "final review is not completed"),
        ("bad_project_input", "invalid project"),
        ("other_project", "project does not match"),
        ("other_report_project", "report does not belong to this project"),
        ("other_manifest", "assembly snapshot changed"),
        ("other_sequence", "sequence identity changed"),
    ],
)
def test_final_review_apply_guard_branches(tmp_path: Path, case: str, expected_error: str) -> None:
    app = Flask(__name__)
    job = (
        None
        if case == "missing"
        else {
            "id": "job-1",
            "project": "demo",
            "status": "running" if case == "running" else "completed",
            "source_sequence": {"sequenceId": "sequence-1"},
            "assembly_manifest_id": "manifest-1",
        }
    )
    report = {
        "job_id": "job-1",
        "project": "other" if case == "other_report_project" else "demo",
        "source_sequence": {
            "sequenceId": "sequence-2" if case == "other_sequence" else "sequence-1"
        },
        "assembly_manifest_id": "manifest-2" if case == "other_manifest" else "manifest-1",
    }

    def resolve_project(_value):
        if case == "bad_project_input":
            raise ValueError("invalid project")
        return "other" if case == "other_project" else "demo"

    services = SimpleNamespace(
        _get_final_review_job=lambda _job_id: job,
        _resolve_project_name=resolve_project,
        _project_paths=lambda _project: {"final_review_report": tmp_path / "report.json"},
        _read_json_safe=lambda _path: report,
        _json_error=_error,
    )
    with app.test_request_context(json={"project": "demo"}):
        response, status = _apply(services, "job-1")

    assert status in {400, 404, 409}
    assert expected_error in response["error"]


def test_register_final_review_routes_exposes_all_endpoints() -> None:
    app = Flask(__name__)
    register_final_review_routes(app, SimpleNamespace())
    routes = {rule.rule: set(rule.methods or ()) for rule in app.url_map.iter_rules()}
    assert "POST" in routes["/stage2/final-review/start"]
    assert "GET" in routes["/stage2/final-review/jobs/<job_id>"]
    assert "POST" in routes["/stage2/final-review/jobs/<job_id>/apply"]


def _job_services(tmp_path: Path, *, transcribe=None):
    root = tmp_path / "demo"
    work = root / "work"
    work.mkdir(parents=True)
    paths = {
        "root": root,
        "work": work,
        "timeline": work / "timeline.json",
        "normalized": root / "normalized.json",
        "assembly_manifest": work / "assembly_manifest.json",
        "final_review_words": work / "final_review_words.json",
        "final_review_report": work / "final_review_report.json",
        "final_review_job_status": work / "final_review_job.json",
        "final_review_iterations": work / "iterations.json",
    }
    jobs = {"job-1": {"id": "job-1", "project": "demo", "status": "queued"}}
    active = {"demo"}
    writes: list[tuple[Path, dict]] = []
    validations: list[dict] = []
    usage: list[dict] = []
    services = SimpleNamespace(
        _FINAL_REVIEW_JOBS=jobs,
        _FINAL_REVIEW_JOBS_LOCK=threading.RLock(),
        _FINAL_REVIEW_ACTIVE_PROJECTS=active,
        _FINAL_REVIEW_CANCEL_EVENTS={"job-1": threading.Event()},
        threading=threading,
        time=time,
        WhisperCancelledError=WhisperCancelledError,
        uuid=uuid,
        FullTimeline=SimpleNamespace(from_dict=lambda payload: payload),
        _project_transition_lock=lambda _project: threading.RLock(),
        _project_has_active_mutation=lambda _project: False,
        reserve_project=reserve_project,
        release_project=release_project,
        get_job=get_job,
        update_job=update_job,
        _utc_now_iso=lambda: "2026-09-09T12:00:00+00:00",
        _project_paths=lambda _project: paths,
        _atomic_write_json_file=lambda path, payload: writes.append((path, dict(payload))),
        _read_json_safe=lambda path: (
            {"manifest_id": "manifest-1", "summary": {}, "findings": []}
            if path == paths["assembly_manifest"]
            else {"scenario_revision_id": "revision-1", "iterations": []}
        ),
        _make_openrouter_usage_callback=lambda _project, **_context: usage.append,
        validate_assembly_manifest=lambda manifest, **_paths: validations.append(manifest),
        transcribe_audio=transcribe
        or (lambda _path, **_options: {"backend": "local", "model": "m", "language": "ru"}),
        ingest_word_index=lambda _result, _source, **_metadata: SimpleNamespace(),
        save_word_index=lambda _path, _index: None,
        review_rendered_timeline=lambda _timeline, _index: {"corrections": []},
        _test_paths=paths,
        _test_writes=writes,
        _test_validations=validations,
        _test_usage=usage,
    )
    services._update_final_review_job = lambda job_id, **updates: update_final_review_job(
        services, job_id, **updates
    )
    services._release_final_review_project = lambda project: release_final_review_project(
        services, project
    )
    return services


def test_final_review_jobs_create_run_and_record_iteration(tmp_path: Path) -> None:
    services = _job_services(tmp_path)
    audio = services._test_paths["work"] / "review.wav"
    created = create_final_review_job(
        services,
        "demo",
        audio,
        {"source_sequence": {"sequenceId": "s1"}, "assembly_manifest_id": "manifest-1"},
    )
    assert created["audio_path"] == "work/review.wav"

    run_final_review_job(
        services,
        job_id="job-1",
        project="demo",
        audio_path=audio,
        language="ru",
        backend=None,
        model=None,
        source_sequence={"sequenceId": "s1"},
    )
    assert services._FINAL_REVIEW_JOBS["job-1"]["status"] == "completed"
    assert len(services._test_validations) == 2
    assert "demo" not in services._FINAL_REVIEW_ACTIVE_PROJECTS

    iteration = record_final_review_iteration(
        services,
        "demo",
        {"id": "job-1", "source_sequence": {"sequenceId": "s1"}},
        [{"block_uid": "b1"}],
    )
    assert iteration["id"] == "iteration_0001"
    assert services._test_writes[-1][1]["iterations"] == [iteration]


def test_final_review_worker_records_cloud_cost_and_failure_cost(tmp_path: Path) -> None:
    services = _job_services(
        tmp_path,
        transcribe=lambda _path, **_options: {
            "backend": "cloud-audio",
            "model": "cloud-model",
            "language": "ru",
            "backend_metadata": {"cost_usd": 0.25},
        },
    )
    run_final_review_job(
        services,
        job_id="job-1",
        project="demo",
        audio_path=services._test_paths["work"] / "review.wav",
        language="ru",
        backend="cloud-audio",
        model="cloud-model",
        source_sequence={},
    )
    assert services._test_usage[0]["cost_usd"] == 0.25

    class CostError(RuntimeError):
        cost_usd = 0.5

    failed = _job_services(
        tmp_path / "failure", transcribe=lambda *_a, **_k: (_ for _ in ()).throw(CostError("boom"))
    )
    run_final_review_job(
        failed,
        job_id="job-1",
        project="demo",
        audio_path=failed._test_paths["work"] / "review.wav",
        language="ru",
        backend="cloud-audio",
        model="cloud-model",
        source_sequence={},
    )
    assert failed._FINAL_REVIEW_JOBS["job-1"]["status"] == "failed"
    assert failed._test_usage[0]["cost_usd"] == 0.5
    assert "demo" not in failed._FINAL_REVIEW_ACTIVE_PROJECTS


@pytest.fixture
def controller(tmp_path, monkeypatch):
    """Real registry/routes/files with only external transcription and threads deferred."""
    services = _job_services(tmp_path)
    paths = services._test_paths
    audio = paths['work'] / 'review.wav'
    with wave.open(str(audio), 'wb') as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(1)
        target.writeframes(b'\0\0' * 150)
    paths['final_review_audio'] = audio
    (paths['work'] / 'full_timeline.json').write_text('{}')
    services._FINAL_REVIEW_JOBS.clear()
    services._FINAL_REVIEW_ACTIVE_PROJECTS.clear()
    services._FINAL_REVIEW_CANCEL_EVENTS.clear()
    services.FullTimeline = SimpleNamespace(
        from_dict=lambda _payload: SimpleNamespace(total_duration_sec=150)
    )
    services._resolve_project_name = lambda value: (
        value if value == 'demo' else (_ for _ in ()).throw(ValueError('project is required'))
    )
    services._ensure_project = services._project_paths
    services._normalization_required = lambda _project: None
    services._json_error = _error
    services._reserve_final_review_project = lambda project: reserve_project(
        services._FINAL_REVIEW_JOBS_LOCK, services._FINAL_REVIEW_ACTIVE_PROJECTS, project
    )
    services.build_assembly_manifest = lambda **_options: {'manifest_id': 'manifest-1'}
    services.attach_scenario_revision = lambda *_args: None
    services._create_final_review_job = lambda project, audio, payload: create_final_review_job(
        services, project, audio, payload
    )
    services._get_final_review_job = lambda job_id: get_job(
        services._FINAL_REVIEW_JOBS_LOCK, services._FINAL_REVIEW_JOBS, job_id
    )
    services._get_latest_final_review_job = lambda project: (
        api_final_review_jobs.get_latest_final_review_job(services, project)
    )
    services._cancel_final_review_job = lambda job_id: (
        api_final_review_jobs.cancel_final_review_job(services, job_id)
    )
    services._load_persisted_final_review_job = lambda project: load_persisted_final_review_job(
        services, project
    )
    services._run_final_review_job = lambda **options: run_final_review_job(services, **options)
    pending = []

    class DeferredThread:
        def __init__(self, *, target, kwargs, daemon):
            self.target, self.kwargs = target, kwargs
            pending.append(self)

        def start(self):
            pass

    services.threading = SimpleNamespace(Thread=DeferredThread, Event=threading.Event)
    services._test_pending = pending
    monkeypatch.setattr(api_final_review_routes, '_stable_wav', lambda _path: True)
    monkeypatch.setenv('ROUGH_CUT_MAX_USD_PER_JOB', '1')
    monkeypatch.setenv('ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE', '0.06')
    monkeypatch.setenv('FINAL_REVIEW_TIMEOUT_SECONDS', '10')
    app = Flask(__name__)
    register_final_review_routes(app, services)
    return app.test_client(), services


def _start_payload(**updates):
    return {
        'project': 'demo', 'backend': 'cloud-audio',
        'cloud_options': {'chunk_minutes': 1, 'overlap_sec': 10}, **updates,
    }


def _run_pending(services):
    pending = services._test_pending[-1]
    pending.target(**pending.kwargs)


def _assert_released(services, job_id):
    assert 'demo' not in services._FINAL_REVIEW_ACTIVE_PROJECTS
    assert job_id not in services._FINAL_REVIEW_CANCEL_EVENTS


def test_latest_selects_newest_matching_memory_job_then_persisted(controller):
    client, services = controller
    services._FINAL_REVIEW_JOBS.update({
        'new': {'id': 'new', 'project': 'demo', 'status': 'running', 'created_at': '2026-10-15'},
        'old': {'id': 'old', 'project': 'demo', 'status': 'completed', 'created_at': '2026-10-14'},
        'other': {'id': 'other', 'project': 'other', 'created_at': '2026-10-16'},
    })
    assert client.get('/stage2/final-review/latest?project=demo').get_json()['result']['id'] == 'new'
    services._FINAL_REVIEW_JOBS.clear()
    services._read_json_safe = lambda _path: {
        'id': 'disk', 'project': 'demo', 'status': 'cancelling', 'cost_usd': 0.02,
        'reserved_cost_usd': 0.06,
    }
    result = client.get('/stage2/final-review/latest?project=demo').get_json()['result']
    assert result['id'] == 'disk'
    assert result['status'] == 'interrupted'
    assert result['reserved_cost_usd'] == 0.06
    services._read_json_safe = lambda _path: {}
    assert client.get('/stage2/final-review/latest?project=demo').status_code == 404


@pytest.mark.parametrize('project', ['', '../bad'])
def test_latest_rejects_missing_or_invalid_project(controller, project):
    client, _services = controller
    assert client.get('/stage2/final-review/latest', query_string={'project': project}).status_code == 400


def test_estimate_counts_overlap_and_start_uses_identical_options(controller):
    client, services = controller
    estimate = client.post('/stage2/final-review/estimate', json=_start_payload())
    assert estimate.status_code == 200
    assert estimate.get_json()['result'] == {
        'duration': 150.0, 'duration_sec': 150.0, 'planned_chunks': 3, 'estimated_cost_usd': '0.17',
        'cost_limit_usd': 1.0, 'enforced': True,
    }
    assert services._FINAL_REVIEW_JOBS == {}
    response = client.post('/stage2/final-review/start', json=_start_payload(confirmed_estimate_usd=0.17))
    assert response.status_code == 202
    job_id = response.get_json()['result']['job_id']
    queued = client.get(f'/stage2/final-review/jobs/{job_id}').get_json()['result']
    assert queued['estimated_cost_usd'] == '0.17'
    assert queued['cost_limit_usd'] == 1.0
    assert queued['cost_usd'] == 0.0
    observed = {}

    def transcribe(_path, **options):
        observed.update(options)
        running = client.get(f'/stage2/final-review/jobs/{job_id}').get_json()['result']
        assert running['status'] == 'running'
        assert running['estimated_cost_usd'] == '0.17'
        assert running['cost_limit_usd'] == 1.0
        assert running['cost_usd'] == 0.0
        return {'backend': 'cloud-audio', 'backend_metadata': {'cost_usd': 0.12}}

    services.transcribe_audio = transcribe
    _run_pending(services)
    result = client.get(f'/stage2/final-review/jobs/{job_id}').get_json()['result']
    assert result['status'] == 'completed'
    assert result['estimated_cost_usd'] == '0.17'
    assert result['cost_usd'] == 0.12
    assert observed['cloud_options']['chunk_minutes'] == 1
    assert observed['cloud_options']['overlap_sec'] == 10
    assert str(observed['cloud_options']['max_cost_usd']) == '1'
    _assert_released(services, job_id)


@pytest.mark.parametrize('endpoint', ['estimate', 'start'])
@pytest.mark.parametrize('rate,limit,status', [('0', '1', 400), ('0.06', '0.16', 409)])
def test_preflight_blocks_unknown_or_over_budget_before_acceptance(
    controller, monkeypatch, endpoint, rate, limit, status
):
    client, services = controller
    monkeypatch.setenv('ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE', rate)
    monkeypatch.setenv('ROUGH_CUT_MAX_USD_PER_JOB', limit)
    response = client.post(f'/stage2/final-review/{endpoint}', json=_start_payload())
    assert response.status_code == status
    assert services._FINAL_REVIEW_JOBS == {}
    assert services._FINAL_REVIEW_ACTIVE_PROJECTS == set()
    assert services._test_usage == []


def test_start_rejects_changed_confirmation_and_invalid_estimate(controller):
    client, services = controller
    assert client.post('/stage2/final-review/start', json=_start_payload(confirmed_estimate_usd='0.16')).status_code == 409
    assert client.post('/stage2/final-review/start', json=_start_payload(confirmed_estimate_usd='NaN')).status_code == 400
    assert not services._test_pending


def test_estimate_disabled_guard_allows_unknown_cost(controller, monkeypatch):
    client, _services = controller
    monkeypatch.setenv('ROUGH_CUT_MAX_USD_PER_JOB', '0')
    monkeypatch.setenv('ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE', '0')
    response = client.post('/stage2/final-review/estimate', json=_start_payload())
    assert response.status_code == 200
    assert response.get_json()['result']['enforced'] is False
    assert response.get_json()['result']['estimated_cost_usd'] == '0'


def test_cancel_queued_job_prevents_transcription_and_releases_project(controller):
    client, services = controller
    response = client.post('/stage2/final-review/start', json=_start_payload())
    job_id = response.get_json()['result']['job_id']
    cancelled = client.post(f'/stage2/final-review/jobs/{job_id}/cancel')
    assert cancelled.status_code == 202
    assert cancelled.get_json()['result']['status'] == 'cancelling'
    services.transcribe_audio = lambda *_a, **_k: pytest.fail('cancelled job reached transcription')
    _run_pending(services)
    result = client.get(f'/stage2/final-review/jobs/{job_id}').get_json()['result']
    assert result['status'] == 'cancelled'
    assert not any(path == services._test_paths['final_review_report'] for path, _ in services._test_writes)
    _assert_released(services, job_id)


@pytest.mark.parametrize('status', ['completed', 'failed', 'cancelled', 'interrupted'])
def test_terminal_cancel_is_unchanged_and_idempotent(controller, status):
    client, services = controller
    job = {'id': 'terminal', 'project': 'demo', 'status': status, 'message': 'existing', 'cost_usd': 0.04}
    services._FINAL_REVIEW_JOBS['terminal'] = dict(job)
    before = list(services._test_writes)
    for _attempt in range(2):
        response = client.post('/stage2/final-review/jobs/terminal/cancel')
        assert response.status_code == 200
        assert response.get_json()['result'] == job
    assert services._test_writes == before
    assert client.post('/stage2/final-review/jobs/absent/cancel').status_code == 404


def test_cancel_after_transcription_prevents_artifact_writes_and_retains_spend(controller):
    client, services = controller
    response = client.post('/stage2/final-review/start', json=_start_payload())
    job_id = response.get_json()['result']['job_id']

    def transcribe(_path, **options):
        assert options['cancel_event'] is services._FINAL_REVIEW_CANCEL_EVENTS[job_id]
        client.post(f'/stage2/final-review/jobs/{job_id}/cancel')
        return {'backend': 'cloud-audio', 'backend_metadata': {'cost_usd': 0.07}}

    services.transcribe_audio = transcribe
    services.save_word_index = lambda *_args: pytest.fail('cancelled job wrote word index')
    _run_pending(services)
    job = client.get(f'/stage2/final-review/jobs/{job_id}').get_json()['result']
    assert job['status'] == 'cancelled'
    assert job['cost_usd'] == 0.07
    assert services._test_usage[-1]['cost_usd'] == 0.07
    _assert_released(services, job_id)


def test_whole_job_deadline_starts_before_worker_queue(controller, monkeypatch):
    client, services = controller
    clock = [100.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    response = client.post('/stage2/final-review/start', json=_start_payload())
    job_id = response.get_json()['result']['job_id']
    clock[0] = 106.0
    observed = {}

    def transcribe(_path, **options):
        observed.update(options)
        return {'backend': 'local'}

    services.transcribe_audio = transcribe
    _run_pending(services)
    assert observed['deadline_monotonic'] == 110.0
    assert services._FINAL_REVIEW_JOBS[job_id]['status'] == 'completed'
    _assert_released(services, job_id)


def test_expired_queue_deadline_fails_before_transcription(controller, monkeypatch):
    client, services = controller
    clock = [100.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    response = client.post('/stage2/final-review/start', json=_start_payload())
    job_id = response.get_json()['result']['job_id']
    clock[0] = 111.0
    services.transcribe_audio = lambda *_a, **_k: pytest.fail('expired job reached transcription')
    _run_pending(services)
    job = services._FINAL_REVIEW_JOBS[job_id]
    assert job['status'] == 'failed'
    assert 'deadline' in job['error'].lower()
    assert job['estimated_cost_usd'] == '0.17'
    assert job['cost_usd'] == 0
    _assert_released(services, job_id)


@pytest.mark.parametrize('exception,status', [(WhisperTimeoutError, 'failed'), (WhisperCancelledError, 'cancelled')])
def test_process_stop_retains_spent_and_reserved_cost(controller, exception, status):
    client, services = controller
    response = client.post('/stage2/final-review/start', json=_start_payload())
    job_id = response.get_json()['result']['job_id']

    def transcribe(*_args, **_options):
        error = exception('stopped')
        error.cost_usd, error.reserved_cost_usd, error.cost_limit_usd = 0.03, 0.06, 1.0
        raise error

    services.transcribe_audio = transcribe
    _run_pending(services)
    job = services._FINAL_REVIEW_JOBS[job_id]
    assert job['status'] == status
    assert job['cost_usd'] == 0.03
    assert job['reserved_cost_usd'] == 0.06
    assert job['cost_limit_usd'] == 1.0
    assert job['estimated_cost_usd'] == '0.17'
    assert services._test_usage[-1]['cost_usd'] == 0.03
    _assert_released(services, job_id)


def test_progress_persists_latest_budget_at_bounded_cadence(controller, monkeypatch):
    client, services = controller
    clock = [100.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    response = client.post('/stage2/final-review/start', json=_start_payload())
    job_id = response.get_json()['result']['job_id']

    def transcribe(_path, *, progress_callback, budget_callback, **_options):
        budget_callback({'spent_usd': 0.02, 'reserved_usd': 0.06, 'limit_usd': 1.0, 'total_usd': 0.08})
        before = len(services._test_writes)
        for processed in range(1, 101):
            progress_callback(float(processed), 150.0, processed)
        assert len(services._test_writes) <= before + 1
        clock[0] += 1.1
        progress_callback(120.0, 150.0, 42)
        persisted = services._test_writes[-1][1]
        assert persisted['transcription_processed_sec'] == 120.0
        assert persisted['transcription_duration_sec'] == 150.0
        assert persisted['cost_usd'] == 0.02
        assert persisted['reserved_cost_usd'] == 0.06
        assert persisted['cost_limit_usd'] == 1.0
        raise RuntimeError('after progress')

    services.transcribe_audio = transcribe
    _run_pending(services)
    job = services._FINAL_REVIEW_JOBS[job_id]
    assert job['status'] == 'failed'
    assert job['cost_usd'] == 0.02
    assert job['reserved_cost_usd'] == 0.06
    _assert_released(services, job_id)


def test_local_preflight_ignores_cloud_rate_configuration(controller, monkeypatch):
    client, services = controller
    monkeypatch.setenv('ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE', 'not-a-rate')
    monkeypatch.setattr('stage2.whisper_runner.available_backends', lambda: ['faster-whisper'])
    payload = _start_payload(backend='faster-whisper')
    response = client.post('/stage2/final-review/estimate', json=payload)
    assert response.status_code == 200
    assert response.get_json()['result'] == {
        'duration': 150.0, 'duration_sec': 150.0, 'planned_chunks': 0,
        'estimated_cost_usd': '0', 'cost_limit_usd': 1.0, 'enforced': False,
    }
    assert client.post('/stage2/final-review/start', json=payload).status_code == 202
    assert services._test_pending[-1].kwargs['backend'] == 'faster-whisper'


def test_worker_start_failure_marks_job_failed_and_cleans_runtime(controller):
    client, services = controller

    class FailingThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError('thread capacity exhausted')

    services.threading.Thread = FailingThread
    response = client.post('/stage2/final-review/start', json=_start_payload())
    assert response.status_code == 500
    job = next(iter(services._FINAL_REVIEW_JOBS.values()))
    assert job['status'] == 'failed'
    assert 'thread capacity' in job['error']
    _assert_released(services, job['id'])


def test_manifest_conflict_still_returns_409_and_releases_project(controller):
    client, services = controller
    services.build_assembly_manifest = lambda **_options: (_ for _ in ()).throw(ValueError('snapshot changed'))
    response = client.post('/stage2/final-review/start', json=_start_payload())
    assert response.status_code == 409
    assert not services._FINAL_REVIEW_ACTIVE_PROJECTS
    assert not services._FINAL_REVIEW_CANCEL_EVENTS


def test_cancel_during_report_generation_prevents_artifact_commit(controller):
    client, services = controller
    response = client.post('/stage2/final-review/start', json=_start_payload())
    job_id = response.get_json()['result']['job_id']
    artifacts = []
    services.save_word_index = lambda path, _index: artifacts.append(path)

    def review(_timeline, _index):
        client.post(f'/stage2/final-review/jobs/{job_id}/cancel')
        return {'corrections': []}

    services.review_rendered_timeline = review
    _run_pending(services)
    assert services._FINAL_REVIEW_JOBS[job_id]['status'] == 'cancelled'
    assert artifacts == []
    assert not any(path == services._test_paths['final_review_report'] for path, _ in services._test_writes)
    _assert_released(services, job_id)


def test_unknown_cloud_usage_stays_pending_in_usage_ledger(controller):
    client, services = controller
    client.post('/stage2/final-review/start', json=_start_payload())
    services.transcribe_audio = lambda *_a, **_k: {
        'backend': 'cloud-audio', 'backend_metadata': {'cost_usd': None, 'reserved_cost_usd': 0.17},
    }
    _run_pending(services)
    assert services._test_usage[-1]['cost_usd'] is None
    job = next(iter(services._FINAL_REVIEW_JOBS.values()))
    assert job['cost_usd'] == 0
    assert job['reserved_cost_usd'] == 0.17


def test_cancel_restored_terminal_checkpoint_is_idempotent(controller):
    client, services = controller
    checkpoint = {'id': 'disk', 'project': 'demo', 'status': 'completed', 'cost_usd': 0.04}
    services._read_json_safe = lambda _path: checkpoint
    response = client.post('/stage2/final-review/jobs/disk/cancel?project=demo')
    assert response.status_code == 200
    assert response.get_json()['result'] == checkpoint
    assert services._test_writes == []


def test_local_failure_does_not_add_provider_usage(controller, monkeypatch):
    client, services = controller
    monkeypatch.setattr('stage2.whisper_runner.available_backends', lambda: ['faster-whisper'])
    client.post('/stage2/final-review/start', json=_start_payload(backend='faster-whisper'))
    services.transcribe_audio = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError('local inference failed'))
    _run_pending(services)
    job = next(iter(services._FINAL_REVIEW_JOBS.values()))
    assert job['status'] == 'failed'
    assert services._test_usage == []
    _assert_released(services, job['id'])


def test_accepted_wav_duration_is_frozen_in_cloud_execution_options(controller):
    client, services = controller
    response = client.post('/stage2/final-review/start', json=_start_payload())
    assert response.status_code == 202
    observed = {}

    def transcribe(_path, **options):
        observed.update(options)
        return {'backend': 'cloud-audio'}

    services.transcribe_audio = transcribe
    _run_pending(services)
    assert observed['cloud_options']['duration_sec'] == 150.0


def test_stale_confirmation_detects_rate_change_below_float_precision(controller, monkeypatch):
    client, services = controller
    monkeypatch.setenv('ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE', '0.060000000000000001')
    first = client.post('/stage2/final-review/estimate', json=_start_payload())
    assert first.status_code == 200
    confirmed = first.get_json()['result']['estimated_cost_usd']
    monkeypatch.setenv('ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE', '0.060000000000000002')
    response = client.post('/stage2/final-review/start', json=_start_payload(confirmed_estimate_usd=confirmed))
    assert response.status_code == 409
    assert services._FINAL_REVIEW_JOBS == {}


def test_estimate_and_jobs_keep_canonical_decimal_strings(controller, monkeypatch):
    client, services = controller
    monkeypatch.setenv('ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE', '0.060000000000000001')
    result = client.post('/stage2/final-review/estimate', json=_start_payload()).get_json()['result']
    assert result['estimated_cost_usd'] == '0.1700000000000000028333333333'
    response = client.post('/stage2/final-review/start', json=_start_payload(
        confirmed_estimate_usd='0.1700000000000000028333333333',
    ))
    assert response.status_code == 202
    job = services._FINAL_REVIEW_JOBS[response.get_json()['result']['job_id']]
    assert job['estimated_cost_usd'] == '0.1700000000000000028333333333'


@pytest.mark.parametrize('phase', ['words', 'report'])
def test_deadline_during_artifact_write_never_completes(controller, monkeypatch, phase):
    client, services = controller
    clock = [100.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    response = client.post('/stage2/final-review/start', json=_start_payload())
    job_id = response.get_json()['result']['job_id']
    paths = services._test_paths
    if phase == 'words':
        services.save_word_index = lambda *_args: clock.__setitem__(0, 111.0)
    else:
        original_write = services._atomic_write_json_file

        def write(path, payload):
            original_write(path, payload)
            if path == paths['final_review_report']:
                clock[0] = 111.0

        services._atomic_write_json_file = write
    _run_pending(services)
    job = services._FINAL_REVIEW_JOBS[job_id]
    assert job['status'] == 'failed'
    assert 'deadline' in job['error'].lower()
    if phase == 'words':
        assert not any(path == paths['final_review_report'] for path, _ in services._test_writes)
    assert not any(payload.get('status') == 'completed' for _path, payload in services._test_writes)
    _assert_released(services, job_id)


def test_cancel_during_word_artifact_write_stops_before_report(controller):
    client, services = controller
    response = client.post('/stage2/final-review/start', json=_start_payload())
    job_id = response.get_json()['result']['job_id']
    services.save_word_index = lambda *_args: services._FINAL_REVIEW_CANCEL_EVENTS[job_id].set()
    _run_pending(services)
    assert services._FINAL_REVIEW_JOBS[job_id]['status'] == 'cancelled'
    assert not any(path == services._test_paths['final_review_report'] for path, _ in services._test_writes)
    _assert_released(services, job_id)


@pytest.mark.parametrize('spent,reserved,stop', [
    (None, 0.17, 'completed'),
    (0.03, 0.06, 'completed'),
    (0.03, 0.06, 'after-result'),
    (0.03, 0.06, 'cancelled'),
])
def test_real_usage_ledger_keeps_reserved_exposure_pending_once(controller, spent, reserved, stop):
    import json
    from decimal import Decimal
    from openrouter_usage import make_usage_callback, usage_summary

    client, services = controller
    ledger = services._test_paths['work'] / 'usage.json'
    services._make_openrouter_usage_callback = lambda _project, **context: make_usage_callback(ledger, **context)
    response = client.post('/stage2/final-review/start', json=_start_payload())
    job_id = response.get_json()['result']['job_id']

    def transcribe(*_args, **_options):
        if stop == 'cancelled':
            error = WhisperCancelledError('stopped with a reservation')
            error.cost_usd, error.reserved_cost_usd, error.cost_limit_usd = spent, reserved, 1.0
            raise error
        return {'backend': 'cloud-audio', 'backend_metadata': {
            'cost_usd': spent, 'reserved_cost_usd': reserved, 'cost_limit_usd': 1.0,
        }}

    services.transcribe_audio = transcribe
    if stop == 'after-result':
        services.review_rendered_timeline = lambda *_args: (_ for _ in ()).throw(RuntimeError('report failed'))
    _run_pending(services)
    summary = usage_summary(ledger)
    assert summary['pending_count'] == 1
    assert summary['total_cost_usd'] == (spent or 0)
    data = json.loads(ledger.read_text())
    pending = [event for event in data['events'] if event['status'] == 'pending']
    assert len(pending) == 1
    assert pending[0]['event_id'] == f'final-review-asr:{job_id}:reserved'
    assert pending[0]['cost_usd'] is None
    confirmed = [event for event in data['events'] if event['status'] == 'confirmed']
    if spent:
        assert len(confirmed) == 1
        assert confirmed[0]['event_id'] == f'final-review-asr:{job_id}'
        assert Decimal(confirmed[0]['cost_usd']) == Decimal('0.03')
    else:
        assert confirmed == []
    _assert_released(services, job_id)


def test_real_local_process_error_preserves_limit_without_provider_usage(controller, monkeypatch):
    import multiprocessing
    from openrouter_usage import make_usage_callback, usage_summary
    from stage2.whisper_process import WhisperProcessError, transcribe_audio_in_process

    if 'fork' not in multiprocessing.get_all_start_methods():
        pytest.skip('process-shaped local error regression requires fork')
    client, services = controller
    monkeypatch.setattr('stage2.whisper_runner.available_backends', lambda: ['faster-whisper'])
    ledger = services._test_paths['work'] / 'usage.json'
    services._make_openrouter_usage_callback = lambda _project, **context: make_usage_callback(ledger, **context)
    response = client.post('/stage2/final-review/start', json=_start_payload(backend='faster-whisper'))
    job_id = response.get_json()['result']['job_id']
    process_errors = []

    def transcribe(path, **options):
        try:
            return transcribe_audio_in_process(
                path.with_name('missing.wav'), timeout_seconds=2, _start_method='fork', **options,
            )
        except WhisperProcessError as error:
            process_errors.append(error)
            raise

    services.transcribe_audio = transcribe
    _run_pending(services)
    assert len(process_errors) == 1
    assert process_errors[0].cost_usd == 0
    assert process_errors[0].reserved_cost_usd == 0
    assert process_errors[0].cost_limit_usd == 0
    job = services._FINAL_REVIEW_JOBS[job_id]
    assert job['status'] == 'failed'
    assert job['cost_limit_usd'] == 1.0
    assert job['cost_usd'] == 0
    assert job['reserved_cost_usd'] == 0
    assert not ledger.exists()
    assert usage_summary(ledger, create=False)['pending_count'] == 0
    _assert_released(services, job_id)
