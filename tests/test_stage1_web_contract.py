from pathlib import Path

ROOT = Path(__file__).parents[1]

# `stage1.js` is a thin ES-module entry point; the actual logic lives in
# `src/static/stage1/*.js`. Concatenate the entry point with every module so
# contract checks below can keep searching a single "STAGE1_JS" haystack
# regardless of which file a given piece of code now lives in.
_STAGE1_SOURCES = [ROOT / "src" / "static" / "stage1.js"] + sorted(
    (ROOT / "src" / "static" / "stage1").glob("*.js")
)
STAGE1_JS = "\n".join(path.read_text(encoding="utf-8") for path in _STAGE1_SOURCES)
STAGE1_CSS = (ROOT / "src" / "static" / "stage1.css").read_text(encoding="utf-8")
PROJECT_HTML = (ROOT / "src" / "templates" / "project.html").read_text(encoding="utf-8")


def test_restored_tts_job_rehydrates_every_resume_control() -> None:
    assert "function restoreTtsControls(state)" in STAGE1_JS
    assert "state.prepared_path" in STAGE1_JS
    assert "state.speaker_dir" in STAGE1_JS
    assert "state.provider" in STAGE1_JS
    assert "state.tts_model" in STAGE1_JS
    assert "state.tts_style" in STAGE1_JS
    assert "state.voice" in STAGE1_JS
    assert "state.gender" in STAGE1_JS
    assert 'hasOwnProperty.call(state, "dry_run")' in STAGE1_JS
    assert "Number(state.speed)" in STAGE1_JS
    assert "restoreTtsControls(response.result);" in STAGE1_JS


def test_tts_style_degradation_is_surfaced_in_live_panel() -> None:
    assert 'id="live_style_warning"' in PROJECT_HTML
    assert "state.style_degraded_ids" in STAGE1_JS
    assert "result.style_degraded" in STAGE1_JS
    assert ".live-warning" in STAGE1_CSS


def test_tts_menu_uses_openrouter_models_and_style_direction() -> None:
    assert '<option value="gemini"' not in PROJECT_HTML
    assert "OpenRouter · MAI-Voice-2" in PROJECT_HTML
    assert "Gemini 2.5 Flash TTS" not in PROJECT_HTML
    assert "OpenRouter · Gemini 3.1 Flash TTS" in PROJECT_HTML
    assert 'id="tts_style"' in PROJECT_HTML
    assert "tts_model: f.tts_model || null" in STAGE1_JS
    assert "tts_style: f.tts_style" in STAGE1_JS
    assert 'ids.provider.addEventListener("change", syncTtsStyleAvailability)' in STAGE1_JS


def test_voiceover_can_be_cleared_with_explicit_confirmation() -> None:
    assert 'id="btn_tts_clear"' in PROJECT_HTML
    assert 'title: "Очистить всю озвучку?"' in STAGE1_JS
    assert 'postJSON("/stage1/tts/clear", { project })' in STAGE1_JS
    assert 'ids.btn_tts_clear.addEventListener("click"' in STAGE1_JS


def test_tts_and_sync_polling_retry_transient_errors_with_bounded_backoff() -> None:
    assert "export const FETCH_TIMEOUT_MS = 30000;" in STAGE1_JS
    assert "new AbortController()" in STAGE1_JS
    assert "timeout.status = 408;" in STAGE1_JS
    assert STAGE1_JS.count("fetchWithTimeout(") >= 5
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
    assert 'id="btn_stage3_prepare"' not in PROJECT_HTML
    assert 'id="btn_export_xml"' in PROJECT_HTML
    assert "Premiere на этом этапе не нужен" in PROJECT_HTML
    assert "`/stage3/jobs/${kind}/start`" in STAGE1_JS
    assert "function pollStage3Job(statusUrl, downloadOnComplete)" in STAGE1_JS
    assert "function restoreStage3Job()" in STAGE1_JS
    assert "/stage3/jobs/status?project=" in STAGE1_JS
    assert "stage3PollTimer = window.setTimeout" in STAGE1_JS
    assert "stage3JobRunning" in STAGE1_JS
    assert (
        "export async function exportTimelineXml() {" in STAGE1_JS
        and "confirmExportSummary()" in STAGE1_JS
        and 'return startStage3Job("prepare");' in STAGE1_JS
    )
    assert "function exportTimelineXml()" in STAGE1_JS
    assert "ids.btn_export_xml.href" not in STAGE1_JS
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


