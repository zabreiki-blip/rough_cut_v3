// Stage 2 sync: registered sequences ("sync sources"), automatic
// СНХ-matching jobs, and the manual review/confirm UI for matches.

import { ids, appState, project } from "./state.js";
import {
  toast, appendLog, formatMediaDuration, escaped, updateActionAvailability,
  isTransientPollingError, pollingBackoffMs, optionalRequestJSON, safely,
} from "./core.js";

let syncMatches = [];
let syncMatchesRevision = 0;
let currentSyncJobId = "";
let syncPollTimer = null;
let syncPollFailures = 0;

function syncCollection(document, keys) {
  if (Array.isArray(document)) return document;
  if (!document || typeof document !== "object") return [];
  for (const key of keys) {
    if (Array.isArray(document[key])) return document[key];
  }
  return [];
}

function normalizedMatchStatus(value) {
  const status = String(value || "unmatched").toLowerCase();
  if (status === "matched" || status === "auto" || status === "ready") return "auto_matched";
  if (status === "review" || status === "candidate" || status === "probable") return "needs_review";
  if (status === "not_found" || status === "missing") return "unmatched";
  return status;
}

function matchCandidate(match) {
  const alternatives = Array.isArray(match.alternatives) ? match.alternatives : [];
  const nested = match.candidate || match.best_candidate || alternatives[0] || {};
  const value = { ...nested };
  [
    "source_id", "sequence_name", "source_in_sec", "source_out_sec",
    "speech_in_sec", "speech_out_sec", "matched_text", "confidence",
  ].forEach((key) => {
    if (match[key] !== undefined && match[key] !== null && match[key] !== "") {
      value[key] = match[key];
    }
  });
  return value;
}

