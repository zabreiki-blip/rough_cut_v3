from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading
from types import ModuleType
import uuid

from flask import Flask, jsonify, request, send_from_directory

from atomic_io import atomic_write_json, atomic_write_text
from export_naming import xml_export_filename
from job_runtime import trim_terminal_jobs
from stage3.media_catalog import MEDIA_EXTENSIONS, MediaCatalogError, scan_media_directory
from stage3.nested_xml import build_nested_xml
from stage3.source_mapping import map_catalog_to_timeline, normalize_source_identity
from stage_common import _scenario_revision_matches, _timeline_is_current

_MATCHED_STATUSES = {"", "auto_matched", "confirmed"}
_STAGE3_JOB_KINDS = ("scan", "prepare")
_STAGE3_JOBS_MAX = 200
_STAGE3_JOBS_TTL_SECONDS = 24 * 60 * 60
_STAGE3_TERMINAL_STATUSES = frozenset({"completed", "failed", "interrupted"})


class Stage3OperationError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _host_path(path: str, *, project: str, project_root: Path) -> str:
    """Translate a project-local container path to the macOS checkout path."""

    raw = Path(str(path or ""))
    relative = None
    for root in (project_root, Path("/app/projects") / project):
        try:
            relative = raw.relative_to(root)
            break
        except ValueError:
            continue
    if relative is None:
        try:
            relative = raw.resolve().relative_to(project_root.resolve())
        except (OSError, ValueError):
            return str(path or "")
    configured = os.getenv("ROUGH_CUT_HOST_PROJECTS_ROOT", "").strip()
    host_projects_root = Path(configured) if configured else project_root.parent
    return str(host_projects_root / project / relative)


def _host_catalog(catalog: dict, *, project: str, project_root: Path) -> dict:
    result = deepcopy(catalog)
    for source in result.get("sources", []):
        if isinstance(source, dict):
            source["container_path"] = _host_path(
                str(source.get("container_path") or ""),
                project=project,
                project_root=project_root,
            )
            if source.get("placeholder_path"):
                source["placeholder_path"] = _host_path(
                    str(source["placeholder_path"]),
                    project=project,
                    project_root=project_root,
                )
    return result


def _host_timeline_paths(data: dict, *, project: str, project_root: Path) -> dict:
    result = deepcopy(data)
    for segment in result.get("segments", []):
        if not isinstance(segment, dict):
            continue
        for key in ("wav_path", "placeholder_path"):
            if segment.get(key):
                segment[key] = _host_path(
                    str(segment[key]), project=project, project_root=project_root
                )
    return result


def _placeholder_unmapped_sources(data: dict, mapped_source_ids: set[str]) -> tuple[dict, int]:
    result = deepcopy(data)
    replaced = 0
    for segment in result.get("segments", []):
        if not isinstance(segment, dict):
            continue
        source_id = str(segment.get("source_id") or "")
        matched = (
            segment.get("type") in {"snh", "host"}
            and str(segment.get("match_status") or "") in _MATCHED_STATUSES
            and source_id
            and float(segment.get("source_out_sec") or 0) > float(segment.get("source_in_sec") or 0)
        )
        if not matched or source_id in mapped_source_ids:
            continue
        segment["match_status"] = "not_found"
        segment["source_id"] = ""
        segment["sequence_id"] = ""
        segment["sequence_name"] = ""
        segment["source_in_sec"] = 0.0
        segment["source_out_sec"] = 0.0
        replaced += 1
    return result, replaced


def _mapping_summary(mapping) -> list[dict]:
    result = []
    for source_id, detail in mapping.details.items():
        result.append(
            {
                "source_id": source_id,
                "status": detail.get("status", "unmapped"),
                "reason": detail.get("reason", ""),
                "media_name": detail.get("media_name", ""),
                "media_source_id": detail.get("media_source_id", ""),
                "max_source_out_sec": detail.get("max_source_out_sec", 0.0),
                "source_label": detail.get("source_label", ""),
                "sequence_aliases": detail.get("sequence_aliases", []),
                "speaker_aliases": detail.get("speaker_aliases", []),
                "media_kind": detail.get("media_kind", "video"),
            }
        )
    return result


