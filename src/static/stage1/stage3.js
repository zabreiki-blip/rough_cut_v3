// Stage 3: headless media scan (review footage) and the XML export job.

import { ids, appState, project } from "./state.js";
import {
  toast, appendLog, escaped, formatMediaDuration, formatMediaSize,
  renderEtaCells, updateActionAvailability, renderNextAction, isTransientPollingError,
  pollingBackoffMs, postJSON, getJSON, optionalRequestJSON, confirmDialog, safely, onProjectInputsChanged,
} from "./core.js";
import { loadSyncWorkspace } from "./sync.js";

let stage3MediaScanAttempted = false;
let stage3MediaScanning = false;
let currentStage3JobId = "";
let currentStage3JobKind = "";
let stage3JobClientStartedAt = null;
let stage3PollTimer = null;
let stage3PollFailures = 0;
let latestStage3Status = null;
let stage3StatusRequest = 0;

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
    const assembly = latestStage3Status && latestStage3Status.assembly;
    const message = !assembly ? "Готовность таймлайна проверяется…"
      : assembly.level === "no_timeline" ? (assembly.message || "Сначала соберите таймлайн.")
      : assembly.level === "error" ? "Не удалось проверить соответствия: ошибка плана монтажа."
      : "Таймлайн существует. Соответствия просмотровок не требуются или ещё не рассчитаны; список пуст.";
    ids.stage3_source_mapping.innerHTML = `<div class="empty-state compact">${escaped(message)}</div>`;
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

function renderStage3Processing(processing) {
  const processingLabels = {idle: "нет активной задачи", queued: "в очереди", running: "выполняется", completed: "завершена", failed: "ошибка", interrupted: "прервана", cancelled: "отменена"};
  if (ids.stage3_processing) ids.stage3_processing.textContent =
    (processingLabels[processing.status] || "состояние неизвестно")
    + (processing.superseded || processing.stale_revision || processing.stale_timeline ? " · результат устарел" : "");
}

// Readiness is independent from the last job and from the downloadable file.
export function renderStage3Status(status) {
  latestStage3Status = status;
  appState.stage3Status = status;
  const processing = status.processing || {};
  const build = status.build || {};
  const assembly = status.assembly || {};
  renderStage3Processing(processing);
  if (ids.stage3_build) {
    if (!build.build_id) ids.stage3_build.textContent = "подтверждённой сборки нет";
    else {
      const gaps = Number(build.summary?.placeholders?.segments || 0);
      const label = build.mode === "draft" ? `Черновик: ${gaps} пропусков`
        : build.mode === "complete" && build.is_current === true ? "Сборка без пропусков" : "Сборка";
      const reasons = [...(build.stale_reasons || []), build.live_timeline_unavailable_reason].filter(Boolean);
      ids.stage3_build.textContent = `${label} · ${build.build_id} · ${build.is_current === true ? "актуальна" : "устарела"}`
        + (reasons.length ? ` (${reasons.join(", ")})` : "");
    }
  }
  if (ids.stage3_assembly) {
    ids.stage3_assembly.textContent = assembly.level === "complete_ready" ? "можно собрать без пропусков"
      : assembly.level === "draft" ? `Черновик: ${Number(assembly.blocker_count || 0)} блокеров`
      : assembly.level === "no_timeline" ? (assembly.message || "Сначала соберите таймлайн.")
      : assembly.level === "error" ? `Ошибка проверки: ${assembly.message || assembly.reason || "план недоступен"}`
      : "состояние неизвестно";
    if (assembly.catalog_incomplete) ids.stage3_assembly.textContent += ` · каталог медиа неполный: ${Number(assembly.uncached_media_files || 0)} файлов без проверки`;
    if (Number(assembly.open_tasks) > 0) ids.stage3_assembly.textContent += ` · открытых задач: ${assembly.open_tasks}`;
  }
  const help = document.getElementById("premiere_import_help");
  if (help) {
    const legacyKnown = help.dataset.legacyExport === "true" || (processing.kind === "prepare" && processing.status === "completed" && !processing.build_id);
    help.hidden = !build.build_id && !legacyKnown;
    if (build.build_id) help.dataset.legacyExport = "false";
    help.textContent = build.build_id
      ? `В Premiere Pro: Файл → Импорт → выберите скачанный XML. Если медиа не найдены, укажите исходные файлы через «Связать медиа». ${build.mode === "draft" ? "Черновик" : "Сборка без пропусков"}: ${Number(build.summary?.placeholders?.segments || 0)} пропусков. ${build.is_current === true ? "Сборка актуальна." : "Сборка устарела: соберите и скачайте XML заново."}`
      : legacyKnown ? "В Premiere Pro: Файл → Импорт → выберите скачанный XML. Если медиа не найдены, используйте «Связать медиа». XML без подтверждённой сборки; актуальность не подтверждена. Пропуски проверьте в результатах экспорта; для проверенной актуальной сборки скачайте XML заново." : "";
  }
  renderNextAction();
  renderStage3Blockers(assembly.blockers || []);
  renderStage3SourceMapping(appState.stage3SourceMapping || []);
}

