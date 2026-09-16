// Stage 1 core pipeline: analyze / TTS job / phrase TTS / validate / build
// timeline, plus the "live job status" panel that TTS progress drives.

import { ids, appState, project, live } from "./state.js";
import {
  toast, appendLog, setStepState, formData, assertRequiredPath,
  postJSON, getJSON, isTransientPollingError, pollingBackoffMs,
  optionalRequestJSON, renderEtaCells, updateActionAvailability, confirmDialog, safely,
  syncTtsStyleAvailability,
} from "./core.js";
import { refreshStatus } from "./normalization.js";

let currentTtsJobId = "";
let ttsJobClientStartedAt = null;
let lastTtsEtaJobId = null;
let ttsPollTimer = null;
let ttsPollFailures = 0;

function eventTitle(event) {
  const map = {
    queued: "в очереди",
    started: "старт",
    item_started: "синтез блока",
    item_generated: "блок готов",
    item_skipped: "пропущен",
    item_retrying: "повтор",
    cancelling: "останавливается",
    cancelled: "прервано",
    interrupted: "прервано",
    completed: "завершено",
    failed: "ошибка",
  };
  return map[event] || event || "—";
}

export function renderLive() {
  if (!ids.live_job_status) return;
  ids.live_job_status.dataset.state = live.status || "idle";
  ids.live_job_status.textContent = live.status || "idle";
  ids.live_event.textContent = eventTitle(live.event);
  ids.live_current.textContent = live.current || "—";
  ids.live_retries.textContent = String(live.retries || 0);
  ids.live_done.textContent = `${live.done || 0}/${live.total || 0}`;
  ids.live_error.textContent = live.error ? `${live.error}` : "—";
  ids.live_error.classList.toggle("has-error", Boolean(live.error));
  if (currentTtsJobId !== lastTtsEtaJobId) {
    lastTtsEtaJobId = currentTtsJobId;
    ttsJobClientStartedAt = null;
  }
  if (["queued", "running", "cancelling"].includes(live.status) && !ttsJobClientStartedAt) {
    ttsJobClientStartedAt = Date.now();
  }
  const ttsFraction = live.total > 0 ? Math.max(0, Math.min(1, (live.done || 0) / live.total)) : 0;
  renderEtaCells(ids.live_job_elapsed, ids.live_job_eta, ttsJobClientStartedAt, ttsFraction, live.status);
}

function setLive(patch) {
  Object.assign(live, patch || {});
  renderLive();
}

async function doAnalyze() {
  const f = formData();
  assertRequiredPath(f.input_path, "input_path");
  assertRequiredPath(f.prepared_path, "prepared_path");
  assertRequiredPath(f.raw_output_path, "raw_output_path");
  setStepState(ids.step_analyze, "active");
  appendLog(`Analyze: ${f.input_path}`);
  const result = await postJSON("/stage1/analyze", {
    project,
    input_path: f.input_path,
    output_path: f.prepared_path,
    raw_output_path: f.raw_output_path,
    use_llm: f.use_llm,
  });
  setStepState(ids.step_analyze, "completed");
  toast(`Анализ завершён: ${result.blocks_count} блоков`, "success");
  appendLog(`Analyze done: blocks=${result.blocks_count}, llm=${result.used_llm}`);
  await refreshStatus();
}