def test_tts_and_stage3_progress_show_elapsed_and_eta() -> None:
    for element_id in (
        "live_job_elapsed",
        "live_job_eta",
        "stage3_job_elapsed",
        "stage3_job_eta",
    ):
        assert f'id="{element_id}"' in PROJECT_HTML
    assert "рассчитывается" in PROJECT_HTML
    assert "function computeEtaSeconds(startedAtMs, fraction)" in STAGE1_JS
    assert "function renderEtaCells(elapsedEl, etaEl, startedAtMs, fraction, status)" in STAGE1_JS
    assert "fraction >= 0.05 && fraction < 1" in STAGE1_JS
    assert "renderEtaCells(ids.live_job_elapsed, ids.live_job_eta" in STAGE1_JS
    assert "renderEtaCells(\n    ids.stage3_job_elapsed, ids.stage3_job_eta," in STAGE1_JS
    assert "ttsJobClientStartedAt" in STAGE1_JS
    assert "stage3JobClientStartedAt" in STAGE1_JS


def test_headless_whisper_model_and_source_scope_controls() -> None:
    assert 'id="sync_whisper_model"' in PROJECT_HTML
    assert '<option value="small" selected>Small — быстро</option>' in PROJECT_HTML
    assert '<option value="medium">Medium — точнее</option>' in PROJECT_HTML
    assert (
        'model: asrEngine === "local" ? (ids.sync_whisper_model?.value || "small") : undefined'
        in STAGE1_JS
    )
    # `stage3MediaCount` moved from a module-level `let` to a shared `appState`
    # property so the sync/stage3 modules can read and mutate it across files.
    assert 'source_scope: appState.stage3MediaCount > 0 ? "review_media" : "all"' in STAGE1_JS


def test_normalization_offers_safe_openrouter_speed_presets() -> None:
    assert 'id="normalization_llm_preset"' in PROJECT_HTML
    assert '<option value="fast" selected>Быстро</option>' in PROJECT_HTML
    assert '<option value="standard">Стандарт</option>' in PROJECT_HTML
    assert 'llm_preset: ids.normalization_llm_preset?.value || "standard"' in STAGE1_JS


def test_normalization_supports_life_and_problem_navigation() -> None:
    assert 'life: "Лайф"' in STAGE1_JS
    assert '<option value="life">Лайф</option>' in PROJECT_HTML
    assert 'id="norm_count_life"' in PROJECT_HTML
    for element_id in (
        "btn_norm_issue_prev",
        "btn_norm_issue_next",
        "btn_norm_issue_filter",
        "norm_issue_position",
    ):
        assert f'id="{element_id}"' in PROJECT_HTML
    assert "function blockNeedsReview(block)" in STAGE1_JS
    assert "function focusIssue(offset)" in STAGE1_JS
    assert 'card.scrollIntoView({ behavior: "smooth", block: "center" });' in STAGE1_JS
    assert 'control.value === "life"' in STAGE1_JS
    assert 'block.text_for_tts = "";' in STAGE1_JS


def test_revision_ui_preserves_edits_and_ignores_stale_diff_responses() -> None:
    # `normalizationDirty` likewise moved onto the shared `appState` object.
    assert "if (appState.normalizationDirty)" in STAGE1_JS
    assert "await saveNormalization();" in STAGE1_JS
    assert "let selectedDiffRequest = 0;" in STAGE1_JS
    assert "if (requestId !== selectedDiffRequest) return;" in STAGE1_JS
    assert "Активная редакция не определена" in STAGE1_JS
    assert "const recoveryRequired = revisionsState.integrity?.blocking === true;" in STAGE1_JS
    assert 'data-action="reconcile-revision"' in STAGE1_JS
    assert 'ids.revision_filters_bar.hidden = mode !== "list";' in STAGE1_JS
    assert ".revision-changes-list[hidden]" in STAGE1_CSS
    assert "use_llm: Boolean(ids.normalization_use_llm?.checked)" in STAGE1_JS


