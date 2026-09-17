from pathlib import Path
from types import SimpleNamespace
import threading
import uuid

from flask import Flask
import pytest

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
