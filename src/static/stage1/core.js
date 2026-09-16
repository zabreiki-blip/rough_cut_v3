// Generic Stage 1 helpers shared by every domain module: DOM/log/toast
// utilities, HTTP wrappers, formatting, the confirm dialog and the
// busy/action-availability reconciliation that touches every button.

import { ids, toastContainer, appState } from "./state.js";

export function now() {
  return new Date().toLocaleTimeString("ru-RU");
}

export function toast(message, type) {
  if (!toastContainer) return;
  const t = document.createElement("div");
  t.className = "toast " + (type || "");
  t.textContent = message;
  toastContainer.appendChild(t);
  setTimeout(() => {
    t.style.opacity = "0";
    t.style.transform = "translateX(20px)";
    t.style.transition = "all 200ms";
    setTimeout(() => t.remove(), 200);
  }, 3500);
}

export function appendLog(message, level) {
  const resolvedLevel = level === true ? "error" : level;
  const tag = resolvedLevel === "error" ? "ERR" : resolvedLevel === "warn" ? "WARN" : "INFO";
  const line = `[${now()}] [${tag}] ${message}`;
  ids.log_box.textContent += `${line}\n`;
  ids.log_box.scrollTop = ids.log_box.scrollHeight;
  if (resolvedLevel === "error") {
    ids.log_box.classList.add("log-error");
    setTimeout(() => ids.log_box.classList.remove("log-error"), 500);
  }
}

export function setStepState(stepEl, state) {
  if (stepEl) stepEl.dataset.state = state;
}

let busy = false;

export function isBusy() {
  return busy;
}

export function setBusy(state) {
  busy = state;
  [ids.btn_analyze, ids.btn_tts, ids.btn_tts_clear, ids.btn_phrase_tts, ids.btn_validate, ids.btn_run_all, ids.btn_build_timeline,
    ids.btn_sync_align, ids.btn_export_xml,
    ids.btn_normalize, ids.btn_save_normalization, ids.btn_confirm_normalization, ids.btn_add_normalization_block,
    ids.btn_reset_normalization,
    ids.btn_add_memory, ids.btn_stage3_media_scan, ids.btn_import_revision].forEach((b) => {
    if (b) b.disabled = state;
  });
  document.querySelectorAll("[data-action='confirm-sync-match']").forEach((button) => {
    button.disabled = state;
  });
  if (!state) updateActionAvailability();
}

function isNormalizationConfirmed() {
  return Boolean(appState.normalizationEligible && appState.normalization && String(appState.normalization.status || "").toLowerCase() === "confirmed");
}