async function doTTS() {
  const f = formData();
  assertRequiredPath(f.prepared_path, "prepared_path");
  assertRequiredPath(f.speaker_dir, "speaker_dir");
  setStepState(ids.step_tts, "active");
  if (ids.btn_tts_cancel) {
    ids.btn_tts_cancel.disabled = false;
    ids.btn_tts_cancel.textContent = "Остановить после текущего блока";
  }
  setLive({
    status: "queued", event: "queued",
    current: "—", retries: 0, done: 0, total: 0, error: "",
  });
  appendLog(`TTS job: provider=${f.provider}, model=${f.tts_model || "local"}, gender=${f.gender}, speed=${f.speed}x, dry=${f.dry_run}`);
  const start = await postJSON("/stage1/tts-jobs/start", {
    project,
    prepared_path: f.prepared_path,
    speaker_dir: f.speaker_dir,
    provider: f.provider,
    tts_model: f.tts_model || null,
    tts_style: f.tts_style,
    gender: f.gender,
    voice_name: f.voice_name || null,
    dry_run: f.dry_run,
    speed: f.speed,
  });

  const jobId = String(start.job_id || "").trim();
  if (!jobId) throw new Error("TTS job start failed: empty job_id");
  currentTtsJobId = jobId;
  const statusUrl = String(
    start.status_url || `/stage1/tts-jobs/${encodeURIComponent(jobId)}?project=${encodeURIComponent(project)}`,
  );
  appState.currentTtsJobStatus = String(start.status || "queued").toLowerCase();
  if (ids.btn_tts_cancel) ids.btn_tts_cancel.hidden = false;

  appendLog(`TTS job queued: ${jobId}`);
  let lastMarker = "";
  let retryCount = 0;
  let pollFailures = 0;
  while (true) {
    let state;
    try {
      state = await getJSON(statusUrl);
      pollFailures = 0;
    } catch (err) {
      if (!isTransientPollingError(err)) throw err;
      pollFailures += 1;
      const delay = pollingBackoffMs(pollFailures, 1200);
      appendLog(
        `TTS status временно недоступен; повтор через ${Math.ceil(delay / 1000)} с.`,
        "warn",
      );
      await new Promise((resolve) => setTimeout(resolve, delay));
      continue;
    }
    const total = Number(state.total || 0);
    const done = Number(state.done || 0);
    const event = String(state.event || "");
    appState.currentTtsJobStatus = String(state.status || "running").toLowerCase();
    const currentId = String(state.current_id || "").trim();
    const attempts = Number(state.attempts || 0);
    const marker = `${event}|${done}|${total}|${currentId}|${attempts}`;

    if (total > 0) {
      const pct = Math.max(0, Math.min(100, Math.round((done / total) * 100)));
      ids.progress_bar.style.width = `${pct}%`;
      ids.m_progress.textContent = `${pct}%`;
      ids.speaker_hint.textContent = `TTS: ${done}/${total}`;
    }

    if (marker !== lastMarker) {
      if (event === "item_retrying") retryCount += 1;
      setLive({
        status: String(state.status || "running"),
        event: event || "running",
        current: currentId || "—",
        retries: retryCount,
        done, total,
        error: String(state.last_error || state.error || ""),
      });
      const level = event === "item_retrying" ? "warn" : event === "failed" ? "error" : "info";
      appendLog(`TTS ${event}: ${done}/${total}` + (currentId ? ` (${currentId})` : ""), level);
      lastMarker = marker;
    }

    if (state.status === "completed") {
      const result = state.result || {};
      setStepState(ids.step_tts, "completed");
      setLive({ status: "completed", event: "completed", current: "—", done: total, total, error: "" });
      const gen = result.files_count || state.generated || 0;
      const skip = result.skipped_count || state.skipped || 0;
      toast(`TTS завершён: ${gen} создано, ${skip} пропущено`, "success");
      appendLog(`TTS done: generated=${gen}, skipped=${skip}, voice=${result.voice || state.voice || ""}`);
      currentTtsJobId = "";
      if (ids.btn_tts_cancel) ids.btn_tts_cancel.hidden = true;
      break;
    }
    if (state.status === "cancelled" || state.status === "interrupted") {
      setStepState(ids.step_tts, "active");
      setLive({
        status: state.status, event: state.event || state.status,
        current: "—", done, total, error: "",
      });
      if (ids.btn_tts_cancel) ids.btn_tts_cancel.hidden = true;
      toast("Озвучка остановлена. Готовые WAV сохранены — можно продолжить позже.", "warn");
      appendLog("TTS остановлен; повторный запуск пропустит уже готовые WAV.", "warn");
      break;
    }
    if (state.status === "failed") {
      setStepState(ids.step_tts, "error");
      setLive({
        status: "failed", event: "failed",
        error: String(state.last_error || state.error || "TTS job failed"),
        done, total,
      });
      throw new Error(state.error || "TTS job failed");
    }

    await new Promise((resolve) => setTimeout(resolve, 1200));
  }

  await refreshStatus();
}

async function cancelTTSJob() {
  if (!currentTtsJobId || !["queued", "running", "cancelling"].includes(appState.currentTtsJobStatus)) return;
  try {
    const state = await postJSON(`/stage1/tts-jobs/${encodeURIComponent(currentTtsJobId)}/cancel`, {});
    appState.currentTtsJobStatus = String(state.status || "cancelling").toLowerCase();
    setLive({status: appState.currentTtsJobStatus, event: "cancelling"});
    ids.btn_tts_cancel.disabled = true;
    ids.btn_tts_cancel.textContent = "Останавливается…";
    appendLog("Запрошена остановка TTS. Текущий внешний вызов будет завершён безопасно.", "warn");
  } catch (err) {
    toast(err.message || String(err), "error");
    appendLog(`TTS cancel: ${err.message || err}`, "error");
  }
}