def _media_kind(item: dict) -> str:
    explicit = str(item.get("media_kind") or "").strip().lower()
    if explicit in {"audio", "video"}:
        return explicit
    return (
        "audio"
        if Path(str(item.get("name") or item.get("relative_path") or "")).suffix.lower()
        in {".mp3", ".wav"}
        else "video"
    )


def _timeline_catalog(catalog: dict) -> dict:
    """Collapse audio companions while retaining standalone audio sources."""

    result = deepcopy(catalog)
    grouped: dict[str, list[dict]] = {}
    for item in result.get("sources", []):
        if not isinstance(item, dict):
            continue
        relative_path = str(item.get("relative_path") or "").strip()
        if not relative_path:
            continue
        key = Path(relative_path).with_suffix("").as_posix().casefold()
        grouped.setdefault(key, []).append(item)
    logical_sources = []
    for items in grouped.values():
        visual = next((item for item in items if _media_kind(item) == "video"), None)
        audio = min(
            items,
            key=lambda item: (
                Path(str(item.get("relative_path") or "")).suffix.lower() != ".wav",
                str(item.get("relative_path") or "").casefold(),
            ),
        )
        logical_sources.append(visual or audio)
    result["sources"] = logical_sources
    return result


def _annotate_media_kinds(mapping, catalog: dict) -> None:
    media_by_id = {
        str(item.get("source_id") or ""): item
        for item in catalog.get("sources", [])
        if isinstance(item, dict)
    }
    for detail in mapping.details.values():
        item = media_by_id.get(str(detail.get("media_source_id") or ""))
        if item is not None:
            detail["media_kind"] = _media_kind(item)


def _attach_audio_placeholder_cards(
    catalog: dict,
    *,
    paths: dict,
    services: ModuleType,
    width: int,
    height: int,
) -> None:
    placeholder_dir = paths["work"] / "placeholders"
    for item in catalog.get("sources", []):
        if not isinstance(item, dict) or _media_kind(item) != "audio":
            continue
        source_id = str(item.get("source_id") or "audio")
        name = str(item.get("name") or Path(str(item.get("relative_path") or "")).name)
        card_path = placeholder_dir / f"stage3_{re.sub(r'[^A-Za-z0-9_.-]+', '-', source_id)}.png"
        services.generate_placeholder(
            seg_id=source_id,
            speaker=Path(name).stem,
            role="НУЖНО ВИДЕО",
            timecode="AUDIO ONLY",
            text=f"Подложите видео вместо аудиоисточника {name}",
            out_path=card_path,
            width=width,
            height=height,
            footer_label=f"НУЖНО ВИДЕО · {Path(name).stem}",
        )
        item["placeholder_path"] = str(card_path)


