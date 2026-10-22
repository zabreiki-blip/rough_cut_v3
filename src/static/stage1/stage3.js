// Stage 3: headless media scan (review footage) and the XML export job.

import { ids, appState, project } from "./state.js";
import {
  toast, appendLog, escaped, formatMediaDuration, formatMediaSize,
  renderEtaCells, updateActionAvailability, isTransientPollingError,
  pollingBackoffMs, postJSON, getJSON, optionalRequestJSON, confirmDialog, safely,
} from "./core.js";
import { loadSyncWorkspace } from "./sync.js";

let stage3MediaScanAttempted = false;
let stage3MediaScanning = false;
let currentStage3JobId = "";
let currentStage3JobKind = "";
let stage3JobClientStartedAt = null;
let stage3PollTimer = null;
let stage3PollFailures = 0;

function stage3MediaItems(result) {
  if (Array.isArray(result)) return result;
  if (!result || typeof result !== "object") return [];
  for (const key of ["sources", "items", "files", "media"]) {
    if (Array.isArray(result[key])) return result[key];
  }
  return [];
}

function mediaItemMetadata(item) {
  const probe = item.probe && typeof item.probe === "object" ? item.probe : {};
  const video = item.video && typeof item.video === "object" ? item.video : {};
  const duration = item.duration_sec ?? item.duration_seconds ?? item.duration
    ?? probe.duration_sec ?? probe.duration_seconds ?? probe.duration;
  const size = item.size_bytes ?? item.size ?? probe.size_bytes ?? probe.size;
  const width = item.width ?? video.width ?? probe.width;
  const height = item.height ?? video.height ?? probe.height;
  const fps = item.fps ?? video.fps ?? probe.fps;
  const resolution = width && height ? `${width}×${height}` : "—";
  const fpsText = Number.isFinite(Number(fps)) ? `${Number(fps).toFixed(2)} fps` : "—";
  return { duration, size, resolution, fpsText };
}

function renderStage3Media(result) {
  if (!ids.stage3_media_list) return;
  const items = stage3MediaItems(result);
  appState.stage3MediaCount = items.length;
  if (ids.stage3_media_directory) {
    ids.stage3_media_directory.textContent = result.directory || "Папка проекта не настроена";
  }
  ids.stage3_media_count.textContent = String(items.length);
  ids.stage3_media_message.textContent = items.length > 0
    ? `Папка просканирована. ${items.length} файл(ов) готовы как временные источники.`
    : "В указанной папке поддерживаемые видео-, WAV- или MP3-файлы не найдены.";
  ids.stage3_media_list.innerHTML = items.length === 0
    ? '<div class="empty-state compact">Проверьте путь и наличие видео-, WAV- или MP3-файлов.</div>'
    : items.map((item) => {
      const path = String(item.container_path || item.relative_path || item.path || item.file || item.name || "Без имени");
      const name = String(item.name || path.split(/[\\/]/).pop() || path);
      const metadata = mediaItemMetadata(item);
      return `<article class="stage3-media-item">
        <div class="stage3-media-file">
          <strong title="${escaped(path)}">${escaped(name)}</strong>
          <span title="${escaped(path)}">${escaped(path)}</span>
        </div>
        <dl class="stage3-media-meta">
          <div><dt>Длительность</dt><dd>${formatMediaDuration(metadata.duration)}</dd></div>
          <div><dt>Размер</dt><dd>${formatMediaSize(metadata.size)}</dd></div>
          <div><dt>Кадр</dt><dd>${escaped(metadata.resolution)}</dd></div>
          <div><dt>Частота</dt><dd>${escaped(metadata.fpsText)}</dd></div>
        </dl>
      </article>`;
    }).join("");
  renderStage3SourceMapping(result.mapping || []);
}

function stage3MappingLabel(item) {
  const aliases = (item.sequence_aliases || []).concat(item.speaker_aliases || []);
  return item.source_label || aliases[0] || item.source_id || "Источник";
}

function renderStage3SourceMapping(mapping) {
  if (!ids.stage3_source_mapping) return;
  const items = Array.isArray(mapping) ? mapping : [];
  appState.stage3SourceMapping = items;
  if (items.length === 0) {
    ids.stage3_source_mapping.innerHTML =
      '<div class="empty-state compact">Сначала соберите Stage 2 timeline.</div>';
    return;
  }
  ids.stage3_source_mapping.innerHTML = items.map((item) => {
    const mapped = item.status === "mapped";
    const reason = mapped
      ? (item.media_kind === "audio"
        ? `карточка + звук · до ${formatMediaDuration(item.max_source_out_sec)}`
        : `до ${formatMediaDuration(item.max_source_out_sec)} на исходнике`)
      : (item.reason === "ambiguous_name_match" ? "неоднозначное имя" :
        (item.reason === "media_too_short" ? "просмотровка короче нужного диапазона" :
          "просмотровка не найдена"));
    return `<article class="stage3-map-row" data-state="${mapped ? "mapped" : "unmapped"}">
      <div><strong>${escaped(stage3MappingLabel(item))}</strong><small>${escaped(item.source_id || "")}</small></div>
      <span class="stage3-map-arrow">→</span>
      <div><strong>${escaped(item.media_name || "Нет файла")}</strong><small>${escaped(reason)}</small></div>
    </article>`;
  }).join("");
}

