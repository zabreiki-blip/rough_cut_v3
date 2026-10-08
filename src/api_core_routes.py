from __future__ import annotations

import json
from pathlib import Path
import shutil
from types import ModuleType
from typing import Any

from flask import Flask, abort, jsonify, render_template, request, send_from_directory

from stage2.cloud_asr import CloudAudioAsrAdapter
from stage2.whisper_runner import available_backends

_READINESS_SEVERITY_ORDER = {"error": 0, "warn": 1, "ok": 2}


def _readiness_item(
    *, item_id: str, label: str, status: str, detail: str, hint: str = ""
) -> dict[str, str]:
    return {"id": item_id, "label": label, "status": status, "detail": detail, "hint": hint}


def _check_ffmpeg() -> dict[str, str]:
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    if ffmpeg_path and ffprobe_path:
        return _readiness_item(
            item_id="ffmpeg",
            label="FFmpeg / FFprobe",
            status="ok",
            detail="ffmpeg и ffprobe найдены в PATH.",
        )
    missing = [
        name for name, path in (("ffmpeg", ffmpeg_path), ("ffprobe", ffprobe_path)) if not path
    ]
    return _readiness_item(
        item_id="ffmpeg",
        label="FFmpeg / FFprobe",
        status="error",
        detail=f"Не найдено: {', '.join(missing)}.",
        hint="Установите: brew install ffmpeg",
    )


def _check_whisper_backend() -> dict[str, str]:
    local_backends = available_backends()
    # Переиспользуем CloudAudioAsrAdapter.is_available() (stage2/cloud_asr.py) —
    # не дублируем чтение SYNC_CLOUD_ASR_API_KEY/OPENROUTER_API_KEY/
    # OPENROUTER_BASE_URL здесь. Читается только признак доступности,
    # значение ключа наружу никогда не попадает.
    cloud_available = CloudAudioAsrAdapter().is_available()
    if local_backends and cloud_available:
        return _readiness_item(
            item_id="whisper_backend",
            label="Whisper-бэкенд (Stage 2)",
            status="ok",
            detail=(
                f"Локальный бэкенд установлен: {', '.join(local_backends)} "
                "(используется по умолчанию); также настроен облачный ASR (cloud-audio)."
            ),
        )
    if local_backends:
        return _readiness_item(
            item_id="whisper_backend",
            label="Whisper-бэкенд (Stage 2)",
            status="ok",
            detail=f"Установлен локальный бэкенд: {', '.join(local_backends)}.",
        )
    if cloud_available:
        return _readiness_item(
            item_id="whisper_backend",
            label="Whisper-бэкенд (Stage 2)",
            status="ok",
            detail=(
                "Локальный бэкенд не установлен, но настроен облачный ASR (cloud-audio); "
                "задайте SYNC_WHISPER_BACKEND=cloud, чтобы использовать его."
            ),
        )
    return _readiness_item(
        item_id="whisper_backend",
        label="Whisper-бэкенд (Stage 2)",
        status="warn",
        detail=(
            "Нет ни локального бэкенда (faster-whisper/mlx-whisper), "
            "ни настроенного облачного ASR; синхронизация Stage 2 недоступна."
        ),
        hint=(
            "Установите локальный бэкенд: pip install -r requirements-whisper.txt "
            "(на Apple Silicon — requirements-macos-apple-silicon.txt); "
            "или настройте облачный ASR: задайте SYNC_CLOUD_ASR_API_KEY "
            "(либо OPENROUTER_API_KEY) и SYNC_WHISPER_BACKEND=cloud."
        ),
    )