def _register_review_sources(catalog: dict, paths: dict, services: ModuleType) -> list:
    """Mirror the current project media catalog into Stage 2 sync sources."""

    source_store, _match_store = services._sync_stores(paths)
    existing = source_store.list()
    grouped: dict[str, list[dict]] = {}
    for item in catalog.get("sources", []):
        if not isinstance(item, dict):
            continue
        relative_path = str(item.get("relative_path") or "").strip()
        if relative_path:
            key = Path(relative_path).with_suffix("").as_posix().casefold()
            grouped.setdefault(key, []).append(item)

    registered = []
    for items in grouped.values():
        videos = [item for item in items if _media_kind(item) == "video"]
        audios = sorted(
            (item for item in items if _media_kind(item) == "audio"),
            key=lambda candidate: (
                Path(
                    str(candidate.get("name") or candidate.get("relative_path") or "")
                ).suffix.lower()
                != ".wav",
                str(candidate.get("relative_path") or "").casefold(),
            ),
        )
        visual = videos[0] if videos else None
        audio = audios[0] if audios else visual
        item = visual or audio
        if item is None or audio is None:
            continue
        relative_path = str(item.get("relative_path") or "").strip()
        source_id = str(item.get("source_id") or "").strip()
        name = str(item.get("name") or Path(relative_path).name).strip()
        guide_audio = str(audio.get("relative_path") or "").strip()
        if not source_id or not relative_path or not name:
            continue
        media_identity = normalize_source_identity(name)
        identity_matches = [
            source
            for source in existing
            if media_identity
            and media_identity
            in {
                normalize_source_identity(source.premiere_sequence),
                normalize_source_identity(source.proxy_sequence_name),
                normalize_source_identity(source.speaker),
            }
        ]
        # Prefer a manually/imported Premiere source on the first scan, then
        # keep the same bound source on every subsequent scan. The catalog ID
        # is file-derived and must not replace a stable Stage 2 source ID.
        bound_source = next(
            (source for source in identity_matches if source.source_kind != "review_media"),
            identity_matches[0] if identity_matches else None,
        )
        if bound_source is None:
            bound_source = next(
                (source for source in existing if source.id == source_id),
                None,
            )
        speaker_hint = re.sub(r"^\d{6,8}[\s_.-]*", "", Path(name).stem)
        speaker_hint = re.sub(
            r"\b(?:просмотровка|просмотр|review|proxy|render|final)\b",
            " ",
            speaker_hint,
            flags=re.IGNORECASE,
        )
        speaker_hint = " ".join(speaker_hint.replace("_", " ").split()) or Path(name).stem
        source_payload = bound_source.to_dict() if bound_source is not None else {}
        source_payload.update(
            {
                "id": bound_source.id if bound_source is not None else source_id,
                "source_kind": "review_media",
                "premiere_sequence": (
                    bound_source.premiere_sequence if bound_source is not None else Path(name).stem
                ),
                "proxy_sequence_name": (
                    bound_source.proxy_sequence_name
                    if bound_source is not None
                    else f"RC_SRC_{Path(name).stem}"
                ),
                # The filename-derived name is a technical label, not a
                # verified speaker identity. Leaving it out of `speaker` keeps
                # newly scanned sources searchable for every speaker (see
                # SyncMatcher._eligible_sources) until an operator confirms
                # who is actually in the footage; the raw guess is preserved
                # below as a UI hint instead of being discarded.
                "speaker": bound_source.speaker if bound_source is not None else "",
                "speakers": bound_source.speakers if bound_source is not None else (),
                "all_speakers": bound_source.all_speakers if bound_source is not None else False,
                "fps": item.get("fps") or 25.0,
                "duration_sec": item.get("duration_sec") or 0.0,
                # faster-whisper/PyAV can decode the audio stream directly from the
                # project-local viewing; no Premiere guide export is required.
                "guide_audio": (
                    bound_source.guide_audio
                    if bound_source is not None and bound_source.guide_audio
                    else guide_audio
                ),
                "media_path": relative_path,
                "media_fingerprint": (
                    bound_source.media_fingerprint
                    if bound_source is not None and bound_source.guide_audio
                    else audio.get("fingerprint") or ""
                ),
                "metadata": {
                    **(bound_source.metadata if bound_source is not None else {}),
                    "managed_by": "stage3_media_scan",
                    "media_name": name,
                    "audio_guide_name": audio.get("name") or Path(guide_audio).name,
                    "has_companion_video": visual is not None,
                    "scanned_media_speaker_hint": speaker_hint,
                },
            }
        )
        registered.append(services.SyncSource.from_dict(source_payload))
    registered_ids = {source.id for source in registered}
    preserved = [
        source
        for source in existing
        if source.id not in registered_ids and source.source_kind != "review_media"
    ]
    with services._SYNC_DATA_LOCK:
        source_store.replace_all(sorted(preserved + registered, key=lambda item: item.id))
    return registered


def _project_transition(project: str, services: ModuleType):
    factory = getattr(services, "_project_transition_lock", None)
    return factory(project) if callable(factory) else nullcontext()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _nested_timeline_filename(project: str, scenario_revision_id: object) -> str:
    """Build the on-disk name for a freshly prepared nested XML."""

    return xml_export_filename(project, scenario_revision_id)