async function clearTTS() {
  const accepted = await confirmDialog({
    title: "Очистить всю озвучку?",
    message: "Будут удалены все текущие файлы закадрового голоса и результаты валидации. Текст сценария и история редакций останутся без изменений.",
    confirmLabel: "Очистить всё",
    cancelLabel: "Отмена",
    danger: true,
  });
  if (!accepted) return;

  const result = await postJSON("/stage1/tts/clear", { project });
  currentTtsJobId = "";
  appState.currentTtsJobStatus = "";
  if (ttsPollTimer) window.clearTimeout(ttsPollTimer);
  ttsPollTimer = null;
  ttsPollFailures = 0;
  ids.progress_bar.style.width = "0%";
  ids.m_progress.textContent = "0%";
  ids.speaker_hint.textContent = "Озвучка очищена — можно запускать TTS через OpenRouter";
  setStepState(ids.step_tts, "idle");
  setStepState(ids.step_validate, "idle");
  setLive({
    status: "idle", event: "—", current: "—",
    retries: 0, done: 0, total: 0, error: "",
  });
  toast(`Озвучка очищена: удалено ${result.files_removed || 0} файлов`, "success");
  appendLog(`TTS cleared: files=${result.files_removed || 0}, manifests=${result.manifests_reset || 0}`);
  await refreshStatus();
}

function restoreTtsControls(state) {
  if (!state || typeof state !== "object") return;
  if (state.prepared_path) ids.prepared_path.value = String(state.prepared_path);
  if (state.speaker_dir) ids.speaker_dir.value = String(state.speaker_dir);
  if (state.provider === "macos") {
    ids.provider.value = "macos";
  } else if (state.tts_model) {
    ids.provider.value = String(state.tts_model);
  }
  if (Object.prototype.hasOwnProperty.call(state, "tts_style") && ids.tts_style) {
    ids.tts_style.value = String(state.tts_style || "");
  }
  syncTtsStyleAvailability();
  if (state.gender) ids.gender.value = String(state.gender);
  if (Object.prototype.hasOwnProperty.call(state, "voice")) {
    ids.voice_name.value = String(state.voice || "");
  }
  if (Object.prototype.hasOwnProperty.call(state, "dry_run")) {
    ids.dry_run.checked = Boolean(state.dry_run);
  }
  const speed = Number(state.speed);
  if (Number.isFinite(speed) && speed > 0) {
    ids.speed.value = String(speed);
    ids.speed_val.textContent = speed.toFixed(2);
  }
}

function renderRestoredTtsJob(state) {
  const total = Number(state.total || 0);
  const done = Number(state.done || 0);
  currentTtsJobId = String(state.id || state.job_id || currentTtsJobId || "");
  appState.currentTtsJobStatus = String(state.status || "").toLowerCase();
  setLive({
    status: appState.currentTtsJobStatus || "idle",
    event: String(state.event || appState.currentTtsJobStatus || "—"),
    current: String(state.current_id || "—"),
    retries: Number(state.retry_count || 0),
    done,
    total,
    error: String(state.last_error || state.error || ""),
  });
  if (total > 0) {
    const pct = Math.max(0, Math.min(100, Math.round((done / total) * 100)));
    ids.progress_bar.style.width = `${pct}%`;
    ids.m_progress.textContent = `${pct}%`;
  }
  const active = ["queued", "running", "cancelling"].includes(appState.currentTtsJobStatus);
  if (ids.btn_tts_cancel) ids.btn_tts_cancel.hidden = !active;
  updateActionAvailability();
  return active;
}

async function pollRestoredTtsJob() {
  if (!currentTtsJobId) return;
  try {
    const state = await getJSON(`/stage1/tts-jobs/${encodeURIComponent(currentTtsJobId)}`);
    ttsPollFailures = 0;
    if (renderRestoredTtsJob(state)) {
      ttsPollTimer = window.setTimeout(pollRestoredTtsJob, 1200);
    } else {
      ttsPollTimer = null;
      if (["cancelled", "interrupted"].includes(appState.currentTtsJobStatus)) {
        appendLog("TTS остановлен; готовые WAV можно продолжить кнопкой «Продолжить озвучку».", "warn");
      }
      await refreshStatus();
    }
  } catch (err) {
    if (!isTransientPollingError(err)) {
      ttsPollTimer = null;
      appendLog(`TTS status: ${err.message || err}`, "warn");
      return;
    }
    ttsPollFailures += 1;
    const delay = pollingBackoffMs(ttsPollFailures, 1200);
    appendLog(
      `TTS status временно недоступен; повтор через ${Math.ceil(delay / 1000)} с.`,
      "warn",
    );
    ttsPollTimer = window.setTimeout(pollRestoredTtsJob, delay);
  }
}

