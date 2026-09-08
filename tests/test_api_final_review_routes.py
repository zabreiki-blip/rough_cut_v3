from pathlib import Path
from types import SimpleNamespace

from flask import Flask
import pytest

from api_final_review_routes import _apply, _start, _wav_duration
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