def _persisted_nested_timeline_filename(project: str, mapping_data: dict) -> str:
    """Read a server-generated export name, falling back for legacy mappings."""

    filename = str(mapping_data.get("nested_timeline_filename") or "").strip()
    if not filename:
        return f"{project}_nested_timeline.xml"
    if Path(filename).name != filename or not filename.endswith(".xml"):
        raise ValueError("invalid nested timeline filename in stage3 source mapping")
    return filename


def _scan_project_media(
    project: str, paths: dict, services: ModuleType, *, recursive: bool = False
) -> dict:
    project_root = paths["root"].resolve()
    projects_root = Path(getattr(services, "PROJECTS_DIR", project_root.parent)).resolve()
    try:
        project_root.relative_to(projects_root)
    except ValueError as exc:
        raise MediaCatalogError("project directory resolves outside the projects root") from exc
    inbox_reviews = paths.get("inbox_reviews", project_root / "ЗАГРУЗИТЬ" / "Просмотровки")
    legacy_reviews = paths.get("media_reviews", project_root / "media" / "reviews")
    # The friendly inbox and legacy folder coexist during migration. Scanning
    # only the inbox after its first upload used to silently remove all legacy
    # review-media registrations.
    directories = [Path(legacy_reviews)]
    inbox_path = Path(inbox_reviews)
    if inbox_path.is_dir() and any(
        item.is_file() and item.suffix.lower() in MEDIA_EXTENSIONS for item in inbox_path.iterdir()
    ):
        directories.insert(0, inbox_path)

    catalogs = [
        scan_media_directory(str(directory), recursive=recursive, media_root=project_root)
        for directory in directories
    ]
    sources_by_path: dict[str, dict] = {}
    for catalog in catalogs:
        for source in catalog.get("sources", []):
            if isinstance(source, dict):
                sources_by_path.setdefault(str(source.get("relative_path") or ""), source)
    return {
        "directory": str(project_root),
        "relative_directory": ".",
        "recursive": recursive,
        "sources": list(sources_by_path.values()),
    }


def _perform_scan(
    project: str, services: ModuleType, *, recursive: bool = False, progress=None
) -> dict:
    paths = services._ensure_project(project)
    if progress:
        progress(10, "Сканирование папки просмотровок")
    catalog = _scan_project_media(project, paths, services, recursive=recursive)
    if progress:
        progress(70, "Регистрация источников Whisper")
    registered = _register_review_sources(catalog, paths, services)
    catalog["registered_sources"] = len(registered)
    work_dir = paths.get("work")
    timeline_path = Path(work_dir) / "full_timeline.json" if work_dir else None
    if timeline_path is not None and timeline_path.is_file():
        if progress:
            progress(85, "Сопоставление просмотровок с таймлайном")
        timeline_data = json.loads(timeline_path.read_text(encoding="utf-8"))
        confirmed, _reason = services._confirmed_normalization(project)
        if confirmed and _timeline_is_current(timeline_data, confirmed):
            source_store, _match_store = services._sync_stores(paths)
            mapping = map_catalog_to_timeline(
                timeline_data,
                {"sources": [source.to_dict() for source in source_store.list()]},
                _timeline_catalog(catalog),
            )
            _annotate_media_kinds(mapping, catalog)
            catalog["mapping"] = _mapping_summary(mapping)
    return {"project": project, **catalog}


