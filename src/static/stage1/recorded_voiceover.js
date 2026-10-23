// Stage 1 recorded voiceover ("Готовая запись"): one shared recording, a range
// per scenario block. The backend (/stage1/recorded-voiceover/*) owns matching,
// publication and revision/generation checks; this module renders the state,
// sends user decisions and drives one shared audio player.
//
// All text that reaches innerHTML goes through escaped(); job/status text is
// assigned via textContent.

import { ids, project } from "./state.js";
import {
  toast, appendLog, requestJSON, getJSON, escaped, safely, readyBlocksSummary,
} from "./core.js";
import { refreshStatus } from "./normalization.js";

export { readyBlocksSummary };

const BASE = "/stage1/recorded-voiceover";
export const POLL_INTERVAL_MS = 2000;
export const SCAN_MIN_INTERVAL_MS = 30000;
const ACTIVE_JOB_STATUSES = ["queued", "running", "cancelling"];

const JOB_STATUS_LABELS = {
  queued: "в очереди",
  running: "идёт сопоставление",
  cancelling: "останавливается",
  completed: "готово",
  failed: "ошибка",
  cancelled: "прервано",
  interrupted: "прервано",
};
const RANGE_STATUS_LABELS = {
  needs_review: "проверить",
  confirmed: "подтверждён",
  missing: "не найден",
};
const SOURCE_LABELS = { recorded: "Диктор", tts: "TTS", none: "нет" };

let rvState = null;
let pollTimer = null;
let lastScanAt = -Infinity;
let stopHandler = null;
let modeChosen = false; // the user picked the mode explicitly: do not auto-switch

export function getRecordedState() {
  return rvState;
}

export function stopRecordedPolling() {
  if (pollTimer !== null && pollTimer !== -1) window.clearTimeout(pollTimer);
  pollTimer = null;
}

// ---------------------------------------------------------------- pure helpers

export function isJobActive(job) {
  return Boolean(job) && ACTIVE_JOB_STATUSES.includes(job.status);
}

export function jobSummaryText(job) {
  if (!job) return "задача не запускалась";
  const label = JOB_STATUS_LABELS[job.status] || job.status || "—";
  const duration = Number(job.transcription_duration_sec || 0);
  const done = Number(job.transcription_processed_sec || 0);
  if (job.status === "running" && duration > 0) {
    return `${label} (${Math.min(100, Math.round((done / duration) * 100))}%)`;
  }
  if (job.status === "failed" || job.status === "interrupted") {
    const detail = job.error || job.message;
    return detail ? `${label}: ${detail}` : label;
  }
  return label;
}

export function blockSourceLabel(block) {
  const source = block && block.publish_preview ? block.publish_preview.voice_source : "none";
  return SOURCE_LABELS[source] || SOURCE_LABELS.none;
}

export function effectiveTakeIndex(range) {
  const takes = (range && range.takes) || [];
  if (!takes.length) return null;
  if (Number.isInteger(range.selected_take) && range.selected_take >= 0 && range.selected_take < takes.length) {
    return range.selected_take;
  }
  for (let index = takes.length - 1; index >= 0; index -= 1) {
    if (!takes[index].partial) return index;
  }
  return takes.length - 1;
}

export function blockBadges(block) {
  const range = block && block.range;
  if (!range) return [];
  const badges = String(range.note || "").split(";").map((part) => part.trim()).filter(Boolean);
  if (!badges.length && (range.takes || []).length > 1) badges.push(`дубли: ${range.takes.length}`);
  return badges;
}

function confirmedRecordedBlocks(state) {
  return ((state && state.blocks) || []).filter((block) => (
    block.range && block.range.status === "confirmed" && block.range.voice_source === "recorded"
    && !block.text_changed
  ));
}

export function canApplyRecording(state) {
  if (!state || !state.scenario_confirmed || state.draft_stale || isJobActive(state.job)) return false;
  return confirmedRecordedBlocks(state).length > 0;
}

