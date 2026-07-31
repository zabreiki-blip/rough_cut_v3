from pathlib import Path


STAGE1_JS = (
    Path(__file__).parents[1] / "src" / "static" / "stage1.js"
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