def _perform_prepare(project: str, services: ModuleType, *, progress=None) -> dict:
    paths = services._ensure_project(project)
    timeline_path = paths["work"] / "full_timeline.json"
    if not timeline_path.is_file():
        raise Stage3OperationError("full_timeline.json not found — run Build Timeline first", 404)
    if progress:
        progress(10, "Проверка собранного таймлайна")
    timeline_data = json.loads(timeline_path.read_text(encoding="utf-8"))
    confirmed, _reason = services._confirmed_normalization(project)
    if not confirmed or not _timeline_is_current(timeline_data, confirmed):
        raise Stage3OperationError(
            "timeline_rebuild_required: нормализация изменилась после сборки таймлайна",
            409,
        )
    if progress:
        progress(25, "Проверка доступных просмотровок")
    catalog = _scan_project_media(project, paths, services)
    logical_catalog = _timeline_catalog(catalog)
    if not logical_catalog.get("sources"):
        return {
            "project": project,
            "format": "legacy",
            "filename": xml_export_filename(project, timeline_data.get("scenario_revision_id")),
            "download_url": f"/stage2/export/xml?project={project}",
            "media_count": 0,
            "audio_count": 0,
            "mapped_source_ids": [],
            "unmapped_source_ids": [],
            "placeholder_segments": sum(
                1
                for segment in timeline_data.get("segments", [])
                if isinstance(segment, dict) and segment.get("type") in {"snh", "host", "life"}
            ),
            "source_mapping": [],
        }
    if progress:
        progress(50, "Подготовка заменяемых subsequence")
    _attach_audio_placeholder_cards(
        logical_catalog,
        paths=paths,
        services=services,
        width=int(timeline_data.get("width") or 1920),
        height=int(timeline_data.get("height") or 1080),
    )
    source_store, _match_store = services._sync_stores(paths)
    sync_sources = [source.to_dict() for source in source_store.list()]
    host_catalog = _host_catalog(logical_catalog, project=project, project_root=paths["root"])
    mapping = map_catalog_to_timeline(timeline_data, {"sources": sync_sources}, host_catalog)
    _annotate_media_kinds(mapping, host_catalog)
    mapped_timeline_source_ids = set(mapping.catalog)
    export_timeline, placeholder_count = _placeholder_unmapped_sources(
        _host_timeline_paths(timeline_data, project=project, project_root=paths["root"]),
        mapped_timeline_source_ids,
    )
    export_catalog: dict[str, object] = {
        str(source["source_id"]): source
        for source in host_catalog.get("sources", [])
        if isinstance(source, dict) and source.get("source_id")
    }
    export_catalog.update(mapping.catalog)
    if progress:
        progress(80, "Генерация nested XML")
    xml_text = build_nested_xml(export_timeline, export_catalog)
    filename = _nested_timeline_filename(project, timeline_data.get("scenario_revision_id"))
    output_path = paths["work"] / filename
    filename_stem = output_path.stem
    collision = 2
    while output_path.exists():
        filename = f"{filename_stem}_{collision}.xml"
        output_path = paths["work"] / filename
        collision += 1
    atomic_write_text(output_path, xml_text)
    mapping_payload = mapping.to_dict()
    mapping_payload["normalization_revision"] = timeline_data.get("normalization_revision")
    mapping_payload["scenario_revision_id"] = str(timeline_data.get("scenario_revision_id") or "")
    mapping_payload["nested_timeline_filename"] = filename
    atomic_write_json(paths["work"] / "stage3_source_mapping.json", mapping_payload)
    mapped = [
        source_id
        for source_id, detail in mapping.details.items()
        if detail.get("status") == "mapped"
    ]
    return {
        "project": project,
        "format": "nested",
        "filename": filename,
        "download_url": f"/stage3/export/xml?project={project}",
        "media_count": len(host_catalog.get("sources", [])),
        "audio_count": sum(
            1
            for item in catalog.get("sources", [])
            if isinstance(item, dict) and _media_kind(item) == "audio"
        ),
        "mapped_source_ids": mapped,
        "unmapped_source_ids": list(mapping.unmapped_source_ids),
        "placeholder_segments": placeholder_count,
        "source_mapping": _mapping_summary(mapping),
    }


def _job_status_path(project: str, kind: str, services: ModuleType) -> Path:
    return services._ensure_project(project)["work"] / f"stage3_{kind}_job_status.json"


def _legacy_job_status_path(project: str, services: ModuleType) -> Path:
    return services._ensure_project(project)["work"] / "stage3_job_status.json"