export async function restoreTtsJob() {
  const response = await optionalRequestJSON(
    `/stage1/tts-jobs/status?project=${encodeURIComponent(project)}`,
  );
  if (!response.available || !response.result) return;
  restoreTtsControls(response.result);
  if (renderRestoredTtsJob(response.result) && !ttsPollTimer) {
    ttsPollTimer = window.setTimeout(pollRestoredTtsJob, 500);
  }
}

async function doPhraseTTS() {
  const f = formData();
  assertRequiredPath(f.speaker_dir, "speaker_dir");
  const phrase = (f.phrase_text || "").trim();
  if (!phrase) {
    toast("Введи текст фразы", "error");
    ids.phrase_text.focus();
    return;
  }
  appendLog(`Phrase TTS: provider=${f.provider}, model=${f.tts_model || "local"}, gender=${f.gender}, speed=${f.speed}x, dry=${f.dry_run}`);
  const result = await postJSON("/stage1/tts-phrase", {
    project,
    text: phrase,
    speaker_dir: f.speaker_dir,
    provider: f.provider,
    tts_model: f.tts_model || null,
    tts_style: f.tts_style,
    gender: f.gender,
    voice_name: f.voice_name || null,
    file_name: f.phrase_file_name || null,
    dry_run: f.dry_run,
    speed: f.speed,
  });
  toast("Фраза озвучена", "success");
  appendLog(`Phrase done: file=${result.file}, voice=${result.voice}`);
  if (result.playback_url && ids.phrase_player) {
    ids.phrase_player.src = `${result.playback_url}?t=${Date.now()}`;
    ids.phrase_player.load();
  }
  await refreshStatus();
}

async function doValidate() {
  const f = formData();
  assertRequiredPath(f.prepared_path, "prepared_path");
  assertRequiredPath(f.speaker_dir, "speaker_dir");
  assertRequiredPath(f.manifest_path, "manifest_path");
  setStepState(ids.step_validate, "active");
  appendLog("Validate...");
  const response = await fetch("/stage1/validate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      project,
      prepared_path: f.prepared_path,
      speaker_dir: f.speaker_dir,
      manifest_path: f.manifest_path,
    }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok && response.status !== 422) {
    throw new Error(data.message || data.error || `HTTP ${response.status}`);
  }
  const result = data.result || {};
  if (result.ok) {
    setStepState(ids.step_validate, "completed");
    toast(`Валидация пройдена: ${result.items_count} элементов`, "success");
  } else {
    setStepState(ids.step_validate, "error");
    const errList = (result.errors || []).join("; ");
    toast(`Валидация: ${errList || "есть ошибки"}`, "warn");
  }
  appendLog(`Validate done: ok=${result.ok}, items=${result.items_count}, errors=${(result.errors||[]).length}`);
  await refreshStatus();
}

export async function doBuildTimeline() {
  appendLog("Stage 2: Building timeline...");
  const f = formData();
  const result = await postJSON("/stage2/build", {
    project,
    raw_path: f.raw_output_path,
    speaker_dir: f.speaker_dir,
    fps: 25,
  });
  const { zk, snh, total_duration_sec } = result;
  appendLog(`Timeline built: ЗК=${zk}, Спикеры=${snh}, длит.=${Math.round(total_duration_sec)}с`);

  ids.ts_zk.textContent = zk;
  ids.ts_snh.textContent = snh;
  const m = Math.floor(total_duration_sec / 60);
  const s = Math.round(total_duration_sec % 60);
  ids.ts_dur.textContent = `${m}:${String(s).padStart(2, "0")}`;
  ids.timeline_stats.style.display = "flex";

  appState.timelineReady = true;
  ids.btn_export_xml.disabled = false;
}

if (ids.btn_analyze) ids.btn_analyze.addEventListener("click", () => safely(doAnalyze));
ids.btn_tts.addEventListener("click", () => safely(doTTS));
ids.provider.addEventListener("change", syncTtsStyleAvailability);
if (ids.btn_tts_cancel) ids.btn_tts_cancel.addEventListener("click", cancelTTSJob);
if (ids.btn_tts_clear) ids.btn_tts_clear.addEventListener("click", () => safely(clearTTS));
ids.btn_phrase_tts.addEventListener("click", () => safely(doPhraseTTS));
ids.btn_validate.addEventListener("click", () => safely(doValidate));
ids.btn_run_all.addEventListener("click", () =>
  safely(async () => {
    await doTTS();
    await doValidate();
  })
);
ids.btn_build_timeline.addEventListener("click", () => safely(doBuildTimeline));

ids.speed.addEventListener("input", () => {
  ids.speed_val.textContent = parseFloat(ids.speed.value).toFixed(2);
});
