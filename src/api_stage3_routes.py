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
from stage3.source_mapping import map_catalog_to_timeline, normalize_source_identity


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
            "media_kind": detail.get("media_kind", "video"),
        })
    return result


def _media_kind(item: dict) -> str:
    explicit = str(item.get("media_kind") or "").strip().lower()
    if explicit in {"audio", "video"}:
        return explicit
    return "audio" if Path(str(item.get("name") or item.get("relative_path") or "")).suffix.lower() in {".mp3", ".wav"} else "video"


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
                Path(str(candidate.get("name") or candidate.get("relative_path") or "")).suffix.lower() != ".wav",
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
            and media_identity in {
                normalize_source_identity(source.premiere_sequence),
                normalize_source_identity(source.proxy_sequence_name),
                normalize_source_identity(source.speaker),
            }
        ]
        # Prefer a manually/imported Premiere source on the first scan, then
        # keep the same bound source on every subsequent scan. The catalog ID
        # is file-derived and must not replace a stable Stage 2 source ID.
        bound_source = next(
            (
                source
                for source in identity_matches
                if source.source_kind != "review_media"
            ),
            identity_matches[0] if identity_matches else None,
        )
        if bound_source is None:
            bound_source = next(
                (source for source in existing if source.id == source_id),
                None,
            )
        speaker = re.sub(r"^\d{6,8}[\s_.-]*", "", Path(name).stem)
        speaker = re.sub(
            r"\b(?:просмотровка|просмотр|review|proxy|render|final)\b",
            " ",
            speaker,
            flags=re.IGNORECASE,
        )
        speaker = " ".join(speaker.replace("_", " ").split()) or Path(name).stem
        source_payload = bound_source.to_dict() if bound_source is not None else {}
        source_payload.update({
            "id": bound_source.id if bound_source is not None else source_id,
            "source_kind": "review_media",
            "premiere_sequence": (
                bound_source.premiere_sequence if bound_source is not None
                else Path(name).stem
            ),
            "proxy_sequence_name": (
                bound_source.proxy_sequence_name if bound_source is not None
                else f"RC_SRC_{Path(name).stem}"
            ),
            "speaker": bound_source.speaker if bound_source is not None else speaker,
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
            },
        })
        registered.append(services.SyncSource.from_dict(source_payload))
    registered_ids = {source.id for source in registered}
    preserved = [
        source for source in existing
        if source.id not in registered_ids and source.source_kind != "review_media"
    ]
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
                    _timeline_catalog(catalog),
                )
                _annotate_media_kinds(mapping, catalog)
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
            gate = services._normalization_required(project)
            if gate:
                return gate
            timeline_path = paths["work"] / "full_timeline.json"
            if not timeline_path.is_file():
                return services._json_error(
                    "full_timeline.json not found — run Build Timeline first", 404
                )
            timeline_data = json.loads(timeline_path.read_text(encoding="utf-8"))
            confirmed, _reason = services._confirmed_normalization(project)
            if (
                not confirmed
                or timeline_data.get("normalization_revision")
                != confirmed.get("revision")
            ):
                return services._json_error(
                    "timeline_rebuild_required: нормализация изменилась после сборки таймлайна",
                    409,
                )
            catalog = scan_project_media(project, paths)
            logical_catalog = _timeline_catalog(catalog)
            if not logical_catalog.get("sources"):
                return jsonify({
                    "ok": True,
                    "result": {
                        "project": project,
                        "format": "legacy",
                        "filename": f"{project}_timeline.xml",
                        "download_url": f"/stage2/export/xml?project={project}",
                        "media_count": 0,
                        "audio_count": 0,
                        "mapped_source_ids": [],
                        "unmapped_source_ids": [],
                        "placeholder_segments": sum(
                            1
                            for segment in timeline_data.get("segments", [])
                            if isinstance(segment, dict)
                            and segment.get("type") in {"snh", "host"}
                        ),
                        "source_mapping": [],
                    },
                })
            _attach_audio_placeholder_cards(
                logical_catalog,
                paths=paths,
                services=services,
                width=int(timeline_data.get("width") or 1920),
                height=int(timeline_data.get("height") or 1080),
            )
            source_store, _match_store = services._sync_stores(paths)
            sync_sources = [source.to_dict() for source in source_store.list()]
            host_catalog = _host_catalog(
                logical_catalog, project=project, project_root=paths["root"]
            )
            mapping = map_catalog_to_timeline(
                timeline_data, {"sources": sync_sources}, host_catalog
            )
            _annotate_media_kinds(mapping, host_catalog)
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
                "format": "nested",
                "filename": filename,
                "download_url": f"/stage3/export/xml?project={project}",
                "media_count": len(host_catalog.get("sources", [])),
                "audio_count": sum(
                    1 for item in catalog.get("sources", [])
                    if isinstance(item, dict) and _media_kind(item) == "audio"
                ),
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