export function mixedVolumeWarning(state) {
  const sources = ((state && state.blocks) || []).map((block) => (
    block.publish_preview ? block.publish_preview.voice_source : "none"
  ));
  if (sources.includes("recorded") && sources.includes("tts")) {
    return "В сборке будут и запись диктора, и синтез TTS: громкость и тембр могут заметно отличаться. "
      + "Проверьте на слух стыки блоков или выровняйте уровень в монтаже.";
  }
  return "";
}

export function buildRangePatch(range, inSec, outSec, status) {
  const patch = {};
  const start = Number(inSec);
  const end = Number(outSec);
  if (Number.isFinite(start) && Number.isFinite(end)) {
    if (start < 0) throw new Error("Начало (in) не может быть отрицательным");
    if (end <= start) throw new Error("Конец (out) должен быть больше начала (in): конец меньше или равен началу");
    if (!range || start !== range.source_in_sec || end !== range.source_out_sec) {
      patch.source_in_sec = start;
      patch.source_out_sec = end;
    }
  }
  if (status) patch.status = status;
  return patch;
}

function formatSec(value) {
  const number = Number(value);
  return Number.isFinite(number) ? String(Math.round(number * 100) / 100) : "";
}

export function renderBlocksHtml(state) {
  const blocks = (state && state.blocks) || [];
  if (!blocks.length) return '<tr><td colspan="7" class="muted">Нет озвучиваемых блоков.</td></tr>';
  return blocks.map((block, index) => {
    const range = block.range;
    const uid = escaped(block.block_uid);
    const source = (block.publish_preview && block.publish_preview.voice_source) || "none";
    const hasRange = Boolean(range) && range.status !== "missing" && !block.text_changed;
    const status = block.text_changed
      ? "текст изменён"
      : (range ? (RANGE_STATUS_LABELS[range.status] || range.status) : "нет разметки");
    const badges = blockBadges(block).map((text) => `<span class="rv-badge">${escaped(text)}</span>`);
    if (block.text_changed) badges.push('<span class="rv-badge rv-badge-warn">запись устарела</span>');
    const reason = block.publish_preview ? block.publish_preview.reason : "";
    const bounds = hasRange
      ? `<input type="number" class="rv-time" data-rv-field="in" step="0.01" min="0" value="${escaped(formatSec(range.source_in_sec))}" aria-label="in">`
        + ' — '
        + `<input type="number" class="rv-time" data-rv-field="out" step="0.01" min="0" value="${escaped(formatSec(range.source_out_sec))}" aria-label="out">`
        + ' <button type="button" class="btn btn-sm" data-rv-action="play-range">▶</button>'
      : '<span class="muted">—</span>';
    const buttons = [];
    if (hasRange) buttons.push('<button type="button" class="btn btn-sm btn-primary" data-rv-action="confirm">Подтвердить</button>');
    if (range && range.voice_source === "tts" && range.status !== "missing" && !block.text_changed) {
      buttons.push('<button type="button" class="btn btn-sm" data-rv-action="use-recorded">Использовать запись</button>');
    } else {
      const disabled = block.tts_available ? "" : ' disabled title="Для блока нет TTS-аудио"';
      buttons.push(`<button type="button" class="btn btn-sm" data-rv-action="use-tts"${disabled}>Использовать TTS</button>`);
    }
    if (source !== "none" || (range && range.voice_source !== "none")) {
      buttons.push('<button type="button" class="btn btn-sm" data-rv-action="use-none">Без озвучки</button>');
    }
    let html = `<tr class="rv-row" data-uid="${uid}" data-recording="${escaped(range ? range.recording_id : "")}">`
      + `<td>${index + 1}</td>`
      + `<td class="rv-text">${escaped(block.display_text)}</td>`
      + `<td class="rv-bounds">${bounds}</td>`
      + `<td><span class="rv-status" data-status="${escaped(range ? range.status : "none")}">${escaped(status)}</span></td>`
      + `<td><span class="rv-source" data-source="${escaped(source)}" title="${escaped(reason)}">${escaped(blockSourceLabel(block))}</span></td>`
      + `<td>${badges.join(" ")}</td>`
      + `<td class="rv-actions">${buttons.join(" ")}</td></tr>`;
    const takes = hasRange ? (range.takes || []) : [];
    if (takes.length > 1) {
      const chosen = effectiveTakeIndex(range);
      const items = takes.map((take, takeIndex) => (
        `<li class="rv-take${takeIndex === chosen ? " rv-take-selected" : ""}">`
        + `#${takeIndex + 1}: ${escaped(formatSec(take.source_in_sec))}–${escaped(formatSec(take.source_out_sec))} с`
        + (take.partial ? ' <span class="rv-badge">частичный дубль</span>' : "")
        + (takeIndex === chosen ? ' <strong>выбран</strong>' : "")
        + ` <button type="button" class="btn btn-sm" data-rv-action="play-take" data-take-index="${takeIndex}">▶ Прослушать</button>`
        + ` <button type="button" class="btn btn-sm" data-rv-action="select-take" data-take-index="${takeIndex}">Выбрать</button>`
        + "</li>"
      )).join("");
      html += `<tr class="rv-takes-row" data-uid="${uid}"><td></td><td colspan="6"><details><summary>Дубли блока (${takes.length})</summary><ul class="rv-takes">${items}</ul></details></td></tr>`;
    }
    return html;
  }).join("");
}