export function updateActionAvailability() {
  if (busy) return;
  const hasDraft = Boolean(
    appState.normalization && Array.isArray(appState.normalization.blocks) && appState.normalization.blocks.length > 0
  );
  const confirmed = isNormalizationConfirmed();
  const blocked = !confirmed;
  if (ids.btn_save_normalization) ids.btn_save_normalization.disabled = !hasDraft || !appState.normalizationDirty;
  if (ids.btn_reset_normalization) ids.btn_reset_normalization.disabled = !hasDraft;
  if (ids.btn_confirm_normalization) {
    ids.btn_confirm_normalization.disabled = !hasDraft || (confirmed && !appState.normalizationDirty);
  }
  if (ids.btn_add_normalization_block) ids.btn_add_normalization_block.disabled = !hasDraft;
  [ids.btn_tts, ids.btn_validate, ids.btn_run_all, ids.btn_build_timeline].forEach((button) => {
    if (button) button.disabled = blocked;
  });
  if (ids.btn_tts && ["queued", "running", "cancelling"].includes(appState.currentTtsJobStatus)) {
    ids.btn_tts.disabled = true;
  }
  if (ids.btn_tts_clear) {
    ids.btn_tts_clear.disabled = ["queued", "running", "cancelling"].includes(appState.currentTtsJobStatus);
  }
  if (ids.btn_sync_align) {
    ids.btn_sync_align.disabled = blocked || appState.syncJobRunning
      || appState.syncFeatureAvailable !== true || appState.syncSources.length === 0;
    ids.btn_sync_align.textContent = ["interrupted", "cancelled", "failed"].includes(appState.currentSyncJobStatus)
      ? "Продолжить сопоставление"
      : "Найти СНХ автоматически";
  }
  if (ids.btn_sync_cancel) ids.btn_sync_cancel.hidden = !appState.syncJobRunning;
  if (ids.sync_whisper_model) {
    ids.sync_whisper_model.disabled = appState.syncJobRunning
      || (ids.sync_asr_engine?.value || "local") === "cloud";
  }
  if (ids.sync_asr_engine) ids.sync_asr_engine.disabled = appState.syncJobRunning;
  if (ids.btn_tts_cancel) {
    ids.btn_tts_cancel.hidden = !["queued", "running", "cancelling"].includes(appState.currentTtsJobStatus);
  }
  if (ids.btn_tts) {
    ids.btn_tts.textContent = ["interrupted", "cancelled", "failed"].includes(appState.currentTtsJobStatus)
      ? "Продолжить озвучку"
      : "TTS";
  }
  document.querySelectorAll("[data-action='confirm-sync-match']").forEach((button) => {
    button.disabled = blocked || appState.syncJobRunning;
  });
  if (ids.voiceover_lock) ids.voiceover_lock.hidden = !blocked;
  if (ids.assembly_lock) ids.assembly_lock.hidden = !blocked;
  if (ids.assembly_gate_message) ids.assembly_gate_message.hidden = !blocked;
  if (ids.btn_stage3_media_scan) {
    ids.btn_stage3_media_scan.disabled = appState.stage3JobRunning;
  }
  if (blocked && ids.btn_export_xml) {
    ids.btn_export_xml.disabled = true;
  } else if (ids.btn_export_xml) {
    ids.btn_export_xml.disabled = !appState.timelineReady || appState.stage3JobRunning;
  }
}

export function formData() {
  const ttsSelection = ids.provider.value.trim();
  const isOpenRouter = ttsSelection !== "macos";
  return {
    input_path: ids.input_path.value.trim(),
    prepared_path: ids.prepared_path.value.trim(),
    raw_output_path: ids.raw_output_path.value.trim(),
    speaker_dir: ids.speaker_dir.value.trim(),
    manifest_path: ids.manifest_path.value.trim(),
    provider: isOpenRouter ? "openrouter" : "macos",
    tts_model: isOpenRouter ? ttsSelection : "",
    tts_style: ids.tts_style && isOpenRouter ? ids.tts_style.value.trim() : "",
    gender: ids.gender.value.trim(),
    voice_name: ids.voice_name.value.trim(),
    phrase_text: ids.phrase_text.value,
    phrase_file_name: ids.phrase_file_name.value.trim(),
    use_llm: ids.use_llm ? ids.use_llm.checked : false,
    dry_run: ids.dry_run.checked,
    speed: parseFloat(ids.speed.value),
  };
}

export function syncTtsStyleAvailability() {
  if (!ids.provider || !ids.tts_style) return;
  const enabled = ids.provider.value !== "macos";
  ids.tts_style.disabled = !enabled;
  if (ids.tts_style_field) ids.tts_style_field.classList.toggle("is-disabled", !enabled);
  if (ids.tts_style_hint) {
    const isMai = ids.provider.value === "microsoft/mai-voice-2";
    ids.tts_style_hint.textContent = !enabled
      ? "Локальный macOS TTS не поддерживает режиссёрские инструкции."
      : isMai
        ? "MAI переводит описание в ближайший Azure-стиль; можно явно указать style:cheerful."
        : "Gemini получает описание как режиссёрскую инструкцию к каждому блоку."
  }
}

export function isLikelySafePath(pathValue) {
  if (!pathValue) return false;
  if (pathValue.includes("\0")) return false;
  if (pathValue.includes("..")) return false;
  return true;
}

export function assertRequiredPath(pathValue, label) {
  const value = (pathValue || "").trim();
  if (!value) throw new Error(`${label} is required`);
  if (!isLikelySafePath(value)) throw new Error(`${label} looks invalid: ${value}`);
}