function stage3JobPayload(value) {
  if (!value || typeof value !== "object") return {};
  return value.job && typeof value.job === "object" ? value.job : value;
}

function renderStage3Job(value) {
  const job = stage3JobPayload(value);
  const status = String(job.status || "queued").toLowerCase();
  const progress = Math.max(0, Math.min(100, Number(job.progress || 0)));
  const stage3JobId = String(job.id || job.job_id || currentStage3JobId || "");
  if (stage3JobId && stage3JobId !== currentStage3JobId) {
    stage3JobClientStartedAt = null;
  }
  currentStage3JobId = stage3JobId;
  currentStage3JobKind = String(job.kind || currentStage3JobKind || "");
  appState.stage3JobRunning = ["queued", "running"].includes(status);
  if (appState.stage3JobRunning && !stage3JobClientStartedAt) {
    stage3JobClientStartedAt = Date.now();
  }
  ids.stage3_media_status.dataset.state = status;
  ids.stage3_media_status.textContent = status === "completed"
    ? (currentStage3JobKind === "prepare" ? "XML готов" : "готово")
    : status === "failed" ? "ошибка"
    : status === "interrupted" ? "прервано"
    : status === "queued" ? "в очереди"
    : (currentStage3JobKind === "prepare" ? "сборка…" : "сканирование…");
  if (ids.stage3_job_progress) ids.stage3_job_progress.hidden = false;
  if (ids.stage3_job_message) ids.stage3_job_message.textContent = job.message || "Stage 3";
  if (ids.stage3_job_numbers) ids.stage3_job_numbers.textContent = `${Math.round(progress)}%`;
  if (ids.stage3_progress_bar) ids.stage3_progress_bar.style.width = `${progress}%`;
  if (job.error) ids.stage3_media_message.textContent = job.error;
  renderEtaCells(
    ids.stage3_job_elapsed, ids.stage3_job_eta,
    stage3JobClientStartedAt, Math.max(0, Math.min(1, progress / 100)), status,
  );
  updateActionAvailability();
  return job;
}

function finishStage3Job() {
  appState.stage3JobRunning = false;
  stage3MediaScanning = false;
  stage3PollFailures = 0;
  if (stage3PollTimer) window.clearTimeout(stage3PollTimer);
  stage3PollTimer = null;
  ids.btn_export_xml.textContent = "Export XML";
  updateActionAvailability();
}

async function applyCompletedStage3Job(job, download) {
  const result = job.result || {};
  if (job.kind === "scan") {
    renderStage3Media(result);
    await loadSyncWorkspace(true);
    toast(`Просмотровки найдены: ${stage3MediaItems(result).length}`, "success");
    appendLog(
      `Stage 3 media: найдено ${stage3MediaItems(result).length}; `
      + `Whisper-источников=${Number(result.registered_sources || 0)}; `
      + `directory=${result.directory || "—"}`
    );
    return;
  }
  renderStage3SourceMapping(result.source_mapping || []);
  ids.stage3_media_message.textContent =
    `Сопоставлено источников: ${(result.mapped_source_ids || []).length}; `
    + `плейсхолдеров: ${Number(result.placeholder_segments || 0)}.`;
  appendLog(
    `Timeline XML (${result.format || "nested"}): mapped=${(result.mapped_source_ids || []).join(", ") || "—"}; `
    + `unmapped=${(result.unmapped_source_ids || []).join(", ") || "—"}`
  );
  if (download && result.download_url) {
    toast("XML готов. Начинаю загрузку.", "success");
    window.location.href = result.download_url;
  }
}

async function pollStage3Job(statusUrl, downloadOnComplete) {
  try {
    const job = renderStage3Job(await getJSON(statusUrl));
    stage3PollFailures = 0;
    const status = String(job.status || "").toLowerCase();
    if (status === "completed") {
      finishStage3Job();
      await applyCompletedStage3Job(job, downloadOnComplete);
      return;
    }
    if (["failed", "interrupted"].includes(status)) {
      finishStage3Job();
      const message = job.error || job.message || "Stage 3 завершился с ошибкой";
      ids.stage3_media_message.textContent = message;
      toast(message, status === "failed" ? "error" : "warn");
      appendLog(`Stage 3 ${job.kind || "job"}: ${message}`, "error");
      return;
    }
    stage3PollTimer = window.setTimeout(
      () => pollStage3Job(statusUrl, downloadOnComplete), 1200
    );
  } catch (err) {
    if (!isTransientPollingError(err)) {
      finishStage3Job();
      const message = err.message || String(err);
      ids.stage3_media_message.textContent = message;
      toast(message, "error");
      appendLog(`Stage 3 status: ${message}`, "error");
      return;
    }
    stage3PollFailures += 1;
    const delay = pollingBackoffMs(stage3PollFailures, 1200);
    stage3PollTimer = window.setTimeout(
      () => pollStage3Job(statusUrl, downloadOnComplete), delay
    );
  }
}