def test_asr_engine_selector_defaults_to_local_and_is_wired_to_request() -> None:
    assert 'id="sync_asr_engine"' in PROJECT_HTML
    assert '<option value="local" selected>Локально (бесплатно, медленно)</option>' in PROJECT_HTML
    assert '<option value="cloud">Облако (быстро, ~2¢/час, платно)</option>' in PROJECT_HTML
    assert 'sync_asr_engine: el("sync_asr_engine")' in STAGE1_JS
    assert 'const asrEngine = ids.sync_asr_engine?.value || "local";' in STAGE1_JS
    assert (
        'model: asrEngine === "local" ? (ids.sync_whisper_model?.value || "small") : undefined'
        in STAGE1_JS
    )
    assert "asr_engine: asrEngine" in STAGE1_JS
    assert "function updateSyncAsrControls()" in STAGE1_JS
    assert "ids.sync_whisper_model.disabled = isCloud;" in STAGE1_JS
    assert 'ids.sync_asr_engine.addEventListener("change", updateSyncAsrControls);' in STAGE1_JS
    assert "Модель Whisper (только локально)" in PROJECT_HTML


def test_native_confirm_is_replaced_with_custom_confirm_dialog() -> None:
    assert "window.confirm(" not in STAGE1_JS
    # Bare `confirm(` must not appear at all: the only surviving call sites are
    # `confirmDialog(` and `function confirmDialog(`, neither of which matches
    # the literal substring `confirm(` (both are followed by `Dialog`).
    assert "confirm(" not in STAGE1_JS

    assert 'id="confirm_dialog"' in PROJECT_HTML
    assert 'role="dialog"' in PROJECT_HTML
    assert 'aria-modal="true"' in PROJECT_HTML
    assert 'aria-labelledby="confirm_dialog_title"' in PROJECT_HTML
    assert 'id="confirm_dialog_title"' in PROJECT_HTML
    assert 'id="confirm_dialog_message"' in PROJECT_HTML
    assert 'id="btn_confirm_dialog_cancel"' in PROJECT_HTML
    assert 'id="btn_confirm_dialog_accept"' in PROJECT_HTML

    assert "function confirmDialog(" in STAGE1_JS
    assert "dialog.showModal();" in STAGE1_JS
    assert "onCancel = (event) => { event.preventDefault(); finish(false); };" in STAGE1_JS
    assert (
        "onBackdropClick = (event) => { if (event.target === dialog) finish(false); };" in STAGE1_JS
    )
    assert 'acceptBtn.classList.toggle("danger", Boolean(danger));' in STAGE1_JS
    assert "if (acceptBtn.disabled) return;" in STAGE1_JS
    assert "acceptBtn.disabled = true;" in STAGE1_JS
    assert STAGE1_JS.count("await confirmDialog({") == 9


def test_asr_metrics_panel_reports_engine_cost_and_timestamp_precision() -> None:
    assert 'id="sync_job_metrics"' in PROJECT_HTML
    for element_id in (
        "sync_metric_backend",
        "sync_metric_wall_time",
        "sync_metric_cost",
        "sync_metric_precision",
    ):
        assert f'id="{element_id}"' in PROJECT_HTML
    assert "function renderSyncJobMetrics(job)" in STAGE1_JS
    assert "job.asr_backend_used" in STAGE1_JS
    assert "job.transcription_wall_sec_total" in STAGE1_JS
    assert "job.asr_cost_usd_total" in STAGE1_JS
    assert "job.asr_timestamp_precision" in STAGE1_JS
    assert '"бесплатно"' in STAGE1_JS
    assert '"точные"' in STAGE1_JS
    assert '"интерполированные"' in STAGE1_JS