export const FETCH_TIMEOUT_MS = 30000;

async function fetchWithTimeout(url, options = {}, timeoutMs = FETCH_TIMEOUT_MS) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } catch (error) {
    if (controller.signal.aborted) {
      const timeout = new Error("Превышено время ожидания ответа сервера");
      timeout.status = 408;
      throw timeout;
    }
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

export async function postJSON(url, payload) {
  const response = await fetchWithTimeout(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || JSON.stringify(data));
  }
  return data.result || {};
}

export async function getJSON(url) {
  const response = await fetchWithTimeout(url);
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) {
    const error = new Error(data.error || JSON.stringify(data));
    error.status = response.status;
    throw error;
  }
  return data.result || {};
}

export async function requestJSON(url, method, payload) {
  const options = { method, headers: { "Content-Type": "application/json" } };
  if (payload !== undefined) options.body = JSON.stringify(payload);
  const response = await fetchWithTimeout(url, options);
  const raw = await response.text();
  let data = {};
  try {
    data = raw ? JSON.parse(raw) : {};
  } catch (_err) {
    data = {};
  }
  if (!response.ok || data.ok === false) {
    const plainText = raw && !raw.trimStart().startsWith("<") ? raw.trim().slice(0, 500) : "";
    const detail = data.message || data.error || plainText;
    throw new Error(detail || `HTTP ${response.status}: сервер прервал запрос без JSON-ответа`);
  }
  if (raw && Object.keys(data).length === 0) {
    throw new Error(`HTTP ${response.status}: сервер вернул некорректный JSON`);
  }
  return data.result !== undefined ? data.result : data;
}

