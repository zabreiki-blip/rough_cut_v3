from pathlib import Path


STAGE1_JS = (
    Path(__file__).parents[1] / "src" / "static" / "stage1.js"
).read_text(encoding="utf-8")
PROJECT_HTML = (
    Path(__file__).parents[1] / "src" / "templates" / "project.html"
).read_text(encoding="utf-8")


def test_restored_tts_job_rehydrates_every_resume_control() -> None:
    assert "function restoreTtsControls(state)" in STAGE1_JS
    assert "state.prepared_path" in STAGE1_JS
    assert "state.speaker_dir" in STAGE1_JS
    assert "state.provider" in STAGE1_JS
    assert "state.voice" in STAGE1_JS
    assert "state.gender" in STAGE1_JS
    assert 'hasOwnProperty.call(state, "dry_run")' in STAGE1_JS
    assert "Number(state.speed)" in STAGE1_JS
    assert "restoreTtsControls(response.result);" in STAGE1_JS


def test_tts_and_sync_polling_retry_transient_errors_with_bounded_backoff() -> None:
    assert "const MAX_POLL_BACKOFF_MS = 10000;" in STAGE1_JS
    assert "function isTransientPollingError(error)" in STAGE1_JS
    assert "function pollingBackoffMs(failures, baseDelay)" in STAGE1_JS
    assert "Math.min(MAX_POLL_BACKOFF_MS" in STAGE1_JS
    assert STAGE1_JS.count("if (!isTransientPollingError(err))") >= 3
    assert "ttsPollTimer = window.setTimeout(pollRestoredTtsJob, delay);" in STAGE1_JS
    assert "syncPollTimer = window.setTimeout(() => pollSyncJob(statusUrl), delay);" in STAGE1_JS


def test_stage3_headless_media_scan_contract() -> None:
    assert 'id="stage3_media_directory"' in PROJECT_HTML
    assert 'id="btn_stage3_media_scan"' in PROJECT_HTML
    assert 'id="stage3_media_list"' in PROJECT_HTML
    assert 'id="btn_stage3_prepare"' in PROJECT_HTML
    assert "Premiere на этом этапе не нужен" in PROJECT_HTML
    assert 'postJSON("/stage3/media/scan", { project })' in STAGE1_JS
    assert "stage3MediaScanAttempted" in STAGE1_JS
    assert "scanStage3MediaOnce();" in STAGE1_JS
    assert "localStorage" not in STAGE1_JS
    assert "/Volumes/L_BRICK" not in PROJECT_HTML
    assert '["sources", "items", "files", "media"]' in STAGE1_JS
    assert "item.duration_sec" in STAGE1_JS
    assert "item.container_path" in STAGE1_JS


def test_whisper_progress_shows_current_viewing_elapsed_and_eta() -> None:
    for element_id in (
        "sync_job_current",
        "sync_job_video_progress",
        "sync_job_elapsed",
        "sync_job_eta",
    ):
        assert f'id="{element_id}"' in PROJECT_HTML
    assert "job.transcription_processed_sec" in STAGE1_JS
    assert "job.transcription_duration_sec" in STAGE1_JS
    assert "job.transcription_source_started_at" in STAGE1_JS
    assert "overallEtaSec" in STAGE1_JS
    assert "stage3SourceMapping.find" in STAGE1_JS


def test_headless_whisper_model_and_source_scope_controls() -> None:
    assert 'id="sync_whisper_model"' in PROJECT_HTML
    assert '<option value="small" selected>Small — быстро</option>' in PROJECT_HTML
    assert '<option value="medium">Medium — точнее</option>' in PROJECT_HTML
    assert 'model: ids.sync_whisper_model?.value || "small"' in STAGE1_JS
    assert 'source_scope: stage3MediaCount > 0 ? "review_media" : "all"' in STAGE1_JS