async function startStage3Job(kind) {
  if (appState.stage3JobRunning || stage3MediaScanning) return;
  stage3MediaScanAttempted = stage3MediaScanAttempted || kind === "scan";
  stage3MediaScanning = kind === "scan";
  appState.stage3JobRunning = true;
  if (kind === "prepare") ids.btn_export_xml.textContent = "Подготовка XML…";
  updateActionAvailability();
  try {
    const started = await postJSON(`/stage3/jobs/${kind}/start`, { project });
    const jobId = String(started.job_id || started.id || "");
    if (!jobId) throw new Error("Backend не вернул идентификатор Stage 3 job");
    currentStage3JobId = jobId;
    currentStage3JobKind = kind;
    renderStage3Job(started);
    stage3PollTimer = window.setTimeout(
      () => pollStage3Job(started.status_url || `/stage3/jobs/${encodeURIComponent(jobId)}`, kind === "prepare"),
      250,
    );
  } catch (err) {
    finishStage3Job();
    throw err;
  }
}

async function restoreStage3Job() {
  const response = await optionalRequestJSON(
    `/stage3/jobs/status?project=${encodeURIComponent(project)}`
  );
  if (!response.available || !response.result) return false;
  const job = renderStage3Job(response.result);
  const status = String(job.status || "").toLowerCase();
  if (["queued", "running"].includes(status)) {
    const jobId = String(job.id || "");
    stage3PollTimer = window.setTimeout(
      () => pollStage3Job(`/stage3/jobs/${encodeURIComponent(jobId)}`, job.kind === "prepare"),
      250,
    );
    return true;
  }
  if (status === "completed") await applyCompletedStage3Job(job, false);
  finishStage3Job();
  return false;
}

export async function scanStage3MediaOnce() {
  if (stage3MediaScanAttempted) return;
  stage3MediaScanAttempted = true;
  try {
    const restoredActive = await restoreStage3Job();
    if (!restoredActive) await startStage3Job("scan");
  } catch (err) {
    appendLog(`Stage 3 media init: ${err.message || err}`, "warn");
  }
}

async function scanStage3Media() {
  return startStage3Job("scan");
}

// Pre-export gate (docs/UIP_ATOM_AUDIT.md item 4): show what is about to be exported
// and require an explicit confirmation while unchecked fragments or placeholders remain.
async function confirmExportSummary() {
  let summary;
  try {
    const status = await getJSON(`/stage1/status?project=${encodeURIComponent(project)}`);
    summary = status.timeline_sync_summary;
  } catch (err) {
    appendLog(`Export XML: не удалось получить сводку сопоставления: ${err.message || err}`, "warn");
    const proceed = await confirmDialog({
      title: "Сводка сопоставления недоступна",
      message: "Не удалось получить сводку сопоставления. Экспортировать без проверки?",
      confirmLabel: "Экспортировать",
      cancelLabel: "Отмена",
      danger: true,
    });
    if (!proceed) appendLog("Export XML: отменён редактором до запуска", "warn");
    return proceed;
  }
  if (!summary || typeof summary !== "object") return true;
  const count = (key) => Number(summary[key]) || 0;
  const review = count("needs_review");
  const placeholders = count("not_found");
  const warnings = Number.isFinite(appState.lastBuildWarningCount) ? appState.lastBuildWarningCount : null;
  appendLog(
    `Export XML: auto_matched=${count("auto_matched")}, confirmed=${count("confirmed")}, `
    + `needs_review=${review}, not_found=${placeholders}`
    + (warnings === null ? "" : `, build_warnings=${warnings}`),
    review + placeholders > 0 ? "warn" : undefined,
  );
  if (review + placeholders === 0) return true;
  const accepted = await confirmDialog({
    title: "Экспорт XML с непроверенными фрагментами",
    message:
      `Экспортировать черновик: ${review} фрагментов требуют проверки и войдут заглушками, `
      + `${placeholders} не найдено?\n`
      + `Автосопоставлено: ${count("auto_matched")}, подтверждено: ${count("confirmed")}, `
      + `требуют проверки (заглушки): ${review}, не найдено (заглушки): ${placeholders}`
      + (warnings === null ? "" : `, предупреждений сборки: ${warnings}`) + ".",
    confirmLabel: "Экспортировать",
    cancelLabel: "Отмена",
    danger: true,
  });
  if (!accepted) appendLog("Export XML: отменён редактором до запуска", "warn");
  return accepted;
}

export async function exportTimelineXml() {
  if (appState.stage3JobRunning || stage3MediaScanning) return undefined;
  if (!(await confirmExportSummary())) return undefined;
  return startStage3Job("prepare");
}

if (ids.btn_stage3_media_scan) ids.btn_stage3_media_scan.addEventListener("click", () => safely(scanStage3Media));
ids.btn_export_xml.addEventListener("click", () => safely(exportTimelineXml));