def _persist_job(job: dict, services: ModuleType) -> None:
    atomic_write_json(_job_status_path(str(job["project"]), str(job["kind"]), services), job)


def _read_json_dict(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_persisted_jobs(project: str, kind: str, services: ModuleType) -> list[dict]:
    """Read the persisted job for ``kind``; fall back to the pre-split shared file."""
    path = _job_status_path(project, kind, services)
    persisted = _read_json_dict(path)
    if persisted is not None:
        return [persisted]
    if path.exists():
        return []
    legacy = _read_json_dict(_legacy_job_status_path(project, services))
    if legacy is not None and legacy.get("kind") == kind:
        return [legacy]
    return []


def _mark_interrupted(job: dict, services: ModuleType) -> None:
    if job.get("status") in {"queued", "running"}:
        job.update(
            {
                "status": "interrupted",
                "message": "Stage 3 был прерван перезапуском сервера",
                "finished_at": _now_iso(),
            }
        )
        _persist_job(job, services)


def _trim_jobs_locked(jobs: dict) -> None:
    trim_terminal_jobs(
        jobs,
        terminal_statuses=_STAGE3_TERMINAL_STATUSES,
        now=datetime.now(timezone.utc),
        ttl_seconds=_STAGE3_JOBS_TTL_SECONDS,
        max_jobs=_STAGE3_JOBS_MAX,
    )


def _update_job(job_id: str, jobs: dict, jobs_lock, services: ModuleType, **updates) -> dict | None:
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return None
        job.update(updates)
        snapshot = dict(job)
    _persist_job(snapshot, services)
    return snapshot


def _create_job(project: str, kind: str, jobs: dict, jobs_lock, services: ModuleType) -> dict:
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "project": project,
        "kind": kind,
        "status": "queued",
        "progress": 0,
        "message": "Задача поставлена в очередь",
        "result": None,
        "error": "",
        "created_at": _now_iso(),
        "started_at": None,
        "finished_at": None,
    }
    with jobs_lock:
        _trim_jobs_locked(jobs)
        jobs[job_id] = job
    _persist_job(job, services)
    return dict(job)


def _run_job(
    job_id: str,
    project: str,
    kind: str,
    recursive: bool,
    jobs: dict,
    jobs_lock,
    active_projects: set[str],
    services: ModuleType,
) -> None:
    def progress(value: int, message: str) -> None:
        _update_job(job_id, jobs, jobs_lock, services, progress=value, message=message)

    _update_job(
        job_id,
        jobs,
        jobs_lock,
        services,
        status="running",
        started_at=_now_iso(),
        message="Запуск Stage 3",
    )
    try:
        result = (
            _perform_scan(project, services, recursive=recursive, progress=progress)
            if kind == "scan"
            else _perform_prepare(project, services, progress=progress)
        )
        _update_job(
            job_id,
            jobs,
            jobs_lock,
            services,
            status="completed",
            progress=100,
            message=("Просмотровки обновлены" if kind == "scan" else "XML готов"),
            result=result,
            finished_at=_now_iso(),
        )
    except Stage3OperationError as exc:
        _update_job(
            job_id,
            jobs,
            jobs_lock,
            services,
            status="failed",
            error=str(exc),
            error_status=exc.status_code,
            message=str(exc),
            finished_at=_now_iso(),
        )
    except (MediaCatalogError, TypeError, ValueError, json.JSONDecodeError) as exc:
        _update_job(
            job_id,
            jobs,
            jobs_lock,
            services,
            status="failed",
            error=str(exc),
            error_status=400,
            message=str(exc),
            finished_at=_now_iso(),
        )
    except Exception:
        _update_job(
            job_id,
            jobs,
            jobs_lock,
            services,
            status="failed",
            error="internal server error",
            error_status=500,
            message="Внутренняя ошибка Stage 3",
            finished_at=_now_iso(),
        )
    finally:
        with jobs_lock:
            active_projects.discard(project)