// -------------------------------------------------------------------- rendering

function setText(element, text) {
  if (element) element.textContent = text;
}

function showMessage(text, level) {
  setText(ids.rv_message, text);
  if (ids.rv_message) ids.rv_message.hidden = !text;
  if (text) {
    toast(text, level || "info");
    appendLog(`Запись диктора: ${text}`, level === "error" || level === "warn");
  }
}

function renderJob(job) {
  setText(ids.rv_job_status, jobSummaryText(job));
  if (ids.rv_job_status) ids.rv_job_status.dataset.state = job ? job.status : "idle";
  if (ids.btn_rv_cancel) ids.btn_rv_cancel.hidden = !isJobActive(job);
}

function renderApply(state) {
  const can = canApplyRecording(state);
  if (ids.btn_rv_apply) ids.btn_rv_apply.disabled = !can;
  const confirmed = confirmedRecordedBlocks(state).length;
  setText(
    ids.rv_apply_hint,
    can
      ? `Будут опубликованы подтверждённые блоки: ${confirmed}; остальные блоки уйдут в TTS (если есть озвучка) или останутся без озвучки.`
      : "Подтвердите хотя бы один блок: публикация идёт по блокам, остальные уйдут в TTS или останутся без озвучки.",
  );
  const warning = mixedVolumeWarning(state);
  setText(ids.rv_loudness_warning, warning);
  if (ids.rv_loudness_warning) ids.rv_loudness_warning.hidden = !warning;
}

function renderAll() {
  const state = rvState;
  if (!state) return;
  const recordings = state.recordings || [];
  setText(
    ids.rv_recordings,
    recordings.length
      ? `Записей: ${recordings.length}. ${state.published && state.published.exists ? "Публикация есть." : "Не применено."}`
      : "Файл записи не выбран.",
  );
  if (ids.btn_rv_align) ids.btn_rv_align.disabled = !recordings.length || isJobActive(state.job) || !state.scenario_confirmed;
  if (ids.rv_blocks) ids.rv_blocks.innerHTML = renderBlocksHtml(state);
  if (state.draft_stale) {
    setText(ids.rv_notice, "Разметка относится к другой редакции сценария — запустите сопоставление заново.");
    if (ids.rv_notice) ids.rv_notice.hidden = false;
  }
  renderJob(state.job);
  renderApply(state);
}

// ----------------------------------------------------------------------- state

function schedulePoll(jobId) {
  if (pollTimer !== null) return;
  pollTimer = window.setTimeout(() => pollJob(jobId), POLL_INTERVAL_MS);
}

