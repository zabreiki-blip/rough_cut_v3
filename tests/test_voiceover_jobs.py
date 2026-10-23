from __future__ import annotations

from pathlib import Path
import threading
import time
from types import SimpleNamespace
from typing import Any
import wave

import pytest

from atomic_io import atomic_write_json
import api_voiceover_jobs
from api_voiceover_jobs import (
    autodetect_voiceover,
    cancel_voiceover_job,
    create_voiceover_job,
    local_asr_backend,
    run_voiceover_job,
    trigger_voiceover_autodetect,
)
from job_runtime import JobRegistry
from project_service import ensure_project, read_json_safe
from stage2.whisper_process import WhisperCancelledError
from voiceover.storage import (
    asr_cache_path,
    load_recorded_voiceover,
    register_recording_file,
    save_recorded_voiceover,
)

SENTENCES = [
    "Город просыпается рано утром",
    "Рабочие выходят на площадь",
    "Строители поднимают новый мост",
]


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


def scenario(*texts: str) -> dict:
    return {
        "status": "confirmed",
        "scenario_revision_id": "rev_1",
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


@pytest.fixture(autouse=True)
def clean_asr_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SYNC_WHISPER_BACKEND", raising=False)
    monkeypatch.delenv("SYNC_WHISPER_MODEL", raising=False)


@pytest.fixture
def paths(tmp_path: Path) -> dict[str, Path]:
    return ensure_project("demo", tmp_path / "projects")


def make_services(paths: dict[str, Path], transcribe: Any) -> SimpleNamespace:
    return SimpleNamespace(
        _VOICEOVER_RUNTIME=JobRegistry(),
        _ASR_SEMAPHORE=threading.Semaphore(1),
        _project_paths=lambda project: paths,
        _utc_now_iso=lambda: "2026-10-24T10:00:00+00:00",
        _atomic_write_json_file=atomic_write_json,
        _read_json_safe=read_json_safe,
        transcribe_audio=transcribe,
    )


class FakeAsr:
    def __init__(self, payloads: dict[str, dict[str, Any]] | dict[str, Any]) -> None:
        self.payloads = payloads
        self.calls: list[dict[str, Any]] = []

    def __call__(self, audio_path: Path, **options: Any) -> dict[str, Any]:
        self.calls.append({"audio": Path(audio_path), **options})
        if "words" in self.payloads:
            return self.payloads  # type: ignore[return-value]
        return self.payloads[Path(audio_path).parent.name]  # type: ignore[index]


def register(paths: dict[str, Path], name: str = "take.wav", seconds: float = 60.0) -> Any:
    source = make_wav(paths["inbox_voiceover"] / name, seconds=seconds)
    return register_recording_file(paths, source)


def run_job(services: SimpleNamespace, paths: dict[str, Path], doc: dict) -> dict:
    job = create_voiceover_job(services, "demo", doc)
    run_voiceover_job(services, job_id=job["id"], project="demo", normalization=doc)
    return services._VOICEOVER_RUNTIME.jobs[job["id"]]


def test_job_stores_needs_review_proposals_without_publishing(paths: dict[str, Path]) -> None:
    recording = register(paths)
    asr = FakeAsr(asr_payload(*SENTENCES))
    services = make_services(paths, asr)

    job = run_job(services, paths, scenario(*SENTENCES))

    assert job["status"] == "completed"
    assert job["recording_ids"] == [recording.id]
    assert job["scenario_revision_id"] == "rev_1"
    data = load_recorded_voiceover(paths)
    assert [item["block_uid"] for item in data["ranges"]] == ["b1", "b2", "b3"]
    assert {item["status"] for item in data["ranges"]} == {"needs_review"}
    assert data["scenario_revision_id"] == "rev_1"
    # nothing is published into the active voiceover
    assert not paths["active_voiceover_manifest"].exists()
    assert not list(paths["speaker"].glob("zk*.wav"))


def test_job_uses_local_backend_only_and_no_cloud_options(
    paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNC_WHISPER_BACKEND", "cloud")
    register(paths)
    asr = FakeAsr(asr_payload(*SENTENCES))

    run_job(make_services(paths, asr), paths, scenario(*SENTENCES))

    assert local_asr_backend() == "auto"
    assert asr.calls[0]["backend"] == "auto"
    assert not asr.calls[0].get("cloud_options")
    assert not asr.calls[0].get("budget_callback")


def test_asr_cache_is_reused_and_independent_of_scenario_text(paths: dict[str, Path]) -> None:
    recording = register(paths)
    asr = FakeAsr(asr_payload(*SENTENCES))
    services = make_services(paths, asr)

    run_job(services, paths, scenario(*SENTENCES))
    second = run_job(services, paths, scenario(SENTENCES[0], "Совсем другой новый текст блока"))

    assert len(asr.calls) == 1
    assert second["status"] == "completed"
    cache_dir = paths["recorded_voiceover_asr"]
    assert len(list(cache_dir.glob("*.json"))) == 1
    assert asr_cache_path(
        paths, recording.sha256, {"backend": "faster-whisper", "model": "", "language": "ru"}
    ).exists()  # the engine that really ran, not the requested "auto"
    ranges = {item["block_uid"]: item for item in load_recorded_voiceover(paths)["ranges"]}
    assert ranges["b1"]["status"] == "needs_review"
    assert ranges["b2"]["status"] == "missing"


def test_empty_asr_result_marks_every_block_missing(paths: dict[str, Path]) -> None:
    register(paths)
    services = make_services(paths, FakeAsr({"words": [], "language": "ru"}))

    job = run_job(services, paths, scenario(*SENTENCES))

    assert job["status"] == "completed"
    assert {item["status"] for item in load_recorded_voiceover(paths)["ranges"]} == {"missing"}


def test_confirmed_manual_range_is_kept_when_text_is_unchanged(paths: dict[str, Path]) -> None:
    recording = register(paths)
    doc = scenario(*SENTENCES)
    services = make_services(paths, FakeAsr(asr_payload(*SENTENCES)))
    run_job(services, paths, doc)
    data = load_recorded_voiceover(paths)
    first = data["ranges"][0]
    first.update(
        status="confirmed",
        origin="manual",
        manual_range=True,
        source_in_sec=1.0,
        source_out_sec=9.0,
    )
    save_recorded_voiceover(paths, data)

    run_job(services, paths, doc)

    kept = load_recorded_voiceover(paths)["ranges"][0]
    assert kept["status"] == "confirmed" and kept["source_in_sec"] == 1.0
    assert kept["recording_id"] == recording.id


def test_two_parts_are_aligned_in_name_order(paths: dict[str, Path]) -> None:
    first = register(paths, "Часть 1.wav", 30.0)
    make_wav(paths["inbox_voiceover"] / "Часть 2.wav", seconds=31.0)
    second = register_recording_file(paths, paths["inbox_voiceover"] / "Часть 2.wav")
    payloads = {
        Path(first.asset).parent.name: asr_payload(SENTENCES[0], SENTENCES[1]),
        Path(second.asset).parent.name: asr_payload(SENTENCES[2]),
    }
    asr = FakeAsr(payloads)

    run_job(make_services(paths, asr), paths, scenario(*SENTENCES))

    ranges = load_recorded_voiceover(paths)["ranges"]
    assert [item["recording_id"] for item in ranges] == [first.id, first.id, second.id]


def test_cancel_while_waiting_for_asr_slot_never_calls_transcribe(paths: dict[str, Path]) -> None:
    register(paths)
    asr = FakeAsr(asr_payload(*SENTENCES))
    services = make_services(paths, asr)
    doc = scenario(*SENTENCES)
    job = create_voiceover_job(services, "demo", doc)
    services._ASR_SEMAPHORE.acquire()  # another job owns the only ASR slot
    worker = threading.Thread(
        target=run_voiceover_job,
        kwargs={"services": services, "job_id": job["id"], "project": "demo", "normalization": doc},
    )
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if services._VOICEOVER_RUNTIME.jobs[job["id"]].get("asr_waiting"):
                break
            time.sleep(0.02)
        assert services._VOICEOVER_RUNTIME.jobs[job["id"]].get("asr_waiting") is True
        snapshot, error = cancel_voiceover_job(services, job["id"])
        assert error is None and snapshot["status"] == "cancelling"
        worker.join(5)
        assert not worker.is_alive()
    finally:
        services._ASR_SEMAPHORE.release()
    assert services._VOICEOVER_RUNTIME.jobs[job["id"]]["status"] == "cancelled"
    assert asr.calls == []
    assert load_recorded_voiceover(paths)["ranges"] == []
    assert services._ASR_SEMAPHORE.acquire(timeout=0.5)  # the slot was not leaked


def test_cancel_during_recognition_stops_without_writing_proposals(paths: dict[str, Path]) -> None:
    register(paths)
    started = threading.Event()

    def blocking(audio_path: Path, **options: Any) -> dict[str, Any]:
        started.set()
        event = options["cancel_event"]
        assert event.wait(5)
        raise WhisperCancelledError("cancelled")

    services = make_services(paths, blocking)
    doc = scenario(*SENTENCES)
    job = create_voiceover_job(services, "demo", doc)
    worker = threading.Thread(
        target=run_voiceover_job,
        kwargs={"services": services, "job_id": job["id"], "project": "demo", "normalization": doc},
    )
    worker.start()
    assert started.wait(5)
    cancel_voiceover_job(services, job["id"])
    worker.join(5)

    assert not worker.is_alive()
    assert services._VOICEOVER_RUNTIME.jobs[job["id"]]["status"] == "cancelled"
    assert load_recorded_voiceover(paths)["ranges"] == []
    assert not list(paths["recorded_voiceover_asr"].glob("*.json"))
    assert "demo" not in services._VOICEOVER_RUNTIME.active_projects


def test_autodetect_registers_new_file_and_queues_local_job(
    paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNC_WHISPER_BACKEND", "openrouter")
    make_wav(paths["inbox_voiceover"] / "запись.wav")
    asr = FakeAsr(asr_payload(*SENTENCES))
    services = make_services(paths, asr)

    job = trigger_voiceover_autodetect(services, "demo", scenario(*SENTENCES), run_async=False)

    assert job is not None
    finished = services._VOICEOVER_RUNTIME.jobs[job["id"]]
    assert finished["status"] == "completed"
    assert len(load_recorded_voiceover(paths)["recordings"]) == 1
    assert [call["backend"] for call in asr.calls] == ["auto"]  # never a paid backend
    assert all(not call.get("cloud_options") for call in asr.calls)
    again = trigger_voiceover_autodetect(services, "demo", scenario(*SENTENCES), run_async=False)
    assert again is None and len(asr.calls) == 1


def test_autodetect_without_new_file_or_scenario_does_nothing(paths: dict[str, Path]) -> None:
    asr = FakeAsr(asr_payload(*SENTENCES))
    services = make_services(paths, asr)

    assert (
        trigger_voiceover_autodetect(services, "demo", scenario(*SENTENCES), run_async=False)
        is None
    )
    make_wav(paths["inbox_voiceover"] / "запись.wav")
    assert trigger_voiceover_autodetect(services, "demo", {}, run_async=False) is None
    assert asr.calls == []
    # the file stays registered; a later scan does not queue it a second time
    assert (
        trigger_voiceover_autodetect(services, "demo", scenario(*SENTENCES), run_async=False)
        is None
    )
    assert asr.calls == []


def test_asr_cache_key_follows_the_resolved_engine(
    paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    register(paths)
    asr = FakeAsr(asr_payload(*SENTENCES))
    services = make_services(paths, asr)
    engine = {"name": "mlx-whisper"}
    monkeypatch.setattr(api_voiceover_jobs, "resolve_backend", lambda _requested: engine["name"])

    run_job(services, paths, scenario(*SENTENCES))
    run_job(services, paths, scenario(*SENTENCES))
    assert len(asr.calls) == 1  # same engine: cached
    engine["name"] = "faster-whisper"  # "auto" now resolves to another engine
    run_job(services, paths, scenario(*SENTENCES))
    assert len(asr.calls) == 2
    assert len(list(paths["recorded_voiceover_asr"].glob("*.json"))) == 2


def test_new_part_during_a_job_is_picked_up_after_it_finishes(paths: dict[str, Path]) -> None:
    first = register(paths, "часть 1.wav")
    asr = FakeAsr(asr_payload(*SENTENCES))
    services = make_services(paths, asr)
    doc = scenario(*SENTENCES)
    assert api_voiceover_jobs.reserve_voiceover_project(services, "demo")
    job = create_voiceover_job(services, "demo", doc)
    make_wav(paths["inbox_voiceover"] / "часть 2.wav", seconds=70.0)

    busy = autodetect_voiceover(services, "demo", doc, run_async=False)

    assert busy.job is None and busy.pending is True
    assert [r["id"] for r in load_recorded_voiceover(paths)["recordings"]] == [first.id]
    run_voiceover_job(services, job_id=job["id"], project="demo", normalization=doc)

    assert len(load_recorded_voiceover(paths)["recordings"]) == 2
    statuses = sorted(item["status"] for item in services._VOICEOVER_RUNTIME.jobs.values())
    assert statuses == ["completed", "completed"]
    assert "demo" not in services._VOICEOVER_RUNTIME.active_projects


def test_autodetect_requires_a_confirmed_scenario(paths: dict[str, Path]) -> None:
    make_wav(paths["inbox_voiceover"] / "запись.wav")
    asr = FakeAsr(asr_payload(*SENTENCES))
    services = make_services(paths, asr)
    draft = {**scenario(*SENTENCES), "status": "draft"}
    atomic_write_json(paths["normalized"], draft)  # nothing passed explicitly: the file is read

    result = autodetect_voiceover(services, "demo", None, run_async=False)

    assert result.job is None and result.scenario_confirmed is False
    assert len(result.registered) == 1 and asr.calls == []
    assert "demo" not in services._VOICEOVER_RUNTIME.active_projects