function candidateChoices(match) {
  const primary = matchCandidate(match);
  const values = [primary, ...(Array.isArray(match.alternatives) ? match.alternatives : [])];
  const seen = new Set();
  return values.filter((candidate) => {
    const key = [
      candidate.source_id,
      Number(candidate.source_in_sec).toFixed(3),
      Number(candidate.source_out_sec).toFixed(3),
    ].join(":");
    if (!candidate.source_id || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function candidateSource(candidate) {
  return appState.syncSources.find((source) =>
    String(source.id || source.source_id || "") === String(candidate.source_id || "")
  ) || {};
}

function syncAudioUrl(sourceId) {
  if (!sourceId) return "";
  return `/stage2/sync-sources/${encodeURIComponent(sourceId)}/audio?project=${encodeURIComponent(project)}`;
}

function matchBlock(match) {
  const normalization = appState.normalization;
  if (!normalization || !Array.isArray(normalization.blocks)) return {};
  const uid = String(match.block_uid || "");
  if (uid) {
    return normalization.blocks.find((block) => String(block.uid || "") === uid) || {};
  }
  const legacyId = String(match.segment_id || "");
  const legacyIndex = Number(legacyId.match(/^snh(\d+)$/)?.[1] || 0) - 1;
  if (legacyIndex < 0) return {};
  return normalization.blocks.filter((block) => String(block.type || "") === "sync")[legacyIndex] || {};
}

function formatSourceSecond(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  const prefix = hours > 0 ? `${hours}:${String(minutes).padStart(2, "0")}` : String(minutes);
  return `${prefix}:${rest.toFixed(1).padStart(4, "0")}`;
}

function sourceSpeakerNames(source) {
  return [source.speaker].concat(
    Array.isArray(source.speakers) ? source.speakers : []
  ).filter(Boolean);
}

// Human-readable search scope shown next to each editable source: matches
// the wording operators need to diagnose why a source is (not) searched for
// a given block's speaker (see
// docs/archive/audits/NORMALIZATION_MATCHING_AUDIT_2026-09-07.md).
function sourceScopeLabel(source) {
  if (source.all_speakers) return "все спикеры";
  const speakers = sourceSpeakerNames(source);
  return speakers.length ? speakers.join(", ") : "спикер не указан — ищется для всех";
}

function sourceEditableId(source) {
  return String(source.id || source.source_id || "");
}

function renderSyncSourcesList() {
  if (!ids.sync_sources_list) return;
  if (appState.syncFeatureAvailable === false || appState.syncSources.length === 0) {
    ids.sync_sources_list.innerHTML = "";
    return;
  }
  ids.sync_sources_list.innerHTML = appState.syncSources.map((source) => {
    const sourceId = sourceEditableId(source);
    const sequence = source.premiere_sequence || source.sequence_name
      || source.name || sourceId || "без имени";
    const duration = formatMediaDuration(source.duration_sec);
    const speakersValue = sourceSpeakerNames(source).join(", ");
    const checked = source.all_speakers ? " checked" : "";
    return `
      <article class="sync-source-card" data-source-id="${escaped(sourceId)}">
        <div class="sync-match-head">
          <div class="sync-match-title">${escaped(sequence)}</div>
          <span class="sync-match-meta">${escaped(duration)}</span>
        </div>
        <div class="sync-match-meta">Область поиска: ${escaped(sourceScopeLabel(source))}</div>
        <div class="sync-source-edit">
          <label class="sync-match-field sync-source-speakers-field">
            <span>Спикеры (через запятую)</span>
            <input type="text" data-sync-source-speakers value="${escaped(speakersValue)}"
              placeholder="Татьяна Васильева, Иван Иванов">
          </label>
          <label class="toggle sync-source-all-toggle">
            <input type="checkbox" data-sync-source-all${checked}>
            <span class="toggle-slider"></span>
            <span>В этой секвенции находятся все спикеры проекта</span>
          </label>
          <button type="button" class="btn btn-sm btn-accent" data-action="save-sync-source">Сохранить</button>
        </div>
      </article>`;
  }).join("");
}

// Pure payload builder for POST /stage2/sync-sources, kept separate from DOM
// reads so it can be unit-tested directly. Mirrors the CEP panel's parsing
// (cep/js/main.js registerSequenceWithCallback): the first name goes to the
// legacy singular `speaker` field, the rest to `speakers`, and both are
// always sent together so the backend merge never wipes one with the other
// (see src/api_stage2_routes.py "speaker" without "speakers" clause).
export function syncSourceEditPayload(source, speakersText, allSpeakersChecked) {
  const names = String(speakersText || "")
    .split(/[,;\n]+/)
    .map((value) => value.trim())
    .filter(Boolean);
  const sourceId = sourceEditableId(source);
  return {
    project,
    id: sourceId,
    source_id: sourceId,
    speaker: names[0] || "",
    speakers: names.slice(1),
    all_speakers: Boolean(allSpeakersChecked),
  };
}

export async function saveSyncSourceEdit(source, speakersText, allSpeakersChecked) {
  const payload = syncSourceEditPayload(source, speakersText, allSpeakersChecked);
  const response = await optionalRequestJSON("/stage2/sync-sources", "POST", payload);
  if (!response.available) {
    toast("Сохранение источников пока не поддерживается backend", "warn");
    appendLog("Stage 2 sync: сохранение источника недоступно в текущей версии backend.", "warn");
    return;
  }
  toast("Источник синхронизации обновлён", "success");
  appendLog(`Stage 2 sync: обновлён источник ${payload.id}.`);
  await loadSyncWorkspace(true);
}

function renderSyncSources() {
  if (!ids.sync_sources_count) return;
  ids.sync_sources_count.textContent = String(appState.syncSources.length);
  if (appState.syncFeatureAvailable === false) {
    ids.sync_sources_names.textContent = "Функция недоступна в текущей версии backend";
    ids.sync_job_status.dataset.state = "idle";
    ids.sync_job_status.textContent = "недоступно";
  } else if (appState.syncSources.length === 0) {
    ids.sync_sources_names.textContent = "Нет зарегистрированных секвенций";
  } else {
    const names = appState.syncSources.map((source) => {
      const sequence = source.premiere_sequence || source.sequence_name
        || source.name || source.id || "без имени";
      const speakers = sourceSpeakerNames(source);
      const scope = source.all_speakers
        ? "все спикеры"
        : (speakers.length ? speakers.join(", ") : (source.speaker || "спикер не указан"));
      return `${sequence} · ${scope}`;
    });
    const visible = names.slice(0, 3).join(", ");
    ids.sync_sources_names.textContent = names.length > 3
      ? `${visible} и ещё ${names.length - 3}`
      : visible;
  }
  renderSyncSourcesList();
  updateActionAvailability();
}

function syncCounts(document) {
  const summary = document && typeof document.summary === "object" ? document.summary : {};
  const explicit = {
    auto: summary.auto_matched ?? summary.auto ?? document?.auto_matched,
    confirmed: summary.confirmed ?? document?.confirmed,
    review: summary.needs_review ?? summary.review ?? document?.needs_review,
    unmatched: summary.unmatched ?? document?.unmatched,
    noSource: summary.no_source ?? document?.no_source,
  };
  if (Object.values(explicit).some((value) => value !== undefined)) {
    return {
      auto: Number(explicit.auto || 0),
      confirmed: Number(explicit.confirmed || 0),
      review: Number(explicit.review || 0),
      unmatched: Number(explicit.unmatched || 0),
      noSource: Number(explicit.noSource || 0),
    };
  }
  return syncMatches.reduce((counts, match) => {
    const status = normalizedMatchStatus(match.status);
    if (status === "auto_matched") counts.auto += 1;
    else if (status === "confirmed") counts.confirmed += 1;
    else if (status === "needs_review") counts.review += 1;
    else if (match.metadata?.unmatched_reason === "no_eligible_source") counts.noSource += 1;
    else counts.unmatched += 1;
    return counts;
  }, { auto: 0, confirmed: 0, review: 0, unmatched: 0, noSource: 0 });
}

function updateSyncAsrControls() {
  if (!ids.sync_whisper_model) return;
  const isCloud = (ids.sync_asr_engine?.value || "local") === "cloud";
  ids.sync_whisper_model.disabled = isCloud;
  ids.sync_whisper_model.title = isCloud
    ? "Small и Medium используются только для локального Whisper"
    : "Выберите локальную модель Whisper";
}

function renderSyncMatches(document) {
  if (!ids.sync_review_list) return;
  const counts = syncCounts(document || {});
  ids.sync_count_auto.textContent = String(counts.auto);
  ids.sync_count_confirmed.textContent = String(counts.confirmed);
  ids.sync_count_review.textContent = String(counts.review);
  ids.sync_count_unmatched.textContent = String(counts.unmatched);
  ids.sync_count_no_source.textContent = String(counts.noSource);

  const reviewItems = syncMatches.filter((match) => {
    const status = normalizedMatchStatus(match.status);
    return status === "needs_review" || status === "unmatched";
  });
  ids.sync_review_count.textContent = `${reviewItems.length} фрагментов`;

  if (appState.syncFeatureAvailable === false) {
    ids.sync_review_list.innerHTML =
      '<div class="empty-state compact">Сопоставление появится после обновления backend.</div>';
    return;
  }
  if (reviewItems.length === 0) {
    ids.sync_review_list.innerHTML = syncMatches.length > 0
      ? '<div class="empty-state compact">Все найденные фрагменты готовы к сборке.</div>'
      : '<div class="empty-state compact">Результатов сопоставления пока нет.</div>';
    return;
  }

  ids.sync_review_list.innerHTML = reviewItems.map((match) => {
    const status = normalizedMatchStatus(match.status);
    const candidate = matchCandidate(match);
    const candidates = candidateChoices(match);
    const block = matchBlock(match);
    const blockUid = String(match.block_uid || match.segment_id || "");
    const matchId = String(match.segment_id || blockUid);
    const speaker = match.speaker || block.speaker || match.segment_id || "СНХ";
    const timecode = match.approximate_timecode || match.approx_timecode || block.timecode_start || "";
    const scenarioText = match.scenario_text || match.query_text
      || block.normalized_text || block.original_text || "";
    const matchedText = candidate.matched_text || match.matched_text || "";
    const sourceRecord = candidateSource(candidate);
    const source = candidate.sequence_name || sourceRecord.sequence_name
      || sourceRecord.premiere_sequence || candidate.source_id || "источник не выбран";
    const confidence = Number(candidate.confidence);
    const confidenceText = Number.isFinite(confidence)
      ? `${Math.round(confidence * (confidence <= 1 ? 100 : 1))}%`
      : "—";
    const llmReview = match.metadata?.llm_semantic_review;
    const llmConfidence = Number(llmReview?.confidence);
    const llmHint = llmReview
      ? `<div class="sync-match-llm">LLM выбрал этот вариант по смыслу${
          Number.isFinite(llmConfidence)
            ? ` · уверенность ${Math.round(llmConfidence * 100)}%`
            : ""
        }. Проверьте аудио перед подтверждением.</div>`
      : "";
    const speakerFilteredSources = Array.isArray(match.metadata?.speaker_filtered_sources)
      ? match.metadata.speaker_filtered_sources.filter((item) => item && item.source_id)
      : [];
    const speakerFilterHint = speakerFilteredSources.length
      ? (() => {
          const entries = speakerFilteredSources.map((item) => {
            const itemSource = candidateSource(item);
            const itemName = itemSource.sequence_name || itemSource.premiere_sequence || item.source_id;
            const itemSpeakers = Array.isArray(item.source_speakers)
              ? item.source_speakers.filter(Boolean)
              : [];
            const itemSpeakerText = itemSpeakers.length ? itemSpeakers.join(", ") : "не указан";
            return `«${escaped(itemName)}» (спикер «${escaped(itemSpeakerText)}»)`;
          });
          const visible = entries.slice(0, 3).join(", ");
          const rest = entries.length > 3 ? ` и ещё ${entries.length - 3}` : "";
          const many = entries.length > 1;
          return `<div class="sync-match-speaker-filter">Источник${many ? "и" : ""} ${visible}${rest}
            не искал${many ? "ись" : "ся"}, потому что указанный у ${many ? "них" : "него"} спикер
            не совпадает со спикером блока «${escaped(speaker)}». Отметьте источник как «все спикеры»
            или укажите у него верные имена спикеров.</div>`;
        })()
      : "";
    const unmatchedReasons = {
      query_too_short: "Фраза слишком короткая для поиска по записи.",
      no_eligible_source: "Нет записи: для этого спикера ещё нет источника — реплика будет найдена после загрузки.",
      no_candidate: "В доступной расшифровке подходящая фраза не найдена.",
      below_review_threshold: "Есть только слабые совпадения; проверьте запись вручную.",
    };
    const unmatchedReason = unmatchedReasons[match.metadata?.unmatched_reason] || "";
    const sourceIn = Number(candidate.source_in_sec);
    const sourceOut = Number(candidate.source_out_sec);
    const hasCandidate = Boolean(candidate.source_id)
      && Number.isFinite(sourceIn) && Number.isFinite(sourceOut) && sourceOut > sourceIn;
    const label = llmReview
      ? "LLM · проверить"
      : (status === "unmatched"
        ? (match.metadata?.unmatched_reason === "no_eligible_source" ? "нет записи" : "не найдено")
        : "нужно проверить");
    const action = hasCandidate && matchId
      ? `<button class="btn btn-sm btn-accent" type="button"
           data-action="confirm-sync-match" data-match-id="${escaped(matchId)}">
           ${status === "unmatched" ? "Подтвердить лучший вариант" : "Подтвердить"}
         </button>`
      : "";
    const candidateSelect = candidates.length > 1
      ? `<label class="sync-match-field sync-match-candidate">
           <span>Вариант</span>
           <select data-sync-candidate>
             ${candidates.map((item, index) => {
               const itemSource = candidateSource(item);
               const itemName = item.sequence_name || itemSource.sequence_name
                 || itemSource.premiere_sequence || item.source_id;
               const itemConfidence = Number(item.confidence);
               const itemPct = Number.isFinite(itemConfidence)
                 ? Math.round(itemConfidence * (itemConfidence <= 1 ? 100 : 1))
                 : 0;
               return `<option value="${index}">${escaped(itemName)} · ${formatSourceSecond(item.source_in_sec)} · ${itemPct}%</option>`;
             }).join("")}
           </select>
         </label>`
      : "";
    const reviewControls = hasCandidate
      ? `<div class="sync-review-controls">
           ${candidateSelect}
           <label class="sync-match-field">
             <span>Source in, сек</span>
             <input data-sync-in type="number" min="0" step="0.04" value="${escaped(sourceIn.toFixed(3))}">
           </label>
           <label class="sync-match-field">
             <span>Source out, сек</span>
             <input data-sync-out type="number" min="0" step="0.04" value="${escaped(sourceOut.toFixed(3))}">
           </label>
         </div>
         <audio data-sync-audio controls preload="none"
           src="${escaped(syncAudioUrl(candidate.source_id))}"
           data-range-in="${escaped(sourceIn)}" data-range-out="${escaped(sourceOut)}"></audio>`
      : "";
    return `
      <article class="sync-match-card" data-status="${escaped(status)}"
        data-block-uid="${escaped(blockUid)}" data-match-id="${escaped(matchId)}"
        data-source-id="${escaped(candidate.source_id || "")}">
        <div class="sync-match-head">
          <div class="sync-match-title">${escaped(speaker)}${timecode ? ` · ${escaped(timecode)}` : ""}</div>
          <span class="sync-match-status">${label}</span>
        </div>
        <p class="sync-match-text">${escaped(scenarioText || "Текст сценария отсутствует")}</p>
        <div class="sync-match-found" data-sync-found>${matchedText
          ? escaped(matchedText)
          : "Whisper-фрагмент не найден"}</div>
        ${llmHint}
        ${unmatchedReason ? `<div class="sync-match-speaker-filter">${escaped(unmatchedReason)}</div>` : ""}
        ${speakerFilterHint}
        <div class="sync-match-meta" data-sync-meta>
          ${escaped(source)} · ${formatSourceSecond(sourceIn)}–${formatSourceSecond(sourceOut)}
          · уверенность ${confidenceText}
        </div>
        ${reviewControls}
        <div class="sync-match-actions">${action}</div>
      </article>`;
  }).join("");
  updateActionAvailability();
}

export async function loadSyncWorkspace(quiet) {
  const [sourcesResponse, matchesResponse, jobResponse] = await Promise.all([
    optionalRequestJSON(`/stage2/sync-sources?project=${encodeURIComponent(project)}`),
    optionalRequestJSON(`/stage2/sync-matches?project=${encodeURIComponent(project)}`),
    optionalRequestJSON(`/stage2/sync-jobs/status?project=${encodeURIComponent(project)}`),
  ]);
  appState.syncFeatureAvailable = sourcesResponse.available || matchesResponse.available;

  const sourcesDocument = sourcesResponse.result || {};
  const matchesDocument = matchesResponse.result || {};
  appState.syncSources = syncCollection(sourcesDocument, ["items", "sources"]);
  syncMatches = syncCollection(matchesDocument, ["items", "matches"]);
  syncMatchesRevision = Number(matchesDocument.revision || 0);
  renderSyncSources();
  renderSyncMatches(matchesDocument);
  if (jobResponse.available && jobResponse.result) {
    const job = syncJobPayload(jobResponse.result);
    renderSyncJob(job);
    const status = String(job.status || "").toLowerCase();
    if (["queued", "running", "cancelling"].includes(status)) {
      appState.syncJobRunning = true;
      updateActionAvailability();
      if (!syncPollTimer && currentSyncJobId) {
        syncPollTimer = window.setTimeout(
          () => pollSyncJob(`/stage2/sync-jobs/${encodeURIComponent(currentSyncJobId)}`),
          500,
        );
      }
    }
    updateActionAvailability();
  }
  if (!quiet && appState.syncFeatureAvailable === false) {
    appendLog("Stage 2 sync API пока недоступен; обычная сборка продолжает работать.", "warn");
  }
}

function syncJobPayload(value) {
  if (!value || typeof value !== "object") return {};
  return value.job && typeof value.job === "object" ? value.job : value;
}

function renderSyncJob(value) {
  const job = syncJobPayload(value);
  const status = String(job.status || "running").toLowerCase();
  currentSyncJobId = String(job.id || job.job_id || currentSyncJobId || "");
  appState.currentSyncJobStatus = status;
  const total = Number(job.total || job.items_total || 0);
  const done = Number(job.done ?? job.completed ?? job.items_done ?? 0);
  const current = job.current_source_id || job.current_source || job.current_id || "";
  const message = job.message || job.event || (current ? `Обрабатывается ${current}` : "Сопоставление СНХ");
  const totalSources = Number(job.total_sources || 0);
  const completedSources = Math.min(
    totalSources || Number.POSITIVE_INFINITY,
    Number(job.transcribed || 0)
      + Number(job.reused_indexes || 0)
      + (Array.isArray(job.source_failures) ? job.source_failures.length : 0),
  );
  const processedVideo = Number(job.transcription_processed_sec || 0);
  const currentVideoDuration = Number(job.transcription_duration_sec || 0);
  const currentFraction = currentVideoDuration > 0
    ? Math.max(0, Math.min(1, processedVideo / currentVideoDuration))
    : 0;
  const sourcePct = totalSources > 0
    ? Math.round(((completedSources + currentFraction) / totalSources) * 100)
    : 0;
  const pct = status === "completed" ? 100
    : (currentVideoDuration > 0 || totalSources > 0 ? sourcePct
      : (total > 0 ? Math.round((done / total) * 100) : 0));
  const mapping = appState.stage3SourceMapping.find((item) => item.source_id === current) || {};
  const jobStartedAt = Date.parse(job.started_at || "");
  const sourceStartedAt = Date.parse(job.transcription_source_started_at || "");
  const elapsedSec = Number.isFinite(jobStartedAt)
    ? Math.max(0, (Date.now() - jobStartedAt) / 1000)
    : 0;
  const sourceElapsedSec = Number.isFinite(sourceStartedAt)
    ? Math.max(0, (Date.now() - sourceStartedAt) / 1000)
    : 0;
  const currentEtaSec = sourceElapsedSec > 0
    && processedVideo > 0
    && currentVideoDuration > processedVideo
    ? sourceElapsedSec * (currentVideoDuration - processedVideo) / processedVideo
    : 0;
  const overallFraction = Math.max(0, Math.min(1, pct / 100));
  const overallEtaSec = elapsedSec > 0 && overallFraction > 0 && overallFraction < 1
    ? elapsedSec * (1 - overallFraction) / overallFraction
    : 0;

  ids.sync_job_status.dataset.state = status;
  const segmentFailures = Array.isArray(job.segment_failures) ? job.segment_failures : [];
  const sourceWarnings = Array.isArray(job.source_warnings) ? job.source_warnings : [];
  ids.sync_job_status.textContent = status === "completed"
    ? (segmentFailures.length ? "готово с ошибками" : "готово")
    : status === "failed" ? "ошибка"
    : status === "interrupted" ? "прервано"
    : status === "cancelled" ? "прервано"
    : status === "cancelling" ? "останавливается"
    : status === "queued" ? "в очереди"
    : "в работе";
  ids.sync_job_progress.hidden = false;
  const warningSuffix = sourceWarnings.length
    ? ` · ${sourceWarnings.map((item) => item.warning || item.source_id || "предупреждение").join("; ")}`
    : "";
  ids.sync_job_message.textContent = (segmentFailures.length
    ? `${message} · ошибок сегментов: ${segmentFailures.length} (${segmentFailures[0].segment_id || "без ID"}: ${segmentFailures[0].error || "ошибка"})`
    : message) + warningSuffix;
  ids.sync_job_numbers.textContent = totalSources > 0
    ? `${completedSources}/${totalSources} файлов · ${pct}%`
    : (total > 0 ? `${done}/${total}` : `${pct}%`);
  ids.sync_progress_bar.style.width = `${Math.max(0, Math.min(100, pct))}%`;
  if (ids.sync_job_current) {
    ids.sync_job_current.textContent = mapping.media_name || current || "—";
  }
  if (ids.sync_job_video_progress) {
    ids.sync_job_video_progress.textContent = currentVideoDuration > 0
      ? `${formatMediaDuration(processedVideo)} / ${formatMediaDuration(currentVideoDuration)}`
      : "—";
  }
  if (ids.sync_job_elapsed) {
    ids.sync_job_elapsed.textContent = elapsedSec > 0 ? formatMediaDuration(elapsedSec) : "—";
  }
  if (ids.sync_job_eta) {
    ids.sync_job_eta.textContent = status === "completed" ? "готово"
      : (overallEtaSec > 0
        ? `≈ ${formatMediaDuration(overallEtaSec)} всего`
        : (currentEtaSec > 0
          ? `≈ ${formatMediaDuration(currentEtaSec)} текущая`
          : "рассчитывается"));
  }
  renderSyncJobMetrics(job);
}

function renderSyncJobMetrics(job) {
  if (!ids.sync_job_metrics) return;
  const backend = String(job.asr_backend_used || "");
  const engine = String(job.asr_engine || "");
  const wallSec = Number(job.transcription_wall_sec_total || 0);
  const costUsd = Number(job.asr_cost_usd_total || 0);
  const precision = String(job.asr_timestamp_precision || "");
  const hasMetrics = Boolean(backend) || wallSec > 0;
  ids.sync_job_metrics.hidden = !hasMetrics;
  if (!hasMetrics) return;
  if (ids.sync_metric_backend) {
    const engineLabel = engine === "cloud" ? "облако"
      : engine === "local" ? "локально"
      : "";
    ids.sync_metric_backend.textContent = engineLabel
      ? (backend ? `${engineLabel} (${backend})` : engineLabel)
      : (backend || "—");
  }
  if (ids.sync_metric_wall_time) {
    ids.sync_metric_wall_time.textContent = wallSec > 0 ? formatMediaDuration(wallSec) : "—";
  }
  if (ids.sync_metric_cost) {
    ids.sync_metric_cost.textContent = costUsd > 0 ? `$${costUsd.toFixed(4)}` : "бесплатно";
  }
  if (ids.sync_metric_precision) {
    ids.sync_metric_precision.textContent = precision === "interpolated"
      ? "интерполированные"
      : (precision === "word" ? "точные" : "—");
  }
}

function finishSyncJob() {
  appState.syncJobRunning = false;
  syncPollFailures = 0;
  if (syncPollTimer) window.clearTimeout(syncPollTimer);
  syncPollTimer = null;
  updateActionAvailability();
}

async function pollSyncJob(statusUrl) {
  try {
    const response = await optionalRequestJSON(statusUrl);
    syncPollFailures = 0;
    if (!response.available) {
      finishSyncJob();
      appendLog("Статус задачи сопоставления больше недоступен.", "warn");
      return;
    }
    const job = syncJobPayload(response.result);
    renderSyncJob(job);
    const status = String(job.status || "").toLowerCase();
    if (status === "completed") {
      finishSyncJob();
      await loadSyncWorkspace(true);
      toast("Автоматическое сопоставление СНХ завершено", "success");
      appendLog("Stage 2 sync: сопоставление завершено.");
      return;
    }
    if (status === "failed" || status === "cancelled") {
      finishSyncJob();
      if (status === "cancelled") {
        toast("Сопоставление остановлено. Индексы и найденные диапазоны сохранены.", "warn");
        appendLog("Stage 2 sync: остановлено; можно продолжить повторным запуском.", "warn");
      } else {
        const message = job.error || job.last_error || "Сопоставление завершилось с ошибкой";
        toast(message, "error");
        appendLog(`Stage 2 sync: ${message}`, "error");
      }
      return;
    }
    if (status === "interrupted") {
      finishSyncJob();
      await loadSyncWorkspace(true);
      toast("Работа была прервана. Подтверждённые СНХ сохранены — запустите сопоставление снова.", "warn");
      appendLog("Stage 2 sync: задача прервана; сохранённые результаты можно продолжить.", "warn");
      return;
    }
    syncPollTimer = window.setTimeout(() => pollSyncJob(statusUrl), 1500);
  } catch (err) {
    if (!isTransientPollingError(err)) {
      finishSyncJob();
      toast(err.message || String(err), "error");
      appendLog(`Stage 2 sync status: ${err.message || err}`, "error");
      return;
    }
    syncPollFailures += 1;
    const delay = pollingBackoffMs(syncPollFailures, 1500);
    appendLog(
      `Stage 2 sync status временно недоступен; повтор через ${Math.ceil(delay / 1000)} с.`,
      "warn",
    );
    syncPollTimer = window.setTimeout(() => pollSyncJob(statusUrl), delay);
  }
}

async function startSyncAlignment() {
  appendLog("Stage 2 sync: запуск распознавания и сопоставления...");
  if (ids.btn_sync_cancel) {
    ids.btn_sync_cancel.disabled = false;
    ids.btn_sync_cancel.textContent = "Остановить после текущего шага";
  }
  const asrEngine = ids.sync_asr_engine?.value || "local";
  const response = await optionalRequestJSON(
    "/stage2/sync-jobs/start",
    "POST",
    {
      project,
      timecode_mode: ids.sync_timecode_mode?.value || "relative",
      // Small/Medium are local Whisper model sizes. Cloud ASR chooses its
      // own configured provider/model default, so never send a local ID.
      model: asrEngine === "local" ? (ids.sync_whisper_model?.value || "small") : undefined,
      asr_engine: asrEngine,
      source_scope: appState.stage3MediaCount > 0 ? "review_media" : "all",
    },
  );
  if (!response.available) {
    appState.syncFeatureAvailable = false;
    renderSyncSources();
    renderSyncMatches({});
    appendLog("Stage 2 sync API пока недоступен; обычная сборка продолжает работать.", "warn");
    toast("Автосопоставление пока не установлено", "warn");
    return;
  }

  const job = syncJobPayload(response.result);
  const jobId = String(job.job_id || job.id || "");
  if (!jobId && String(job.status || "").toLowerCase() !== "completed") {
    throw new Error("Backend не вернул идентификатор задачи сопоставления");
  }
  appState.syncFeatureAvailable = true;
  appState.syncJobRunning = true;
  currentSyncJobId = jobId;
  appState.currentSyncJobStatus = String(job.status || "queued").toLowerCase();
  renderSyncJob(job);
  updateActionAvailability();

  if (String(job.status || "").toLowerCase() === "completed") {
    finishSyncJob();
    await loadSyncWorkspace(true);
    return;
  }
  const suppliedUrl = String(job.status_url || "");
  const statusUrl = suppliedUrl.startsWith("/stage2/sync-jobs/")
    ? suppliedUrl
    : `/stage2/sync-jobs/${encodeURIComponent(jobId)}`;
  syncPollTimer = window.setTimeout(() => pollSyncJob(statusUrl), 500);
}

async function cancelSyncJob() {
  if (!currentSyncJobId || !appState.syncJobRunning) return;
  try {
    const response = await optionalRequestJSON(
      `/stage2/sync-jobs/${encodeURIComponent(currentSyncJobId)}/cancel`,
      "POST",
      {},
    );
    if (!response.available) throw new Error("Backend не поддерживает остановку задачи");
    renderSyncJob(response.result);
    ids.btn_sync_cancel.disabled = true;
    ids.btn_sync_cancel.textContent = "Останавливается…";
    appendLog("Запрошена остановка сопоставления; уже сохранённые результаты останутся.", "warn");
  } catch (err) {
    toast(err.message || String(err), "error");
    appendLog(`Stage 2 sync cancel: ${err.message || err}`, "error");
  }
}

async function confirmSyncMatch(matchId, card) {
  const match = syncMatches.find((item) =>
    String(item.segment_id || item.block_uid || "") === matchId
  );
  if (!match) throw new Error("Совпадение больше не найдено; обновите список");
  const candidates = candidateChoices(match);
  const selectedIndex = Number(card?.querySelector("[data-sync-candidate]")?.value || 0);
  const candidate = candidates[selectedIndex] || matchCandidate(match);
  const sourceIn = Number(card?.querySelector("[data-sync-in]")?.value ?? candidate.source_in_sec);
  const sourceOut = Number(card?.querySelector("[data-sync-out]")?.value ?? candidate.source_out_sec);
  const sourceId = String(card?.dataset.sourceId || candidate.source_id || "");
  if (!sourceId || !Number.isFinite(sourceIn) || !Number.isFinite(sourceOut)
    || sourceIn < 0 || sourceOut <= sourceIn) {
    throw new Error("Проверьте источник и границы Source in/out");
  }
  const payload = {
    project,
    expected_revision: syncMatchesRevision,
    status: "confirmed",
    source_id: sourceId,
    source_in_sec: sourceIn,
    source_out_sec: sourceOut,
  };
  ["speech_in_sec", "speech_out_sec", "matched_text", "confidence"].forEach((key) => {
    if (candidate[key] !== undefined) payload[key] = candidate[key];
  });
  const response = await optionalRequestJSON(
    `/stage2/sync-matches/${encodeURIComponent(matchId)}`,
    "PATCH",
    payload,
  );
  if (!response.available) {
    appendLog("Подтверждение СНХ недоступно в текущей версии backend.", "warn");
    return;
  }
  toast("Диапазон СНХ подтверждён", "success");
  appendLog(`Stage 2 sync: подтверждён ${matchId}.`);
  await loadSyncWorkspace(true);
}

ids.btn_sync_align.addEventListener("click", () => safely(startSyncAlignment));
if (ids.btn_sync_cancel) ids.btn_sync_cancel.addEventListener("click", cancelSyncJob);
if (ids.sync_asr_engine) {
  ids.sync_asr_engine.addEventListener("change", updateSyncAsrControls);
  updateSyncAsrControls();
}
if (ids.sync_sources_list) {
  ids.sync_sources_list.addEventListener("click", (event) => {
    const button = event.target.closest("[data-action='save-sync-source']");
    if (!button) return;
    const card = button.closest(".sync-source-card");
    const sourceId = String(card?.dataset.sourceId || "");
    const source = appState.syncSources.find((item) => sourceEditableId(item) === sourceId);
    if (!source) return;
    const speakersInput = card.querySelector("[data-sync-source-speakers]");
    const allCheckbox = card.querySelector("[data-sync-source-all]");
    safely(() => saveSyncSourceEdit(
      source,
      speakersInput ? speakersInput.value : "",
      Boolean(allCheckbox && allCheckbox.checked),
    ));
  });
}
ids.sync_review_list.addEventListener("click", (event) => {
  const button = event.target.closest("[data-action='confirm-sync-match']");
  if (!button) return;
  const matchId = String(button.dataset.matchId || "");
  if (matchId) safely(() => confirmSyncMatch(matchId, button.closest(".sync-match-card")));
});
ids.sync_review_list.addEventListener("change", (event) => {
  const select = event.target.closest("[data-sync-candidate]");
  const card = event.target.closest(".sync-match-card");
  if (select && card) {
    const match = syncMatches.find((item) =>
      String(item.segment_id || item.block_uid || "") === String(card.dataset.matchId || "")
    );
    const candidate = match ? candidateChoices(match)[Number(select.value)] : null;
    if (!candidate) return;
    const source = candidateSource(candidate);
    const sourceName = candidate.sequence_name || source.sequence_name
      || source.premiere_sequence || candidate.source_id;
    const confidence = Number(candidate.confidence);
    card.dataset.sourceId = candidate.source_id;
    card.querySelector("[data-sync-in]").value = Number(candidate.source_in_sec).toFixed(3);
    card.querySelector("[data-sync-out]").value = Number(candidate.source_out_sec).toFixed(3);
    card.querySelector("[data-sync-found]").textContent =
      candidate.matched_text || "Whisper-фрагмент не найден";
    card.querySelector("[data-sync-meta]").textContent =
      `${sourceName} · ${formatSourceSecond(candidate.source_in_sec)}–${formatSourceSecond(candidate.source_out_sec)}`
      + ` · уверенность ${Number.isFinite(confidence) ? Math.round(confidence * (confidence <= 1 ? 100 : 1)) + "%" : "—"}`;
    const audio = card.querySelector("[data-sync-audio]");
    audio.src = syncAudioUrl(candidate.source_id);
    audio.dataset.rangeIn = candidate.source_in_sec;
    audio.dataset.rangeOut = candidate.source_out_sec;
    audio.load();
  }
  if (card && event.target.matches("[data-sync-in], [data-sync-out]")) {
    const audio = card.querySelector("[data-sync-audio]");
    audio.dataset.rangeIn = card.querySelector("[data-sync-in]").value;
    audio.dataset.rangeOut = card.querySelector("[data-sync-out]").value;
  }
});
ids.sync_review_list.addEventListener("play", (event) => {
  const audio = event.target.closest("[data-sync-audio]");
  if (!audio) return;
  const start = Number(audio.dataset.rangeIn);
  const end = Number(audio.dataset.rangeOut);
  if (Number.isFinite(start) && (!Number.isFinite(audio.currentTime)
    || audio.currentTime < start || (Number.isFinite(end) && audio.currentTime >= end))) {
    audio.currentTime = start;
  }
}, true);
ids.sync_review_list.addEventListener("timeupdate", (event) => {
  const audio = event.target.closest("[data-sync-audio]");
  if (!audio) return;
  const end = Number(audio.dataset.rangeOut);
  if (Number.isFinite(end) && audio.currentTime >= end) audio.pause();
}, true);
