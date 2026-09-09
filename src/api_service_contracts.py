"""Typed structural contracts for the API compatibility facade."""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Protocol

from stage2.models import FullTimeline as FullTimelineModel
from stage2.sync_models import WordIndex

JsonDict = dict[str, Any]
UsageCallback = Callable[[JsonDict], None]


class FinalReviewJobServices(Protocol):
    """Dependencies consumed by final-review job orchestration."""

    _FINAL_REVIEW_JOBS: dict[str, JsonDict]
    _FINAL_REVIEW_JOBS_LOCK: Any
    _FINAL_REVIEW_ACTIVE_PROJECTS: set[str]
    uuid: ModuleType
    FullTimeline: type[FullTimelineModel]

    def _project_transition_lock(self, project: str) -> AbstractContextManager[Any]: ...

    def _project_has_active_mutation(self, project: str) -> bool: ...

    def reserve_project(self, lock: Any, active_projects: set[str], project: str) -> bool: ...

    def release_project(self, lock: Any, active_projects: set[str], project: str) -> None: ...

    def get_job(self, lock: Any, jobs: dict[str, JsonDict], job_id: str) -> JsonDict | None: ...

    def update_job(
        self,
        lock: Any,
        jobs: dict[str, JsonDict],
        job_id: str,
        updates: JsonDict,
    ) -> JsonDict | None: ...

    def _utc_now_iso(self) -> str: ...

    def _project_paths(self, project: str) -> dict[str, Path]: ...

    def _atomic_write_json_file(self, path: Path, payload: JsonDict) -> None: ...

    def _read_json_safe(self, path: Path) -> JsonDict: ...

    def _make_openrouter_usage_callback(self, project: str, **context: Any) -> UsageCallback: ...

    def _update_final_review_job(self, job_id: str, **updates: Any) -> JsonDict | None: ...

    def _release_final_review_project(self, project: str) -> None: ...

    def validate_assembly_manifest(self, manifest: JsonDict, **paths: Path) -> None: ...

    def transcribe_audio(self, audio_path: Path, **options: Any) -> JsonDict: ...

    def ingest_word_index(
        self, payload: JsonDict, source_id: str, **metadata: str
    ) -> WordIndex: ...

    def save_word_index(self, path: Path, index: WordIndex) -> None: ...

    def review_rendered_timeline(
        self, timeline: FullTimelineModel, index: WordIndex
    ) -> JsonDict: ...


class TtsJobServices(Protocol):
    """Dependencies consumed by Stage 1 TTS job helpers."""

    app: Any
    datetime: ModuleType
    UTC: Any
    uuid: ModuleType
    threading: ModuleType
    time: ModuleType
    TtsCancelledError: type[Exception]
    _TTS_JOBS: dict[str, JsonDict]
    _TTS_JOBS_LOCK: Any
    _TTS_ACTIVE_PROJECTS: set[str]
    _TTS_CANCEL_EVENTS: dict[str, Any]
    _TTS_JOBS_TTL_SECONDS: float
    _TTS_JOBS_MAX: int
    _TTS_SEMAPHORE: Any

    def _normalize_project_name(self, name: str) -> str: ...

    def _project_paths(self, project: str) -> dict[str, Path]: ...

    def _atomic_write_json_file(self, path: Path, payload: JsonDict) -> None: ...

    def _read_json_safe(self, path: Path) -> JsonDict: ...

    def _utc_now_iso(self) -> str: ...

    def reserve_project(self, lock: Any, active_projects: set[str], project: str) -> bool: ...

    def release_project(self, lock: Any, active_projects: set[str], project: str) -> None: ...

    def update_job(
        self,
        lock: Any,
        jobs: dict[str, JsonDict],
        job_id: str,
        updates: JsonDict,
        *,
        increments: dict[str, int | float] | None = None,
    ) -> JsonDict | None: ...

    def get_job(self, lock: Any, jobs: dict[str, JsonDict], job_id: str) -> JsonDict | None: ...

    def _persist_tts_job(self, job: JsonDict) -> None: ...

    def _trim_tts_jobs_locked(self) -> None: ...

    def _tts_cancel_event(self, job_id: str) -> Any: ...

    def _get_tts_job(self, job_id: str) -> JsonDict | None: ...

    def _update_tts_job(self, job_id: str, **updates: Any) -> JsonDict | None: ...

    def _release_tts_project(self, project: str) -> None: ...

    def _make_openrouter_usage_callback(self, project: str, **context: Any) -> UsageCallback: ...

    def synthesize_tts(self, **options: Any) -> JsonDict: ...

    def _save_voiceover_revision_assets(self, project: str, result: JsonDict) -> None: ...