async function pollJob(jobId) {
  pollTimer = null;
  let job;
  try {
    job = await getJSON(`${BASE}/jobs/${encodeURIComponent(jobId)}?project=${encodeURIComponent(project)}`);
  } catch (err) {
    appendLog(`Запись диктора: статус задачи недоступен (${err.message || err})`, true);
    schedulePoll(jobId);
    return;
  }
  if (rvState) rvState.job = job;
  renderJob(job);
  if (isJobActive(job)) {
    schedulePoll(jobId);
    return;
  }
  await loadRecordedState();
  refreshStatus().catch((err) => appendLog(`Status refresh: ${err.message || err}`, true));
  if (job.status === "completed") showMessage("Сопоставление записи со сценарием завершено — проверьте блоки.", "success");
  else if (job.status === "failed") showMessage(`Сопоставление не удалось: ${job.error || job.message || "ошибка"}`, "error");
}

export function setRecordedState(state) {
  rvState = state;
  renderAll();
  if (state && isJobActive(state.job)) schedulePoll(state.job.id);
}

export async function loadRecordedState() {
  const state = await getJSON(`${BASE}?project=${encodeURIComponent(project)}`);
  setRecordedState(state);
  return state;
}

export async function restoreRecordedJob() {
  const state = await loadRecordedState();
  if (!modeChosen && (state.recordings || []).length) setVoiceMode("recorded");
  if (state.job && isJobActive(state.job)) {
    setText(ids.rv_notice, "Сопоставление записи продолжается.");
    if (ids.rv_notice) ids.rv_notice.hidden = false;
  }
  return state;
}

export async function scanRecordedInbox({ now = Date.now } = {}) {
  const stamp = now();
  if (stamp - lastScanAt < SCAN_MIN_INTERVAL_MS) return null;
  lastScanAt = stamp;
  const payload = { project };
  if (rvState && rvState.scenario_revision_id) payload.expected_revision_id = rvState.scenario_revision_id;
  const result = await requestJSON(`${BASE}/scan`, "POST", payload);
  if (result.state) setRecordedState(result.state);
  if (result.job && isJobActive(result.job)) {
    setText(ids.rv_notice, "Найдена запись диктора — идёт сопоставление");
    if (ids.rv_notice) ids.rv_notice.hidden = false;
    setVoiceMode("recorded");
  } else if (result.message) {
    setText(ids.rv_notice, result.message);
    if (ids.rv_notice) ids.rv_notice.hidden = false;
  }
  return result;
}

// ------------------------------------------------------------------- mutations

function requireState() {
  if (!rvState) throw new Error("Состояние записи диктора не загружено");
  return rvState;
}

function guard() {
  const state = requireState();
  return {
    project,
    expected_revision_id: state.scenario_revision_id,
    expected_generation: state.generation,
  };
}

const STALE_PATTERN = /revision changed|generation mismatch|another scenario revision/i;

export function conflictMessage(error) {
  const raw = String((error && error.message) || "");
  if (STALE_PATTERN.test(raw)) {
    return "Данные изменились: сценарий переключён на другую редакцию или разметка обновлена в другом окне. "
      + "Состояние перезагружено — повторите действие.";
  }
  return `Действие отклонено: ${raw || "конфликт"}. Состояние перезагружено.`;
}

async function mutate(action) {
  try {
    await action();
  } catch (err) {
    if (err && err.status === 409) {
      showMessage(conflictMessage(err), "warn");
      await loadRecordedState().catch((reloadError) => appendLog(`Запись диктора: ${reloadError.message || reloadError}`, true));
      return false;
    }
    throw err;
  }
  await loadRecordedState();
  return true;
}

function rangeOf(uid) {
  const block = ((rvState && rvState.blocks) || []).find((item) => item.block_uid === uid);
  return block ? block.range : null;
}

export function confirmBlock(uid, { inSec, outSec } = {}) {
  return mutate(async () => {
    const range = rangeOf(uid);
    const patch = buildRangePatch(range, inSec === undefined ? NaN : inSec, outSec === undefined ? NaN : outSec, "confirmed");
    await requestJSON(`${BASE}/ranges/${encodeURIComponent(uid)}`, "PATCH", { ...guard(), ...patch });
  });
}