def _macos_connector_health_url(connector_url: str) -> str:
    """Derive the connector's `/health` URL from its configured `/synthesize` URL."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(connector_url)
    return urlunsplit((parts.scheme, parts.netloc, "/health", "", ""))


def _redact_url_userinfo(url: str) -> str:
    """Strip any embedded credentials (userinfo) from a URL for safe display.

    Only scheme, host, port and path are kept; login/password never leave this
    function. The original URL (with userinfo, if any) must still be used for
    the actual HTTP request.
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    netloc = parts.hostname or ""
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _check_macos_connector(connector_url: str) -> dict[str, str]:
    """Check TTS readiness via the macOS connector bridge (Docker/Linux API host).

    The `/health` endpoint of the connector requires no authentication (see
    `src/macos_connector.py`), so no token is ever sent or logged here.
    """
    label = "TTS-провайдер"
    health_url = _macos_connector_health_url(connector_url)
    display_url = _redact_url_userinfo(health_url)
    unreachable = _readiness_item(
        item_id="tts_provider",
        label=label,
        status="error",
        detail=f"Провайдер macos: коннектор не отвечает ({display_url}).",
        hint=(
            "Запустите коннектор на macOS-хосте: python -m macos_connector; "
            "проверьте MACOS_CONNECTOR_URL."
        ),
    )

    try:
        import requests
    except ImportError:
        return _readiness_item(
            item_id="tts_provider",
            label=label,
            status="error",
            detail="Провайдер macos: библиотека requests не установлена.",
            hint="Установите зависимости: pip install -r requirements.txt",
        )

    try:
        response = requests.get(health_url, timeout=2)
    except requests.RequestException:
        return unreachable
    if response.status_code != 200:
        return unreachable

    try:
        payload = response.json()
    except ValueError:
        payload = {}

    say_available = bool(payload.get("say_available"))
    afconvert_available = bool(payload.get("afconvert_available"))
    if say_available and afconvert_available:
        return _readiness_item(
            item_id="tts_provider",
            label=label,
            status="ok",
            detail=(
                "Провайдер macos: синтез выполняется через коннектор на хосте, "
                "say и afconvert доступны."
            ),
        )
    missing = [
        name
        for name, available in (("say", say_available), ("afconvert", afconvert_available))
        if not available
    ]
    return _readiness_item(
        item_id="tts_provider",
        label=label,
        status="error",
        detail=f"Провайдер macos: на хосте коннектора не найдено {', '.join(missing)}.",
        hint="Установите недостающие утилиты на macOS-хосте коннектора.",
    )


def _check_tts_provider(settings: Any) -> dict[str, str]:
    provider = str(getattr(settings, "tts_provider", "") or "")
    label = "TTS-провайдер"
    if provider == "macos":
        connector_url = str(getattr(settings, "macos_connector_url", "") or "")
        if connector_url:
            return _check_macos_connector(connector_url)
        say_path = shutil.which("say")
        afconvert_path = shutil.which("afconvert")
        if say_path and afconvert_path:
            return _readiness_item(
                item_id="tts_provider",
                label=label,
                status="ok",
                detail="Провайдер macos: команды say и afconvert найдены.",
            )
        missing = [
            name for name, path in (("say", say_path), ("afconvert", afconvert_path)) if not path
        ]
        return _readiness_item(
            item_id="tts_provider",
            label=label,
            status="error",
            detail=f"Провайдер macos: не найдено {', '.join(missing)}.",
            hint="Доступно только на macOS со стандартными системными утилитами.",
        )
    if provider == "openrouter":
        if settings.openrouter_api_key:
            return _readiness_item(
                item_id="tts_provider",
                label=label,
                status="ok",
                detail="Провайдер openrouter: ключ задан.",
            )
        return _readiness_item(
            item_id="tts_provider",
            label=label,
            status="warn",
            detail="Провайдер openrouter: OPENROUTER_API_KEY не задан.",
            hint="Задайте OPENROUTER_API_KEY в .env или переключите TTS_PROVIDER.",
        )
    return _readiness_item(
        item_id="tts_provider",
        label=label,
        status="warn",
        detail=f"Неизвестный провайдер: {provider!r}.",
        hint="TTS_PROVIDER должен быть openrouter или macos.",
    )


def _check_api_key(*, item_id: str, label: str, has_key: bool, hint: str) -> dict[str, str]:
    if has_key:
        return _readiness_item(item_id=item_id, label=label, status="ok", detail="Ключ задан.")
    return _readiness_item(
        item_id=item_id, label=label, status="warn", detail="Ключ не задан.", hint=hint
    )


def _build_readiness(settings: Any) -> dict[str, Any]:
    # Ключи и их значения никогда не возвращаются клиенту — только факт наличия.
    checks: list[dict[str, str]] = [
        _check_ffmpeg(),
        _check_whisper_backend(),
        _check_tts_provider(settings),
        _check_api_key(
            item_id="openrouter_key",
            label="OPENROUTER_API_KEY",
            has_key=bool(settings.openrouter_api_key),
            hint="Нужен для LLM-анализа сценария и OpenRouter TTS.",
        ),
    ]
    overall = min(
        (check["status"] for check in checks),
        key=lambda status: _READINESS_SEVERITY_ORDER.get(status, 1),
        default="ok",
    )
    return {"ok": True, "overall": overall, "checks": checks}


