from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
from types import ModuleType

from flask import Flask, jsonify, request, send_from_directory

from atomic_io import atomic_write_json, atomic_write_text
from stage3.media_catalog import MediaCatalogError, scan_media_directory
from stage3.nested_xml import build_nested_xml
from stage3.source_mapping import map_catalog_to_timeline


_MATCHED_STATUSES = {"", "auto_matched", "confirmed"}


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
            and float(segment.get("source_out_sec") or 0)
            > float(segment.get("source_in_sec") or 0)
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
        result.append({
            "source_id": source_id,
            "status": detail.get("status", "unmapped"),
            "reason": detail.get("reason", ""),
            "media_name": detail.get("media_name", ""),
            "media_source_id": detail.get("media_source_id", ""),
            "max_source_out_sec": detail.get("max_source_out_sec", 0.0),
            "sequence_aliases": detail.get("sequence_aliases", []),
            "speaker_aliases": detail.get("speaker_aliases", []),
        })
    return result


def _register_review_sources(catalog: dict, paths: dict, services: ModuleType) -> list:
    """Mirror the current project media catalog into Stage 2 sync sources."""

    source_store, _match_store = services._sync_stores(paths)
    existing = source_store.list()
    preserved = [source for source in existing if source.source_kind != "review_media"]
    registered = []
    for item in catalog.get("sources", []):
        if not isinstance(item, dict):
            continue
        relative_path = str(item.get("relative_path") or "").strip()
        source_id = str(item.get("source_id") or "").strip()
        name = str(item.get("name") or Path(relative_path).name).strip()
        if not source_id or not relative_path or not name:
            continue
        speaker = re.sub(r"^\d{6,8}[\s_.-]*", "", Path(name).stem)
        speaker = re.sub(
            r"\b(?:просмотровка|просмотр|review|proxy|render|final)\b",
            " ",
            speaker,
            flags=re.IGNORECASE,
        )
        speaker = " ".join(speaker.replace("_", " ").split()) or Path(name).stem
        registered.append(services.SyncSource.from_dict({
            "id": source_id,
            "source_kind": "review_media",
            "premiere_sequence": Path(name).stem,
            "proxy_sequence_name": f"RC_SRC_{Path(name).stem}",
            "speaker": speaker,
            "all_speakers": False,
            "fps": item.get("fps") or 25.0,
            "duration_sec": item.get("duration_sec") or 0.0,
            # faster-whisper/PyAV can decode the audio stream directly from the
            # project-local viewing; no Premiere guide export is required.
            "guide_audio": relative_path,
            "media_path": relative_path,
            "media_fingerprint": item.get("fingerprint") or "",
            "metadata": {
                "managed_by": "stage3_media_scan",
                "media_name": name,
            },
        }))
    with services._SYNC_DATA_LOCK:
        source_store.replace_all(sorted(preserved + registered, key=lambda item: item.id))
    return registered