export async function optionalRequestJSON(url, method, payload) {
  const options = { method: method || "GET", headers: { "Content-Type": "application/json" } };
  if (payload !== undefined) options.body = JSON.stringify(payload);
  const response = await fetchWithTimeout(url, options);
  if (response.status === 404) return { available: false, result: null };
  const raw = await response.text();
  let data = {};
  try {
    data = raw ? JSON.parse(raw) : {};
  } catch (_err) {
    data = {};
  }
  if (!response.ok || data.ok === false) {
    const plainText = raw && !raw.trimStart().startsWith("<") ? raw.trim().slice(0, 500) : "";
    const error = new Error(data.message || data.error || plainText || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return {
    available: true,
    result: data.result !== undefined ? data.result : data,
  };
}

const MAX_POLL_BACKOFF_MS = 10000;

export function isTransientPollingError(error) {
  const status = Number((error && error.status) || 0);
  return status === 0 || status === 408 || status === 429 || status >= 500;
}

export function pollingBackoffMs(failures, baseDelay) {
  const exponent = Math.min(4, Math.max(0, Number(failures || 1) - 1));
  return Math.min(MAX_POLL_BACKOFF_MS, baseDelay * (2 ** exponent));
}

export const blockTypeLabels = {
  voiceover: "Закадр", sync: "Синхрон", host: "Ведущий", life: "Лайф",
  technical: "Технический", other: "Прочее", unknown: "Не определён",
};
export const sourceLabels = {
  builtin_rule: "правило", rule: "правило", memory: "память",
  llm: "LLM", manual: "исправлено вручную", correction: "ручное обучение",
  unknown: "не определено",
};

export function escaped(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#039;");
}

export function computeEtaSeconds(startedAtMs, fraction) {
  if (!Number.isFinite(startedAtMs)) return { elapsedSec: 0, etaSec: 0 };
  const elapsedSec = Math.max(0, (Date.now() - startedAtMs) / 1000);
  const etaSec = elapsedSec > 0 && fraction >= 0.05 && fraction < 1
    ? elapsedSec * (1 - fraction) / fraction
    : 0;
  return { elapsedSec, etaSec };
}

export function formatMediaDuration(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = Math.floor(seconds % 60);
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`
    : `${minutes}:${String(rest).padStart(2, "0")}`;
}

export function formatMediaSize(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} Б`;
  const units = ["КБ", "МБ", "ГБ", "ТБ"];
  let size = bytes / 1024;
  let unit = units[0];
  for (let index = 1; index < units.length && size >= 1024; index += 1) {
    size /= 1024;
    unit = units[index];
  }
  return `${size >= 10 ? size.toFixed(1) : size.toFixed(2)} ${unit}`;
}

export function renderEtaCells(elapsedEl, etaEl, startedAtMs, fraction, status) {
  const { elapsedSec, etaSec } = computeEtaSeconds(startedAtMs, fraction);
  if (elapsedEl) elapsedEl.textContent = elapsedSec > 0 ? formatMediaDuration(elapsedSec) : "—";
  if (etaEl) {
    etaEl.textContent = status === "completed" ? "готово"
      : (fraction < 0.05 ? "рассчитывается"
        : (etaSec > 0 ? `≈ ${formatMediaDuration(etaSec)}` : "рассчитывается"));
  }
}

export function confirmDialog({ title, message, confirmLabel, cancelLabel, danger }) {
  const dialog = ids.confirm_dialog;
  const acceptBtn = ids.btn_confirm_dialog_accept;
  const cancelBtn = ids.btn_confirm_dialog_cancel;
  ids.confirm_dialog_title.textContent = title || "";
  ids.confirm_dialog_message.textContent = message || "";
  acceptBtn.textContent = confirmLabel || "Подтвердить";
  cancelBtn.textContent = cancelLabel || "Отмена";
  acceptBtn.classList.toggle("danger", Boolean(danger));
  acceptBtn.disabled = false;
  const previouslyFocused = document.activeElement;
  return new Promise((resolve) => {
    let settled = false;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      dialog.removeEventListener("cancel", onCancel);
      dialog.removeEventListener("click", onBackdropClick);
      dialog.removeEventListener("keydown", onKeydown);
      acceptBtn.onclick = null;
      cancelBtn.onclick = null;
      dialog.close();
      if (previouslyFocused && typeof previouslyFocused.focus === "function") {
        previouslyFocused.focus();
      }
      resolve(result);
    };
    const onCancel = (event) => { event.preventDefault(); finish(false); };
    const onBackdropClick = (event) => { if (event.target === dialog) finish(false); };
    const onKeydown = (event) => {
      if (event.key !== "Tab") return;
      const focusable = [cancelBtn, acceptBtn];
      const currentIndex = focusable.indexOf(document.activeElement);
      event.preventDefault();
      const nextIndex = event.shiftKey
        ? (currentIndex <= 0 ? focusable.length - 1 : currentIndex - 1)
        : (currentIndex === -1 || currentIndex === focusable.length - 1 ? 0 : currentIndex + 1);
      focusable[nextIndex].focus();
    };
    cancelBtn.onclick = () => finish(false);
    acceptBtn.onclick = () => {
      if (acceptBtn.disabled) return;
      acceptBtn.disabled = true;
      finish(true);
    };
    dialog.addEventListener("cancel", onCancel);
    dialog.addEventListener("click", onBackdropClick);
    dialog.addEventListener("keydown", onKeydown);
    dialog.showModal();
    acceptBtn.focus();
  });
}

export async function safely(fn) {
  if (busy) return;
  setBusy(true);
  try {
    await fn();
  } catch (err) {
    const msg = err.message || String(err);
    toast(msg, "error");
    appendLog(`ERROR: ${msg}`, true);
    if (msg.includes("macOS TTS unavailable") || msg.includes("command 'say'")) {
      appendLog("Подсказка: выбери provider=openrouter или настрой macOS-коннектор.", true);
    }
  } finally {
    setBusy(false);
  }
}

export function openTab(tabName) {
  document.querySelectorAll(".workspace-tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.tab === tabName);
  });
  document.querySelectorAll("[data-tab-panel]").forEach((panel) => {
    const active = panel.dataset.tabPanel === tabName;
    panel.classList.toggle("active", active);
    panel.hidden = !active;
  });
}
