from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import threading

import pytest

import api


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
    # Authentication is intentionally bypassed in tests unless API_TOKEN is set.
    monkeypatch.delitem(api.app.config, "API_TOKEN", raising=False)
    return api.app.test_client()


def test_public_route_and_http_method_contract() -> None:
    expected = {
        "/": {"GET"},
        "/health": {"GET"},
        "/project/<project>": {"GET"},
        "/projects": {"GET", "POST"},
        "/projects/<project>": {"DELETE"},
        "/speaker/<project>/<path:filename>": {"GET"},
        "/stage1/analyze": {"POST"},
        "/stage1/normalization": {"GET", "PUT"},
        "/stage1/normalization-jobs/<job_id>": {"GET"},
        "/stage1/normalization-jobs/start": {"POST"},
        "/stage1/normalization-jobs/status": {"GET"},
        "/stage1/normalization/confirm": {"POST"},
        "/stage1/normalization/confirm/preview": {"POST"},
        "/stage1/normalization/generate": {"POST"},
        "/stage1/normalization/history": {"GET"},
        "/stage1/normalization/history/<name>/diff": {"GET"},
        "/stage1/normalization/history/<name>/restore": {"POST"},
        "/stage1/normalization/memory": {"GET", "POST"},
        "/stage1/normalization/memory/<int:rule_id>": {"DELETE", "PATCH"},
        "/stage1/normalization/reset": {"POST"},
        "/stage1/openrouter-usage": {"GET"},
        "/stage1/status": {"GET"},
        "/stage1/tts": {"POST"},
        "/stage1/tts/clear": {"POST"},
        "/stage1/tts-jobs/<job_id>": {"GET"},
        "/stage1/tts-jobs/<job_id>/cancel": {"POST"},
        "/stage1/tts-jobs/start": {"POST"},
        "/stage1/tts-jobs/status": {"GET"},
        "/stage1/tts-phrase": {"POST"},
        "/stage1/validate": {"POST"},
        "/stage2/build": {"POST"},
        "/stage2/export/xml": {"GET"},
        "/stage2/final-review/start": {"POST"},
        "/stage2/final-review/estimate": {"POST"},
        "/stage2/final-review/latest": {"GET"},
        "/stage2/final-review/jobs/<job_id>": {"GET"},
        "/stage2/final-review/jobs/<job_id>/cancel": {"POST"},
        "/stage2/final-review/jobs/<job_id>/apply": {"POST"},
        "/stage2/sync-jobs/<job_id>": {"GET"},
        "/stage2/sync-jobs/<job_id>/cancel": {"POST"},
        "/stage2/sync-jobs/start": {"POST"},
        "/stage2/sync-jobs/status": {"GET"},
        "/stage2/sync-matches": {"GET", "PATCH"},
        "/stage2/sync-matches/<block_uid>": {"PATCH"},
        "/stage2/sync-sources": {"DELETE", "GET", "POST"},
        "/stage2/sync-sources/<source_id>/audio": {"GET", "PUT"},
        "/stage2/sync-sources/<source_id>/words": {"PUT"},
        "/stage2/timeline": {"GET"},
        "/stage3/media/scan": {"POST"},
        "/stage3/timeline/prepare": {"POST"},
        "/stage3/jobs/scan/start": {"POST"},
        "/stage3/jobs/prepare/start": {"POST"},
        "/stage3/jobs/<job_id>": {"GET"},
        "/stage3/jobs/status": {"GET"},
        "/stage3/export/xml": {"GET"},
        "/stage3/plan": {"GET"},
        "/stage3/builds": {"GET"},
        "/stage4/revisions": {"GET"},
        "/stage4/inbox": {"GET"},
        "/stage4/revisions/import": {"POST"},
        "/stage4/revisions/<revision_id>/activate": {"POST"},
        "/stage4/revisions/<revision_id>/reconcile": {"POST"},
        "/stage4/diffs/<diff_id>": {"GET"},
        "/system/readiness": {"GET"},
    }
    actual: dict[str, set[str]] = defaultdict(set)
    for rule in api.app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        actual[rule.rule].update(rule.methods - {"HEAD", "OPTIONS"})

    assert dict(actual) == expected