def register_stage3_routes(app: Flask, services: ModuleType) -> None:
    def scan_project_media(project: str, paths: dict, *, recursive: bool = False) -> dict:
        project_root = paths["root"].resolve()
        projects_root = Path(
            getattr(services, "PROJECTS_DIR", project_root.parent)
        ).resolve()
        try:
            project_root.relative_to(projects_root)
        except ValueError as exc:
            raise MediaCatalogError(
                "project directory resolves outside the projects root"
            ) from exc
        return scan_media_directory(
            str(paths.get("media_reviews", project_root / "media" / "reviews")),
            recursive=recursive,
            media_root=project_root,
        )

    @app.post("/stage3/media/scan")
    def stage3_media_scan():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return services._json_error("JSON object is required", 400)
        try:
            project = services._resolve_project_name(payload.get("project"))
            paths = services._ensure_project(project)
            recursive = payload.get("recursive", False)
            if not isinstance(recursive, bool):
                raise MediaCatalogError("recursive must be a boolean")
            catalog = scan_project_media(project, paths, recursive=recursive)
            registered = _register_review_sources(catalog, paths, services)
            catalog["registered_sources"] = len(registered)
            work_dir = paths.get("work")
            timeline_path = Path(work_dir) / "full_timeline.json" if work_dir else None
            if timeline_path is not None and timeline_path.is_file():
                timeline_data = json.loads(timeline_path.read_text(encoding="utf-8"))
                source_store, _match_store = services._sync_stores(paths)
                mapping = map_catalog_to_timeline(
                    timeline_data,
                    {"sources": [source.to_dict() for source in source_store.list()]},
                    catalog,
                )
                catalog["mapping"] = _mapping_summary(mapping)
        except (MediaCatalogError, ValueError) as exc:
            return services._json_error(str(exc), 400)
        except Exception as exc:
            return services._internal_error(exc)
        return jsonify({"ok": True, "result": {"project": project, **catalog}})

    @app.post("/stage3/timeline/prepare")
    def stage3_timeline_prepare():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return services._json_error("JSON object is required", 400)
        try:
            project = services._resolve_project_name(payload.get("project"))
            paths = services._ensure_project(project)
            timeline_path = paths["work"] / "full_timeline.json"
            if not timeline_path.is_file():
                return services._json_error(
                    "full_timeline.json not found — run Build Timeline first", 404
                )
            timeline_data = json.loads(timeline_path.read_text(encoding="utf-8"))
            catalog = scan_project_media(project, paths)
            source_store, _match_store = services._sync_stores(paths)
            sync_sources = [source.to_dict() for source in source_store.list()]
            host_catalog = _host_catalog(
                catalog, project=project, project_root=paths["root"]
            )
            mapping = map_catalog_to_timeline(
                timeline_data, {"sources": sync_sources}, host_catalog
            )
            mapped_timeline_source_ids = set(mapping.catalog)
            export_timeline, placeholder_count = _placeholder_unmapped_sources(
                _host_timeline_paths(
                    timeline_data, project=project, project_root=paths["root"]
                ),
                mapped_timeline_source_ids,
            )
            # Keep every viewing in RC_VIEWINGS, even if the current script has
            # not selected a quote from it yet. Aliases keyed by Stage 2 source
            # ID point at the same path and are deduplicated by the exporter.
            export_catalog = {
                str(source["source_id"]): source
                for source in host_catalog.get("sources", [])
                if isinstance(source, dict) and source.get("source_id")
            }
            export_catalog.update(mapping.catalog)
            xml_text = build_nested_xml(export_timeline, export_catalog)
            filename = f"{project}_nested_timeline.xml"
            artifact = paths["work"] / filename
            mapping_path = paths["work"] / "stage3_source_mapping.json"
            atomic_write_text(artifact, xml_text)
            atomic_write_json(mapping_path, mapping.to_dict())
        except (MediaCatalogError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return services._json_error(str(exc), 400)
        except Exception as exc:
            return services._internal_error(exc)
        mapped = [
            source_id
            for source_id, detail in mapping.details.items()
            if detail.get("status") == "mapped"
        ]
        return jsonify({
            "ok": True,
            "result": {
                "project": project,
                "filename": filename,
                "download_url": f"/stage3/export/xml?project={project}",
                "media_count": len(catalog.get("sources", [])),
                "mapped_source_ids": mapped,
                "unmapped_source_ids": list(mapping.unmapped_source_ids),
                "placeholder_segments": placeholder_count,
                "source_mapping": _mapping_summary(mapping),
            },
        })

    @app.get("/stage3/export/xml")
    def stage3_export_xml():
        try:
            project = services._resolve_project_name(request.args.get("project"))
            paths = services._project_paths(project)
        except ValueError as exc:
            return services._json_error(str(exc), 400)
        filename = f"{project}_nested_timeline.xml"
        if not (paths["work"] / filename).is_file():
            return services._json_error(
                "nested timeline not found — prepare Stage 3 first", 404
            )
        return send_from_directory(
            paths["work"], filename, as_attachment=True, mimetype="application/xml"
        )