export async function refreshStage3Status() {
  const request = ++stage3StatusRequest;
  try {
    const status = await getJSON(`/stage3/status?project=${encodeURIComponent(project)}`);
    if (request === stage3StatusRequest) renderStage3Status(status);
    return status;
  } catch (error) {
    if (request !== stage3StatusRequest) return;
    latestStage3Status = null;
    appState.stage3Status = null;
    const help = document.getElementById("premiere_import_help");
    if (help && !help.hidden) help.textContent = "Актуальность XML не удалось проверить. Обновите состояние перед импортом в Premiere.";
    renderNextAction();
    for (const element of [ids.stage3_processing, ids.stage3_build, ids.stage3_assembly]) {
      if (element) element.textContent = "проверка недоступна; обновите состояние";
    }
    renderStage3Blockers([]);
    renderStage3SourceMapping(appState.stage3SourceMapping || []);
    appendLog(`Готовность монтажа: ${error.message || error}`, "warn");
  }
}

function stage3JobPayload(value) {
  if (!value || typeof value !== "object") return {};
  return value.job && typeof value.job === "object" ? value.job : value;
}

function renderStage3Job(value) {
  const job = stage3JobPayload(value);
  // A response started before this job update must not restore the previous task.
  stage3StatusRequest += 1;
  renderStage3Processing(job);
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
    ? (currentStage3JobKind === "prepare" ? "экспорт завершён" : "готово")
    : status === "failed" ? "ошибка"
    : status === "interrupted" ? "прервано"
    : status === "queued" ? "в очереди"
    : (currentStage3JobKind === "prepare" ? "сборка…" : "сканирование…");
  if (ids.stage3_job_progress) ids.stage3_job_progress.hidden = false;
  if (ids.stage3_job_message) ids.stage3_job_message.textContent = job.message || "Stage 3";
  if (ids.stage3_job_numbers) ids.stage3_job_numbers.textContent = `${Math.round(progress)}%`;
  if (ids.stage3_progress_bar) ids.stage3_progress_bar.style.width = `${progress}%`;
  if (job.error) {
    ids.stage3_media_message.textContent = job.error_code === "assembly_incomplete"
      ? (job.message || job.error) : job.error;
  }
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
  ids.btn_export_xml.textContent = "Скачать XML для Premiere";
  updateActionAvailability();
}

const STAGE3_PLACEHOLDER_REASON_LABELS = {
  missing_wav: "Нет WAV",
  missing_voiceover: "Нет озвучки",
  recorded_beyond_source: "Диапазон за пределами записи",
  no_speaker: "Не указан спикер",
  no_file: "Нет записи спикера",
  asr_failed: "Сбой распознавания",
  not_found: "Не найдено",
  needs_review: "Нужна проверка",
  unmapped_source: "Источник не подключён",
  life_unassigned: "LIFE: материал не назначен",
  host_standup: "Стендап ведущего: дубль не назначен",
};

// Extra blocker reasons produced by the "assembly without gaps" validation.
const STAGE3_BLOCKER_REASON_LABELS = {
  ...STAGE3_PLACEHOLDER_REASON_LABELS,
  speech_overlap: "Речь пересекается с соседним клипом",
  source_missing: "Файл просмотровки не найден",
  invalid_edit_range: "Некорректный диапазон In/Out",
  edit_beyond_source: "Диапазон выходит за длину источника",
  speech_outside_edit: "Речь вне диапазона клипа",
  open_task: "Есть незакрытая задача",
};

export function stage3PrepareMode() {
  const value = ids.stage3_prepare_mode ? String(ids.stage3_prepare_mode.value || "") : "";
  return value === "complete" ? "complete" : "draft";
}

export function describeStage3Blocker(blocker) {
  const reason = String(blocker && blocker.reason || "");
  const label = STAGE3_BLOCKER_REASON_LABELS[reason] || reason || "Причина не указана";
  const type = String(blocker && blocker.type || "").toUpperCase() || "—";
  const segmentId = String(blocker && (blocker.segment_id || blocker.uid) || "—");
  return `${type} · ${segmentId} — ${label}`;
}