def test_extracted_views_keep_legacy_api_module_exports() -> None:
    for endpoint in (
        "health",
        "project_home",
        "stage1_analyze",
        "stage1_normalization_generate",
        "stage1_tts_job_start",
        "stage1_validate",
    ):
        assert getattr(api, endpoint) is api.app.view_functions[endpoint]


def test_health_response_contract(client) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.is_json
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["status"] == "healthy"
    assert isinstance(payload["configuration_warnings"], list)
    assert set(payload) == {"ok", "status", "configuration_warnings"}


def test_project_collection_response_contract(client) -> None:
    assert client.get("/projects").get_json() == {"ok": True, "projects": []}

    created = client.post("/projects", json={"project": "Монтаж_01"})
    assert created.status_code == 200
    assert created.get_json() == {
        "ok": True,
        "project": "Монтаж_01",
        "url": "/project/Монтаж_01",
    }
    assert client.get("/projects").get_json() == {
        "ok": True,
        "projects": ["Монтаж_01"],
    }

    deleted = client.delete("/projects/Монтаж_01")
    assert deleted.status_code == 200
    assert deleted.get_json() == {"ok": True, "project": "Монтаж_01"}


def test_project_delete_is_rejected_while_a_job_mutates_it(client) -> None:
    api._ensure_project("busy")
    with api._NORMALIZATION_JOBS_LOCK:
        api._NORMALIZATION_ACTIVE_PROJECTS.add("busy")
    try:
        response = client.delete("/projects/busy")
    finally:
        with api._NORMALIZATION_JOBS_LOCK:
            api._NORMALIZATION_ACTIVE_PROJECTS.discard("busy")

    assert response.status_code == 409
    assert (api.PROJECTS_DIR / "busy").is_dir()


def test_empty_stage_responses_keep_public_envelopes(client) -> None:
    status = client.get("/stage1/status?project=demo")
    assert status.status_code == 200
    status_payload = status.get_json()
    assert status_payload["ok"] is True
    assert status_payload["result"]["project"] == "demo"
    assert set(status_payload) == {"ok", "result"}
    assert status_payload["result"]["openrouter_usage"]["total_cost_usd"] == 0.0

    usage = client.get("/stage1/openrouter-usage?project=demo")
    assert usage.status_code == 200
    assert usage.get_json()["result"]["pending_count"] == 0

    sources = client.get("/stage2/sync-sources?project=demo")
    assert sources.status_code == 200
    assert sources.get_json() == {
        "ok": True,
        "result": {"project": "demo", "sources": []},
    }
    matches = client.get("/stage2/sync-matches?project=demo")
    assert matches.status_code == 200
    assert matches.get_json() == {
        "ok": True,
        "result": {
            "project": "demo",
            "matches": [],
            "auto_matched": 0,
            "confirmed": 0,
            "needs_review": 0,
            "not_found": 0,
            "matched": 0,
            "review": 0,
            "unmatched": 0,
            "no_source": 0,
        },
    }