def _start_job(
    kind: str,
    jobs: dict,
    jobs_lock,
    active_projects: set[str],
    services: ModuleType,
):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return services._json_error("JSON object is required", 400)
    try:
        project = services._resolve_project_name(payload.get("project"))
        services._ensure_project(project)
        recursive = payload.get("recursive", False)
        if not isinstance(recursive, bool):
            raise MediaCatalogError("recursive must be a boolean")
    except (MediaCatalogError, ValueError) as exc:
        return services._json_error(str(exc), 400)
    if kind == "prepare":
        gate = services._normalization_required(project)
        if gate:
            return gate
    with _project_transition(project, services):
        has_active_mutation = getattr(services, "_project_has_active_mutation", None)
        if callable(has_active_mutation) and has_active_mutation(project):
            return services._json_error(
                f"another mutating job is active for project {project}", 409
            )
        with jobs_lock:
            if project in active_projects:
                return services._json_error(f"stage3 job already active for project {project}", 409)
            active_projects.add(project)
    try:
        job = _create_job(project, kind, jobs, jobs_lock, services)
        worker = threading.Thread(
            target=_run_job,
            args=(job["id"], project, kind, recursive, jobs, jobs_lock, active_projects, services),
            daemon=True,
        )
        worker.start()
    except Exception:
        with jobs_lock:
            active_projects.discard(project)
        raise
    return jsonify(
        {
            "ok": True,
            "result": {
                "project": project,
                "job_id": job["id"],
                "kind": kind,
                "status": "queued",
                "status_url": f"/stage3/jobs/{job['id']}?project={project}",
            },
        }
    )