export function selectTake(uid, index) {
  return mutate(async () => {
    await requestJSON(`${BASE}/ranges/${encodeURIComponent(uid)}`, "PATCH", { ...guard(), selected_take: index });
  });
}

export function setBlockVoiceSource(uid, source) {
  return mutate(async () => {
    await requestJSON(`${BASE}/blocks/${encodeURIComponent(uid)}/voice-source`, "PATCH", { ...guard(), voice_source: source });
  });
}

// Short summary of what the publication will do, per block: Диктор / TTS / нет.
export function applySummaryText(state) {
  const counts = { recorded: 0, tts: 0, none: 0 };
  for (const block of (state && state.blocks) || []) {
    const source = block.publish_preview ? block.publish_preview.voice_source : "none";
    counts[source in counts ? source : "none"] += 1;
  }
  return `${SOURCE_LABELS.recorded}: ${counts.recorded}, ${SOURCE_LABELS.tts}: ${counts.tts}, ${SOURCE_LABELS.none}: ${counts.none}`;
}

function askConfirm(text) {
  const ask = typeof globalThis.confirm === "function" ? globalThis.confirm.bind(globalThis) : null;
  return ask ? Boolean(ask(text)) : true; // no dialogs outside of a browser
}

export async function applyRecording() {
  if (!askConfirm(`Применить запись диктора к редакции?\n${applySummaryText(rvState)}`)) return false;
  let summary = null;
  const ok = await mutate(async () => {
    summary = await requestJSON(`${BASE}/apply`, "POST", guard());
  });
  if (ok) {
    refreshStatus().catch((err) => appendLog(`Status refresh: ${err.message || err}`, true));
    showMessage("Запись применена к редакции. Блоки без записи озвучены TTS или остались без озвучки.", "success");
  }
  return ok && summary !== null;
}

export async function startAlignment() {
  const state = requireState();
  return mutate(async () => {
    await requestJSON(`${BASE}/jobs/start`, "POST", { project, expected_revision_id: state.scenario_revision_id });
  });
}

export async function cancelAlignment() {
  const job = rvState && rvState.job;
  if (!isJobActive(job)) return;
  await requestJSON(`${BASE}/jobs/${encodeURIComponent(job.id)}/cancel`, "POST", { project });
  await loadRecordedState();
}

export async function uploadRecording(file) {
  if (!file) throw new Error("Выберите файл записи (WAV или MP3)");
  const form = new FormData();
  form.append("project", project);
  form.append("audio", file);
  const response = await fetch(`${BASE}/source`, { method: "POST", body: form });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) {
    throw new Error(data.message || data.error || `HTTP ${response.status}`);
  }
  const result = data.result || {};
  if (result.state) setRecordedState(result.state);
  showMessage(`Запись загружена: ${file.name}. Нажмите «Сопоставить со сценарием».`, "success");
  return result;
}

// ---------------------------------------------------------------------- player

export function playRange(inSec, outSec, recordingId) {
  const player = ids.rv_player;
  if (!player) return;
  const recording = recordingId
    || (rvState && rvState.recordings && rvState.recordings[0] && rvState.recordings[0].id)
    || "";
  const src = `${BASE}/audio?project=${encodeURIComponent(project)}&recording_id=${encodeURIComponent(recording)}`;
  if (player.dataset.rvSrc !== src) {
    player.src = src;
    player.dataset.rvSrc = src;
  }
  if (stopHandler) player.removeEventListener("timeupdate", stopHandler);
  stopHandler = () => {
    if (player.currentTime >= outSec) {
      player.pause();
      player.removeEventListener("timeupdate", stopHandler);
      stopHandler = null;
    }
  };
  player.addEventListener("timeupdate", stopHandler);
  player.currentTime = inSec;
  // Metadata may not be loaded yet: the browser then ignores the seek above.
  player.addEventListener("loadedmetadata", () => { player.currentTime = inSec; }, { once: true });
  const started = player.play();
  if (started && typeof started.catch === "function") {
    started.catch((err) => appendLog(`Проигрыватель: ${err.message || err}`, true));
  }
}