def test_legacy_stage1_analyze_only_writes_canonical_outputs(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = api._ensure_project("demo")
    source = paths["input"] / "scenario.txt"
    source.write_text("ДИКТОР: Текст.", encoding="utf-8")
    captured: dict[str, Path] = {}

    def fake_analyze(*, input_path: Path, output_path: Path, raw_output_path: Path, **_):
        captured.update(
            input_path=input_path,
            output_path=output_path,
            raw_output_path=raw_output_path,
        )
        return {"blocks_count": 1}

    monkeypatch.setattr(api, "analyze_script", fake_analyze)
    response = client.post(
        "/stage1/analyze",
        json={
            "project": "demo",
            "input_path": str(source),
            "output_path": str(paths["prepared"]),
            "raw_output_path": str(paths["raw"]),
            "use_llm": False,
        },
    )

    assert response.status_code == 200
    assert captured["output_path"] == paths["prepared"]
    assert captured["raw_output_path"] == paths["raw"]


@pytest.mark.parametrize(
    ("field", "filename"),
    [
        ("output_path", "full_timeline.json"),
        ("raw_output_path", "alternate_raw.txt"),
    ],
)
def test_legacy_stage1_analyze_rejects_noncanonical_output_paths(
    client, field: str, filename: str
) -> None:
    paths = api._ensure_project("demo")
    source = paths["input"] / "scenario.txt"
    source.write_text("ДИКТОР: Текст.", encoding="utf-8")
    target = paths["work"] / filename
    target.write_text("sentinel", encoding="utf-8")

    response = client.post(
        "/stage1/analyze",
        json={"project": "demo", "input_path": str(source), field: str(target)},
    )

    assert response.status_code == 400
    assert "canonical" in response.get_json()["error"]
    assert target.read_text(encoding="utf-8") == "sentinel"


def test_legacy_stage1_analyze_is_blocked_by_active_project_mutation(client) -> None:
    with api._TTS_JOBS_LOCK:
        api._TTS_ACTIVE_PROJECTS.add("demo")
    try:
        response = client.post("/stage1/analyze", json={"project": "demo"})
    finally:
        with api._TTS_JOBS_LOCK:
            api._TTS_ACTIVE_PROJECTS.discard("demo")

    assert response.status_code == 409
    assert response.get_json()["error"] == "another mutating job is active for this project"


def test_stage4_inbox_lists_friendly_scenario_and_review_files(client, tmp_path: Path) -> None:
    assert client.post("/projects", json={"project": "demo"}).status_code == 200
    project = tmp_path / "projects" / "demo" / "ЗАГРУЗИТЬ"
    (project / "Сценарии" / "Редакция 2.docx").write_bytes(b"scenario-v2")
    (project / "Просмотровки" / "Интервью.wav").write_bytes(b"audio")

    response = client.get("/stage4/inbox?project=demo")

    assert response.status_code == 200
    result = response.get_json()["result"]
    assert [item["name"] for item in result["scenario_files"]] == ["Редакция 2.docx"]
    assert [item["name"] for item in result["review_files"]] == ["Интервью.wav"]


def test_stage4_activation_is_blocked_while_project_tts_is_running(client) -> None:
    with api._TTS_JOBS_LOCK:
        api._TTS_ACTIVE_PROJECTS.add("demo")
    try:
        response = client.post(
            "/stage4/revisions/rev_0001/activate",
            json={"project": "demo"},
        )
    finally:
        with api._TTS_JOBS_LOCK:
            api._TTS_ACTIVE_PROJECTS.discard("demo")

    assert response.status_code == 409
    assert "озвучки" in response.get_json()["error"]


def _job_registry(job_label: str) -> api.ActiveJobRegistry:
    for registry in api.iter_job_registries():
        if registry.label == job_label:
            return registry
    raise AssertionError(f"no job registry registered under label {job_label!r}")


@pytest.mark.parametrize(
    "job_label",
    ["нормализации", "сопоставления СНХ", "подготовки Stage 3"],
)
def test_stage4_activation_is_blocked_by_other_mutating_jobs(client, job_label: str) -> None:
    registry = _job_registry(job_label)
    with registry.lock:
        registry.active_projects.add("demo")
    try:
        response = client.post(
            "/stage4/revisions/rev_0001/activate",
            json={"project": "demo"},
        )
    finally:
        with registry.lock:
            registry.active_projects.discard("demo")

    assert response.status_code == 409
    assert job_label in response.get_json()["error"]


def test_job_registries_are_visible_from_api_and_stage4_routes_in_priority_order(
    client,
) -> None:
    # api.py and api_stage4_routes.py must observe the exact same registry
    # objects (and priority order) after Stage 3 routes have registered
    # themselves during api.py's module-level setup.
    import api_stage4_routes

    labels = [registry.label for registry in api.iter_job_registries()]
    assert labels == [
        "озвучки",
        "нормализации",
        "сопоставления СНХ",
        "финальной проверки",
        "подготовки Stage 3",
    ]

    # api_stage4_routes._active_project_job reads registries off the passed-in
    # ``services`` module (here, ``api``) rather than any private module state
    # of its own, so this exercises the same cross-module visibility.
    registry = _job_registry("подготовки Stage 3")
    with registry.lock:
        registry.active_projects.add("demo")
    try:
        assert api_stage4_routes._active_project_job(api, "demo") == "подготовки Stage 3"
    finally:
        with registry.lock:
            registry.active_projects.discard("demo")


def test_register_job_registry_replaces_existing_label_without_duplicating() -> None:
    original_labels = [registry.label for registry in api.iter_job_registries()]

    replacement_lock = threading.RLock()
    replacement_active: set[str] = set()
    api.register_job_registry("нормализации", replacement_lock, replacement_active)
    try:
        labels = [registry.label for registry in api.iter_job_registries()]
        assert labels == original_labels
        assert labels.count("нормализации") == 1

        registry = _job_registry("нормализации")
        assert registry.lock is replacement_lock
        assert registry.active_projects is replacement_active
    finally:
        api.register_job_registry(
            "нормализации",
            api._NORMALIZATION_JOBS_LOCK,
            api._NORMALIZATION_ACTIVE_PROJECTS,
        )

    assert _job_registry("нормализации").lock is api._NORMALIZATION_JOBS_LOCK


def test_stage4_mismatched_work_state_is_reported_and_activation_is_blocked(
    client,
) -> None:
    paths = api._ensure_project("demo")
    (paths["scenario_versions"] / "rev_0001.json").write_text(
        json.dumps({"revision_id": "rev_0001"}), encoding="utf-8"
    )
    (paths["work"] / "active_revision.json").write_text(
        json.dumps({"revision_id": "rev_0001"}), encoding="utf-8"
    )
    paths["normalized"].write_text(
        json.dumps({"scenario_revision_id": "rev_0002", "blocks": []}),
        encoding="utf-8",
    )

    listed = client.get("/stage4/revisions?project=demo")
    activated = client.post("/stage4/revisions/rev_0001/activate", json={"project": "demo"})

    integrity = listed.get_json()["result"]["integrity"]
    assert integrity["status"] == "active_work_mismatch"
    assert integrity["blocking"] is True
    assert activated.status_code == 409


@pytest.mark.parametrize(
    ("url", "error"),
    [
        ("/stage1/normalization-jobs/missing", "normalization job not found"),
        ("/stage1/tts-jobs/missing", "job not found"),
        ("/stage2/sync-jobs/missing", "job not found"),
    ],
)
def test_unknown_job_error_contract(client, url: str, error: str) -> None:
    response = client.get(url)

    assert response.status_code == 404
    assert response.get_json() == {"ok": False, "error": error}


def test_missing_timeline_error_contract(client) -> None:
    response = client.get("/stage2/timeline?project=demo")

    assert response.status_code == 404
    assert response.get_json() == {
        "ok": False,
        "error": "full_timeline.json not found — run Build first",
    }


def _load_route_generator():
    import importlib.machinery
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "gen-api-routes"
    loader = importlib.machinery.SourceFileLoader("gen_api_routes", str(path))
    spec = importlib.util.spec_from_loader("gen_api_routes", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_readme_api_routes_block_matches_generator() -> None:
    generator = _load_route_generator()
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")

    assert generator.BEGIN in readme and generator.END in readme
    start = readme.index(generator.BEGIN)
    end = readme.index(generator.END) + len(generator.END)
    assert readme[start:end] == generator.render_block(), (
        "README.md API routes block drifted; run scripts/gen-api-routes --write"
    )
    assert generator.main(["--check"]) == 0


def test_route_generator_lists_every_route_sorted() -> None:
    generator = _load_route_generator()
    table = generator.render_table()
    paths = [line.split("|")[2].strip().strip("`") for line in table.splitlines()[2:]]

    assert paths == sorted(paths)
    expected = {rule.rule for rule in api.app.url_map.iter_rules() if rule.endpoint != "static"}
    assert set(paths) == expected
    assert "/static/<path:filename>" not in paths