export function renderStage3Blockers(blockers) {
  const items = Array.isArray(blockers) ? blockers : [];
  appState.stage3Blockers = items;
  if (!ids.stage3_blockers) return items;
  ids.stage3_blockers.hidden = items.length === 0;
  ids.stage3_blockers.innerHTML = items.length === 0 ? "" : (
    `<strong>Сборка без пропусков невозможна: блокеров ${items.length}</strong><ul>`
    + items.map((item) =>
      `<li title="${escaped(item.detail || "")}">${escaped(describeStage3Blocker(item))}</li>`
    ).join("")
    + "</ul>"
  );
  return items;
}

export function describeStage3Placeholders(result) {
  const total = Number(result.placeholder_segments || 0);
  let text = `плейсхолдеров: ${total}`;
  if (total > 0) {
    text += ` (${Number(result.placeholder_duration_sec || 0).toFixed(1)} с)`;
    const parts = Object.entries(result.placeholder_breakdown || {})
      .filter(([, item]) => Number(item && item.segments) > 0)
      .map(([reason, item]) =>
        `${STAGE3_PLACEHOLDER_REASON_LABELS[reason] || reason}: ${Number(item.segments)}`
        + ` / ${Number(item.duration_sec || 0).toFixed(1)} с`
      );
    if (parts.length) text += `: ${parts.join("; ")}`;
  }
  return text;
}

export function stage3BuildLabel(result) {
  const buildId = String((result && result.build_id) || "");
  return buildId ? `Сборка ${buildId}: ` : "";
}

// The immutable build is addressed by id, so a later prepare cannot swap the file under the link.
export function stage3DownloadUrl(result) {
  const url = String((result && result.download_url) || "");
  const buildId = String((result && result.build_id) || "");
  if (!url || !buildId || /[?&]build_id=/.test(url)) return url;
  return `${url}${url.includes("?") ? "&" : "?"}build_id=${encodeURIComponent(buildId)}`;
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
  renderStage3Blockers([]);
  ids.stage3_media_message.textContent =
    `${result.build_id ? stage3BuildLabel(result) : "Экспортированный XML (без подтверждённой сборки): "}Сопоставлено источников: ${(result.mapped_source_ids || []).length}; `
    + `${describeStage3Placeholders(result)}.`;
  appendLog(
    `Timeline XML (${result.format || "nested"}${result.build_id ? `, build ${result.build_id}` : ""}): mapped=${(result.mapped_source_ids || []).join(", ") || "—"}; `
    + `unmapped=${(result.unmapped_source_ids || []).join(", ") || "—"}`
  );
  const help = document.getElementById("premiere_import_help");
  if (help) {
    help.hidden = false;
    help.dataset.legacyExport = result.build_id ? "false" : "true";
    help.textContent = `В Premiere Pro: Файл → Импорт → выберите скачанный XML. Если медиа не найдены, укажите исходники через «Связать медиа». ${result.mode === "complete" ? "Сборка без пропусков" : "Черновик"}: ${describeStage3Placeholders(result)}. Актуальность проверяется в состоянии сборки.`;
  }
  if (download && result.download_url) {
    toast(`XML готов${result.build_id ? ` (сборка ${result.build_id})` : ""}. Начинаю загрузку.`, "success");
    window.location.href = stage3DownloadUrl(result);
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
      await refreshStage3Status();
      return;
    }
    if (["failed", "interrupted"].includes(status)) {
      finishStage3Job();
      if (job.error_code === "assembly_incomplete") {
        renderStage3Blockers(job.blockers);
      }
      const message = job.error_code === "assembly_incomplete"
        ? (job.message || "Сборка без пропусков невозможна")
        : (job.error || job.message || "Stage 3 завершился с ошибкой");
      ids.stage3_media_message.textContent = message;
      toast(message, status === "failed" ? "error" : "warn");
      appendLog(`Stage 3 ${job.kind || "job"}: ${message}`, "error");
      await refreshStage3Status();
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
    if (kind === "prepare") renderStage3Blockers([]);
    const body = kind === "prepare" ? { project, mode: stage3PrepareMode() } : { project };
    const started = await postJSON(`/stage3/jobs/${kind}/start`, body);
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
  if (status === "failed" && job.error_code === "assembly_incomplete") {
    renderStage3Blockers(job.blockers);
  }
  finishStage3Job();
  await refreshStage3Status();
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
  // "Assembly without gaps" never exports placeholders, so the draft confirmation is moot:
  // the backend refuses to publish while blockers remain.
  if (stage3PrepareMode() !== "complete" && !(await confirmExportSummary())) return undefined;
  return startStage3Job("prepare");
}

if (ids.btn_stage3_media_scan) ids.btn_stage3_media_scan.addEventListener("click", () => safely(scanStage3Media));
ids.btn_export_xml.addEventListener("click", () => safely(exportTimelineXml));

onProjectInputsChanged(refreshStage3Status);