def test_sidebar_reports_cumulative_openrouter_usage() -> None:
    assert 'id="openrouter_usage"' in PROJECT_HTML
    assert 'class="openrouter-usage-total"' in PROJECT_HTML
    assert 'class="openrouter-usage-pending" hidden' in PROJECT_HTML
    usage_js = (ROOT / "src" / "static" / "stage1" / "usage.js").read_text(encoding="utf-8")
    usage_css = (ROOT / "src" / "static" / "stage1.css").read_text(encoding="utf-8")
    assert "/stage1/openrouter-usage" in usage_js
    assert "уточняется" in usage_js
    assert "toFixed(4)" in usage_js
    assert "pendingElement.hidden = pending <= 0" in usage_js
    assert "· уточняется" not in usage_js
    assert ".openrouter-usage-pending" in usage_css
    assert "display: grid" in usage_css


def test_stage1_entry_point_is_loaded_as_module_without_native_confirm() -> None:
    # Regression guard for the U7 ES-module split: the template must load the
    # thin entry point as `type="module"` (so its `import` statements work),
    # and no module may reintroduce the blocking native `window.confirm(`.
    assert '<script type="module" src="' in PROJECT_HTML
    assert "stage1.js" in PROJECT_HTML
    assert "window.confirm(" not in STAGE1_JS


def test_project_page_has_scroll_jump_buttons_and_module() -> None:
    scroll_js = (ROOT / "src" / "static" / "scroll_jump.js").read_text(encoding="utf-8")
    assert 'id="btn_scroll_top"' in PROJECT_HTML
    assert 'id="btn_scroll_bottom"' in PROJECT_HTML
    assert 'aria-label="Прокрутить страницу в начало"' in PROJECT_HTML
    assert 'aria-label="Прокрутить страницу в конец"' in PROJECT_HTML
    assert "filename='scroll_jump.js'" in PROJECT_HTML
    assert "export function computeVisibility" in scroll_js
    assert "prefers-reduced-motion" in scroll_js
    assert ".scroll-jump-btn:focus-visible" in STAGE1_CSS


def test_scroll_jump_logic_in_node_runtime() -> None:
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, str(ROOT / "tests" / "scroll_jump_test.cjs")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_normalization_manual_review_controls_are_wired() -> None:
    for element_id in (
        "normalization_confirm_summary",
        "normalization_group_bar",
        "norm_selected_count",
        "btn_norm_group_review",
        "btn_norm_group_clear",
    ):
        assert f'id="{element_id}"' in PROJECT_HTML
        assert f'el("{element_id}")' in STAGE1_JS
    assert '"/stage1/normalization/confirm/preview"' in STAGE1_JS
    assert "preview.validation" in STAGE1_JS
    assert "err?.payload?.issues" in STAGE1_JS
    for action in ("add-fragment", "delete-fragment", "split-fragment", "goto-scene"):
        assert f'data-action="{action}"' in STAGE1_JS
    assert ".normalization-confirm-summary" in STAGE1_CSS
    assert ".normalization-group-bar" in STAGE1_CSS


def test_normalization_history_controls_are_wired() -> None:
    for element_id in (
        "btn_normalization_history",
        "btn_normalization_history_close",
        "normalization_history_panel",
        "normalization_history_list",
        "normalization_history_diff",
    ):
        assert f'id="{element_id}"' in PROJECT_HTML
        assert f'el("{element_id}")' in STAGE1_JS
    assert "История разбора" in PROJECT_HTML
    assert "/stage1/normalization/history" in STAGE1_JS
    assert "/restore" in STAGE1_JS and "/diff" in STAGE1_JS
    # Rollback is confirmed in the app dialog, never with native browser popups.
    assert "restoreNormalizationSnapshot" in STAGE1_JS
    assert "window.confirm" not in STAGE1_JS and "alert(" not in STAGE1_JS