// ----------------------------------------------------------------- mode switch

export function setVoiceMode(mode, { user = false } = {}) {
  if (user) modeChosen = true;
  const recorded = mode === "recorded";
  if (ids.tts_mode_panel) ids.tts_mode_panel.hidden = recorded;
  if (ids.recorded_panel) ids.recorded_panel.hidden = !recorded;
  if (ids.voice_mode_tts) {
    ids.voice_mode_tts.setAttribute("aria-pressed", String(!recorded));
    ids.voice_mode_tts.classList.toggle("active", !recorded);
  }
  if (ids.voice_mode_recorded) {
    ids.voice_mode_recorded.setAttribute("aria-pressed", String(recorded));
    ids.voice_mode_recorded.classList.toggle("active", recorded);
  }
}

export function restoreVoiceMode() {
  setVoiceMode("tts");
}

// -------------------------------------------------------------------- DOM wiring

function rowInputs(row) {
  const read = (field) => {
    const input = row.querySelector(`[data-rv-field="${field}"]`);
    return input && input.value !== "" ? Number(input.value) : NaN;
  };
  return { inSec: read("in"), outSec: read("out") };
}

function onBlocksClick(event) {
  const button = event.target.closest ? event.target.closest("[data-rv-action]") : null;
  if (!button) return;
  const row = button.closest("tr");
  const uid = row && row.dataset.uid;
  if (!uid) return;
  const action = button.dataset.rvAction;
  const range = rangeOf(uid);
  const takeIndex = Number(button.dataset.takeIndex);
  if (action === "play-range") {
    const { inSec, outSec } = rowInputs(row);
    if (Number.isFinite(inSec) && Number.isFinite(outSec)) playRange(inSec, outSec, range && range.recording_id);
  } else if (action === "play-take" && range && range.takes[takeIndex]) {
    const take = range.takes[takeIndex];
    playRange(take.source_in_sec, take.source_out_sec, range.recording_id);
  } else if (action === "select-take") {
    safely(() => selectTake(uid, takeIndex));
  } else if (action === "confirm") {
    safely(() => confirmBlock(uid, rowInputs(row)));
  } else if (action === "use-tts") {
    safely(() => setBlockVoiceSource(uid, "tts"));
  } else if (action === "use-recorded") {
    safely(() => setBlockVoiceSource(uid, "recorded"));
  } else if (action === "use-none") {
    safely(() => setBlockVoiceSource(uid, "none"));
  }
}

if (ids.rv_blocks) ids.rv_blocks.addEventListener("click", onBlocksClick);
if (ids.voice_mode_tts) ids.voice_mode_tts.addEventListener("click", () => setVoiceMode("tts", { user: true }));
if (ids.voice_mode_recorded) {
  ids.voice_mode_recorded.addEventListener("click", () => {
    setVoiceMode("recorded", { user: true });
    loadRecordedState().catch((err) => appendLog(`Запись диктора: ${err.message || err}`, true));
  });
}
if (ids.btn_rv_upload) {
  ids.btn_rv_upload.addEventListener("click", () => safely(async () => {
    const file = ids.rv_file && ids.rv_file.files && ids.rv_file.files[0];
    await uploadRecording(file);
  }));
}
if (ids.btn_rv_scan) {
  ids.btn_rv_scan.addEventListener("click", () => safely(async () => {
    lastScanAt = -Infinity;
    const result = await scanRecordedInbox();
    if (result && !result.job) showMessage("Новых записей в ЗАГРУЗИТЬ/Закадр не найдено.", "info");
  }));
}
if (ids.btn_rv_align) ids.btn_rv_align.addEventListener("click", () => safely(startAlignment));
if (ids.btn_rv_cancel) ids.btn_rv_cancel.addEventListener("click", () => safely(cancelAlignment));
if (ids.btn_rv_apply) ids.btn_rv_apply.addEventListener("click", () => safely(applyRecording));