class NormalizationJobServices(Protocol):
    """Dependencies consumed by Stage 1 normalization job helpers."""

    app: Any
    uuid: ModuleType
    NORMALIZATION_MEMORY_PATH: Path
    _NORMALIZATION_JOBS: dict[str, JsonDict]
    _NORMALIZATION_JOBS_LOCK: Any
    _NORMALIZATION_ACTIVE_PROJECTS: set[str]
    _NORMALIZATION_JOBS_MAX: int

    def _normalize_project_name(self, name: str) -> str: ...

    def _project_paths(self, project: str) -> dict[str, Path]: ...

    def _atomic_write_json_file(self, path: Path, payload: JsonDict) -> None: ...

    def _read_json_safe(self, path: Path) -> JsonDict: ...

    def _utc_now_iso(self) -> str: ...

    def reserve_project(self, lock: Any, active_projects: set[str], project: str) -> bool: ...

    def release_project(self, lock: Any, active_projects: set[str], project: str) -> None: ...

    def update_job(
        self,
        lock: Any,
        jobs: dict[str, JsonDict],
        job_id: str,
        updates: JsonDict,
    ) -> JsonDict | None: ...

    def get_job(self, lock: Any, jobs: dict[str, JsonDict], job_id: str) -> JsonDict | None: ...

    def _persist_normalization_job(self, job: JsonDict) -> None: ...

    def _update_normalization_job(self, job_id: str, **updates: Any) -> JsonDict | None: ...

    def _release_normalization_project(self, project: str) -> None: ...

    def _make_openrouter_usage_callback(self, project: str, **context: Any) -> UsageCallback: ...

    def generate_normalization(
        self, input_path: Path, output_path: Path, **options: Any
    ) -> JsonDict: ...


class SyncWorkerServices(Protocol):
    """Dependencies consumed by the extracted Stage 2 sync worker."""

    app: Any
    os: ModuleType
    threading: ModuleType
    time: ModuleType
    _JobCancelled: type[Exception]
    WhisperCancelledError: type[Exception]
    AlignmentRequest: Any
    MatcherConfig: Any
    SyncMatcher: Any
    _TRANSCRIPTION_CACHE_KEY: str
    _SYNC_DATA_LOCK: Any
    _SYNC_JOBS_LOCK: Any
    _SYNC_CANCEL_EVENTS: dict[str, Any]

    def _project_paths(self, project: str) -> dict[str, Path]: ...

    def _sync_stores(self, paths: dict[str, Path]) -> tuple[Any, Any]: ...

    def _sync_cancel_event(self, job_id: str) -> Any: ...

    def _update_sync_job(self, job_id: str, **updates: Any) -> JsonDict | None: ...

    def _utc_now_iso(self) -> str: ...

    def _word_index_path(self, paths: dict[str, Path], source_id: str) -> Path: ...

    def _transcription_config(self, **options: Any) -> JsonDict: ...

    def _resolve_sync_audio_path(self, source: Any, paths: dict[str, Path]) -> Path: ...

    def load_word_index(self, path: Path, source_id: str) -> WordIndex: ...

    def _transcription_cache_metadata(
        self, audio_path: Path, config: dict[str, str], **options: Any
    ) -> JsonDict: ...

    def save_word_index(self, path: Path, index: WordIndex) -> None: ...

    def rescale_stretched_word_index(
        self, index: WordIndex, duration_sec: float
    ) -> float | None: ...

    def _audio_stat_signature(self, path: Path) -> JsonDict: ...

    def _audio_cache_signature(self, path: Path) -> JsonDict: ...

    def transcribe_audio(self, audio_path: Path, **options: Any) -> JsonDict: ...

    def ingest_word_index(
        self, payload: JsonDict, source_id: str, **metadata: Any
    ) -> WordIndex: ...

    def _word_index_audio_matches(
        self, index: WordIndex, audio_path: Path, **options: Any
    ) -> bool: ...

    def load_normalization(self, path: Path) -> JsonDict: ...

    def _sync_alignment_units(
        self,
        blocks: list[JsonDict],
        *,
        source_speakers: set[tuple[str, ...]] | None = None,
    ) -> list[JsonDict]: ...

    def get_settings(self) -> Any: ...

    def _semantic_review_candidates(self, *args: Any, **kwargs: Any) -> list[Any]: ...

    def normalize_text(self, text: str) -> list[str]: ...

    def review_candidates_with_llm(self, *args: Any, **kwargs: Any) -> Any: ...

    def _apply_semantic_review(self, *args: Any, **kwargs: Any) -> Any: ...

    def _sync_matching_fingerprint(self, *args: Any, **kwargs: Any) -> str: ...

    def _sync_summary(self, *args: Any, **kwargs: Any) -> JsonDict: ...

    def _release_sync_project(self, project: str) -> None: ...

    def _make_openrouter_usage_callback(self, project: str, **context: Any) -> UsageCallback: ...