def register_stage3_routes(app: Flask, services: ModuleType) -> None:
    jobs: dict[str, dict] = {}
    jobs_lock = threading.RLock()
    active_projects: set[str] = set()
    # Publish the registry through the API facade so scenario-version transitions
    # can atomically coordinate with Stage 3 jobs as well.
    services.register_job_registry("подготовки Stage 3", jobs_lock, active_projects)

    @app.post("/stage3/jobs/scan/start")
    def stage3_scan_job_start():
        return _start_job("scan", jobs, jobs_lock, active_projects, services)

    @app.post("/stage3/jobs/prepare/start")
    def stage3_prepare_job_start():
        return _start_job("prepare", jobs, jobs_lock, active_projects, services)

    @app.get("/stage3/jobs/<job_id>")
    def stage3_job_status(job_id: str):
        with jobs_lock:
            job = dict(jobs[job_id]) if job_id in jobs else None
        if job is None:
            try:
                project = services._resolve_project_name(request.args.get("project"))
            except ValueError as exc:
                return services._json_error(str(exc), 400)
            for kind in _STAGE3_JOB_KINDS:
                for persisted in _read_persisted_jobs(project, kind, services):
                    if str(persisted.get("id") or "") == job_id:
                        job = persisted
                        _mark_interrupted(job, services)
                        break
                if job is not None:
                    break
        if job is None:
            return services._json_error("job not found", 404)
        return jsonify({"ok": True, "result": job})

    @app.get("/stage3/jobs/status")
    def stage3_job_status_alias():
        try:
            project = services._resolve_project_name(request.args.get("project"))
        except ValueError as exc:
            return services._json_error(str(exc), 400)
        kind = str(request.args.get("kind") or "").strip()
        if kind and kind not in {"scan", "prepare"}:
            return services._json_error("kind must be 'scan' or 'prepare'", 400)
        with jobs_lock:
            candidates = [
                dict(job)
                for job in jobs.values()
                if job.get("project") == project and (not kind or job.get("kind") == kind)
            ]
        if candidates:
            latest = max(candidates, key=lambda item: str(item.get("created_at") or ""))
            return jsonify({"ok": True, "result": latest})
        restored_jobs = [
            restored
            for current_kind in ([kind] if kind else _STAGE3_JOB_KINDS)
            for restored in _read_persisted_jobs(project, current_kind, services)
        ]
        if not restored_jobs:
            return services._json_error("job not found", 404)
        restored = max(restored_jobs, key=lambda item: str(item.get("created_at") or ""))
        _mark_interrupted(restored, services)
        return jsonify({"ok": True, "result": restored})

    @app.post("/stage3/media/scan")
    def stage3_media_scan():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return services._json_error("JSON object is required", 400)
        try:
            project = services._resolve_project_name(payload.get("project"))
            recursive = payload.get("recursive", False)
            if not isinstance(recursive, bool):
                raise MediaCatalogError("recursive must be a boolean")
            with _project_transition(project, services):
                has_active_mutation = getattr(services, "_project_has_active_mutation", None)
                if callable(has_active_mutation) and has_active_mutation(project):
                    return services._json_error(
                        f"another mutating job is active for project {project}", 409
                    )
                with jobs_lock:
                    if project in active_projects:
                        return services._json_error(
                            f"stage3 job already active for project {project}", 409
                        )
                    active_projects.add(project)
            try:
                result = _perform_scan(project, services, recursive=recursive)
            finally:
                with jobs_lock:
                    active_projects.discard(project)
        except (MediaCatalogError, ValueError, json.JSONDecodeError) as exc:
            return services._json_error(str(exc), 400)
        except Exception as exc:
            return services._internal_error(exc)
        return jsonify({"ok": True, "result": result})

    @app.post("/stage3/timeline/prepare")
    def stage3_timeline_prepare():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return services._json_error("JSON object is required", 400)
        try:
            project = services._resolve_project_name(payload.get("project"))
            gate = services._normalization_required(project)
            if gate:
                return gate
            with _project_transition(project, services):
                result = _perform_prepare(project, services)
        except Stage3OperationError as exc:
            return services._json_error(str(exc), exc.status_code)
        except (MediaCatalogError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return services._json_error(str(exc), 400)
        except Exception as exc:
            return services._internal_error(exc)
        return jsonify({"ok": True, "result": result})

    @app.get("/stage3/export/xml")
    def stage3_export_xml():
        try:
            project = services._resolve_project_name(request.args.get("project"))
            paths = services._project_paths(project)
        except ValueError as exc:
            return services._json_error(str(exc), 400)
        timeline_path = paths["work"] / "full_timeline.json"
        try:
            timeline_data = json.loads(timeline_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return services._json_error(
                "full_timeline.json not found — run Build Timeline first", 404
            )
        except (OSError, json.JSONDecodeError) as exc:
            return services._json_error(f"invalid full_timeline.json: {exc}", 500)
        confirmed, _reason = services._confirmed_normalization(project)
        if not confirmed or not _timeline_is_current(timeline_data, confirmed):
            return services._json_error(
                "timeline_rebuild_required: нормализация изменилась после сборки таймлайна",
                409,
            )
        mapping_path = paths["work"] / "stage3_source_mapping.json"
        try:
            mapping_data = json.loads(mapping_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            # Legacy Stage 3 exports did not always persist mapping metadata.
            mapping_data = {}
        except (OSError, json.JSONDecodeError) as exc:
            return services._json_error(f"invalid stage3_source_mapping.json: {exc}", 500)
        try:
            filename = _persisted_nested_timeline_filename(project, mapping_data)
        except ValueError as exc:
            return services._json_error(str(exc), 500)
        if not (paths["work"] / filename).is_file():
            return services._json_error("nested timeline not found — prepare Stage 3 first", 404)
        mapping_normalization_revision = mapping_data.get("normalization_revision")
        if (
            mapping_normalization_revision not in (None, "")
            and mapping_normalization_revision != timeline_data.get("normalization_revision")
        ) or not _scenario_revision_matches(
            mapping_data.get("scenario_revision_id"),
            timeline_data.get("scenario_revision_id"),
        ):
            return services._json_error(
                "stage3_rebuild_required: редакция изменилась после подготовки Stage 3",
                409,
            )
        return send_from_directory(
            paths["work"],
            filename,
            as_attachment=True,
            download_name=xml_export_filename(project, timeline_data.get("scenario_revision_id")),
            mimetype="application/xml",
        )