def _active_revision_input_path(paths: dict[str, Path], input_files: list[str]) -> str:
    """Prefer the scenario file owned by the active revision."""

    active_path = paths["work"] / "active_revision.json"
    try:
        active = json.loads(active_path.read_text(encoding="utf-8"))
        revision_id = str(active.get("revision_id") or "")
        manifest_path = paths["scenario_versions"] / f"{revision_id}.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        source_file = Path(str(manifest.get("source_file") or "")).name
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        source_file = ""
    if source_file:
        active_input = next(
            (value for value in input_files if Path(value).name == source_file),
            "",
        )
        if active_input:
            return active_input
    return input_files[0] if input_files else ""


def register_core_routes(app: Flask, services: ModuleType) -> None:
    """Register project-shell routes while resolving services through the API facade."""

    @app.get("/")
    def project_home():
        services.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        return render_template("index.html", projects=services._list_projects())

    @app.get("/projects")
    def list_projects():
        services.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        return jsonify({"ok": True, "projects": services._list_projects()})

    @app.post("/projects")
    def create_project():
        payload = request.get_json(silent=True) or {}
        project_raw = payload.get("project")
        if not project_raw:
            return services._json_error("project is required", 400)

        try:
            project = services._normalize_project_name(str(project_raw))
        except ValueError as exc:
            return services._json_error(str(exc), 400)

        services._ensure_project(project)
        return jsonify({"ok": True, "project": project, "url": f"/project/{project}"})

    @app.delete("/projects/<project>")
    def delete_project(project: str):
        try:
            project = services._normalize_project_name(project)
        except ValueError as exc:
            return services._json_error(str(exc), 400)

        with services._project_transition_lock(project):
            if services._project_has_active_mutation(project):
                return services._json_error(
                    "Нельзя удалить проект, пока выполняется фоновая задача", 409
                )
            project_dir = services.PROJECTS_DIR / project
            if not project_dir.exists():
                return services._json_error("Проект не найден", 404)
            shutil.rmtree(project_dir)
        return jsonify({"ok": True, "project": project})

    @app.get("/project/<project>")
    def project_dashboard(project: str):
        try:
            project = services._normalize_project_name(project)
        except ValueError:
            abort(404)

        settings = services.get_settings()
        paths = services._ensure_project(project)
        status = services._build_status(project)

        input_files = services._discover_input_files(paths["input"], project)
        active_input = _active_revision_input_path(paths, input_files)
        defaults = {
            "project": project,
            "input_path": (active_input or f"projects/{project}/input/scenario.docx"),
            "output_path": f"projects/{project}/work/scenario_prepared.json",
            "raw_output_path": f"projects/{project}/work/scenario_raw.txt",
            "prepared_path": f"projects/{project}/work/scenario_prepared.json",
            "speaker_dir": f"projects/{project}/speaker",
            "manifest_path": f"projects/{project}/speaker/manifest.json",
            "provider": settings.tts_provider,
            "tts_model": settings.openrouter_tts_model,
            "gender": settings.tts_gender,
        }
        return render_template("project.html", defaults=defaults, status=status)

    @app.get("/health")
    def health():
        # /health is public. Warning texts can quote raw env values, so only
        # authenticated callers get them; anonymous callers see variable names.
        warnings = list(services._CONFIG_WARNINGS)
        if not services._request_is_authenticated():
            warnings = services.warning_variable_names(warnings)
        return jsonify(
            {
                "ok": True,
                "status": "healthy",
                "configuration_warnings": warnings,
            }
        )

    @app.get("/system/readiness")
    def system_readiness():
        settings = services.get_settings()
        return jsonify(_build_readiness(settings))

    @app.get("/stage1/status")
    def stage1_status():
        raw_project = request.args.get("project")
        try:
            project = services._resolve_project_name(raw_project)
            services._ensure_project(project)
        except ValueError as exc:
            return services._json_error(str(exc), 400)

        return jsonify({"ok": True, "result": services._build_status(project)})

    @app.get("/speaker/<project>/<path:filename>")
    def speaker_file(project: str, filename: str):
        try:
            project = services._normalize_project_name(project)
        except ValueError:
            abort(404)

        if "/" in filename or filename.startswith(".") or not filename.endswith(".wav"):
            abort(404)

        paths = services._project_paths(project)
        normalized = services._read_json_safe(paths["normalized"])
        active_speaker_dir = services._revision_speaker_dir(
            paths, normalized.get("scenario_revision_id")
        )
        if (active_speaker_dir / filename).is_file():
            return send_from_directory(active_speaker_dir, filename, as_attachment=False)
        # Legacy projects created before revision-local aliases retain their
        # old playback path until a baseline is created.
        return send_from_directory(paths["speaker"], filename, as_attachment=False)
