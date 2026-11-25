// Optional Russian transcript and translated-audio controls for sync review.
// The backend remains the source of truth; this module only renders its state.
import { appState, project } from "./state.js";
import { formData, postJSON, toast, escaped } from "./core.js";

const PHRASE_TIMEOUT_MS = 20 * 60 * 1000;
const sourceId = (source) => String(source?.id || source?.source_id || "");
const variants = (match) => match?.metadata?.translation_audio?.variants || {};
const translationConfig = (match) => match?.metadata?.translation_audio || {};
const semanticSummaries = new Map();

export function audioFileUrl(segmentId, kind = "") {
  const query = new URLSearchParams({ project });
  if (kind) query.set("kind", kind);
  return `/stage2/translated-sync/${encodeURIComponent(segmentId)}/audio-file?${query}`;
}

let activePreviewStopper = null;
export function stopTranslatedPreview() { activePreviewStopper?.(); }
async function refresh(callback) {
  stopTranslatedPreview();
  if (typeof callback === "function") await callback();
}

export async function uploadWordTranscript(source, card, callback) {
  const id = sourceId(source);
  const file = card?.querySelector("[data-ts-file]")?.files?.[0];
  if (!id || !file) throw new Error("Выберите файл Word (.docx)");
  const mode = card.querySelector("[data-ts-mode]")?.value || "relative";
  const start = card.querySelector("[data-ts-start]")?.value || "";
  const body = new FormData();
  body.set("project", project); body.set("file", file); body.set("timecode_mode", mode);
  if (start) body.set("sequence_start_tc", start);
  const controller = new AbortController(); const timer = window.setTimeout(() => controller.abort(), PHRASE_TIMEOUT_MS);
  let response;
  try { response = await fetch(`/stage2/sync-sources/${encodeURIComponent(id)}/transcript-docx`, { method: "POST", body, credentials: "same-origin", signal: controller.signal }); }
  finally { window.clearTimeout(timer); }
  const payload = await response.json();
  if (!response.ok || payload.ok === false) throw new Error(payload.error || `HTTP ${response.status}`);
  const result = payload.result || {};
  toast(`Word загружен: ${result.cue_count || 0} реплик`, "success");
  await refresh(callback);
  return result;
}

export async function findByWord(sources, callback, semantic = false) {
  const ids = (sources || []).map(sourceId).filter(Boolean);
  if (!ids.length) throw new Error("Нет выбранных источников с Word-файлом");
  const result = await postJSON("/stage2/translated-sync/match", { project, source_ids: ids, semantic }, semantic ? PHRASE_TIMEOUT_MS : undefined);
  if (semantic) {
    const details = result.semantic || {};
    const statuses = (details.statuses || []).map((item) => `${item.segment_id}: ${item.status}${item.error ? ` (${item.error})` : ""}`).join("; ");
    const summary = `Смысловой поиск: ${details.calls || 0}/${details.max_calls || 8} запросов; до ${details.max_candidates_per_unit || 24} реплик на цитату. ${details.limited ? "Результаты ограничены: уточните источник, время или формулировку. " : ""}${statuses}`;
    ids.forEach((id) => semanticSummaries.set(id, summary));
    toast(summary, "warn");
  }
  toast(`Поиск по Word завершён: ${(result.matches || []).length} совпадений`, "success");
  await refresh(callback);
  return result;
}

export function renderSourceControls(source) {
  const summary = source?.metadata?.translated_transcript;
  const count = Number(summary?.cue_count || 0);
  const status = summary ? `Загружено реплик: ${count} · ${summary.timecode_mode === "display" ? "таймкод камеры" : "относительное время"}` : "Файл Word не загружен";
  return `<div class="sync-transcript-controls" data-ts-source="${escaped(sourceId(source))}">
    <label class="sync-match-field"><span>Файл Word (.docx)</span><input type="file" accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document" data-ts-file></label>
    <label class="sync-match-field"><span>Время документа</span><select data-ts-mode><option value="relative"${summary?.timecode_mode !== "display" ? " selected" : ""}>От начала файла просмотра</option><option value="display"${summary?.timecode_mode === "display" ? " selected" : ""}>Таймкод камеры</option></select></label>
    <label class="sync-match-field"><span>Начальный TC зарегистрированного источника</span><input type="text" placeholder="01:00:00:00" data-ts-start value="${escaped(summary?.sequence_start_tc || source?.sequence_start_tc || "")}"></label>
    <button type="button" class="btn btn-sm" data-ts-upload>Загрузить Word</button>
    <label><input type="checkbox" data-ts-semantic> Поиск перефразировок через настроенную LLM: цитата сценария и русские переводы кандидатов будут отправлены модели (до 24 реплик и 8 запросов).</label>
    <button type="button" class="btn btn-sm btn-accent" data-ts-find>Найти по Word</button>
    <span class="sync-match-meta" data-ts-status>${escaped(status)} ${escaped(semanticSummaries.get(sourceId(source)) || "")}</span>
    <span class="sync-match-llm" data-ts-error hidden></span></div>`;
}

function variantOptions(available, active) {
  return ["tts", "recorded"].filter((key) => available[key]).map((key) =>
    `<option value="${key}"${active === key ? " selected" : ""}>${key === "tts" ? "Синтез" : "Запись"}</option>`
  ).join("");
}

export function renderTranslatedControls(match) {
  const cfg = translationConfig(match); const audio = variants(match); const active = cfg.active || "tts";
  const chosen = audio[active] || {};
  const sourceIn = Number(match.source_in_sec); const sourceOut = Number(match.source_out_sec);
  const assigned = match.status === "confirmed" && !match.metadata?.retime_pending_review
    && Boolean(match.source_id) && match.source_in_sec !== null && match.source_in_sec !== undefined
    && match.source_out_sec !== null && match.source_out_sec !== undefined
    && Number.isFinite(sourceIn) && Number.isFinite(sourceOut) && sourceIn >= 0 && sourceOut > sourceIn;
  const enabled = assigned && cfg.enabled !== false && Boolean(audio[active]);
  const selection = enabled ? active : "original";
  const source = (appState.syncSources || []).find((item) => sourceId(item) === String(match.source_id || "")) || {};
  const guideOffset = Number(source.guide_offset_sec || 0);
  const disabled = assigned ? "" : " disabled";
  return `<details class="sync-translated-audio" data-ts-sync="${escaped(match.segment_id || "")}" data-ts-can-generate="${assigned}" data-ts-source-id="${escaped(match.source_id || "")}" data-ts-source-in="${escaped(match.source_in_sec ?? 0)}" data-ts-source-out="${escaped(match.source_out_sec ?? 0)}" data-ts-speech-in="${escaped(match.metadata?.speech_in_sec ?? match.source_in_sec ?? 0)}" data-ts-guide-offset="${escaped(guideOffset)}" data-ts-source-url="/stage2/sync-sources/${encodeURIComponent(match.source_id || "")}/audio?project=${encodeURIComponent(project)}"><summary>Русская озвучка (по желанию)</summary>
    <div class="sync-review-controls"><label class="sync-match-field"><span>Источник</span><select data-ts-active${disabled}><option value="original"${selection === "original" ? " selected" : ""}>Только исходник</option>${variantOptions(audio, selection)}</select></label>
    <label class="sync-match-field"><span>Голос TTS</span><input data-ts-voice placeholder="Голос для перевода"></label>
    <label class="sync-match-field"><span>Пауза, сек</span><input type="number" step="0.05" data-ts-delay value="${escaped(cfg.delay_sec ?? 0.8)}"></label>
    <label class="sync-match-field"><span>Уровень, dB</span><input type="number" step="0.5" data-ts-gain value="${escaped(cfg.gain_db ?? 0)}"></label>
    <label class="sync-match-field"><span>Приглушение оригинала, dB</span><input type="number" step="0.5" data-ts-duck value="${escaped(cfg.duck_db ?? -18)}"></label>
    <label class="sync-match-field"><span>Переход приглушения, сек</span><input type="number" min="0" step="0.05" data-ts-fade value="${escaped(cfg.fade_sec ?? 0.2)}"></label>
    <label class="sync-match-field"><span>Начало записи, сек</span><input type="number" min="0" step="0.05" data-ts-in value="${escaped(chosen.source_in_sec ?? 0)}"></label>
    <label class="sync-match-field"><span>Конец записи, сек</span><input type="number" min="0" step="0.05" data-ts-out value="${escaped(chosen.source_out_sec ?? "")}"></label>
    <label class="sync-match-field"><span>Общая запись</span><select data-ts-recording data-ts-recording-current="${escaped(chosen.recording_id || "")}"><option value="">Выберите запись…</option></select></label>
    <input type="file" accept="audio/wav,audio/mpeg,.wav,.mp3" data-ts-audio>
    <button type="button" class="btn btn-sm" data-ts-generate${disabled}>Сгенерировать TTS</button><button type="button" class="btn btn-sm" data-ts-upload-audio${disabled}>Загрузить запись</button>
    <button type="button" class="btn btn-sm" data-ts-save${disabled}>Сохранить настройки</button>
    <button type="button" class="btn btn-sm" data-ts-mix-preview${enabled ? "" : " disabled"}>Прослушать исходник + перевод</button><button type="button" class="btn btn-sm" data-ts-stop-preview hidden>Остановить предпрослушивание</button>
    ${enabled ? `<audio controls preload="none" data-ts-preview src="${audioFileUrl(match.segment_id, active)}" data-ts-in="${escaped(chosen.source_in_sec ?? 0)}" data-ts-out="${escaped(chosen.source_out_sec ?? "")}"></audio>` : ""}
    ${assigned ? "" : '<span class="sync-match-llm" data-ts-prerequisite>Сначала назначьте источник и подтвердите корректный диапазон СНХ.</span>'}
    <span class="sync-match-llm" data-ts-error hidden></span></div></details>`;
}

async function jsonRequest(url, method, body) {
  const response = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify({ project, ...body }), credentials: "same-origin" });
  const payload = await response.json();
  if (!response.ok || payload.ok === false) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload.result || {};
}

function setError(card, error) { const node = card?.querySelector("[data-ts-error]"); if (node) { node.textContent = String(error?.message || error); node.hidden = false; } }
function values(card, includeRange = true) {
  const hasVariant = Boolean(card.querySelector("[data-ts-active]")?.value || card.querySelector("[data-ts-recording]")?.value);
  const entries = [["delay_sec", "[data-ts-delay]"], ["gain_db", "[data-ts-gain]"], ["duck_db", "[data-ts-duck]"], ["fade_sec", "[data-ts-fade]"], ["source_in_sec", "[data-ts-in]"], ["source_out_sec", "[data-ts-out]"], ["active", "[data-ts-active]"]];
  return Object.fromEntries(entries.map(([key, selector]) => [key, card.querySelector(selector)?.value])
    .filter(([key, value]) => value !== undefined && value !== ""
      && (includeRange || !["source_in_sec", "source_out_sec"].includes(key))
      && (hasVariant || !["source_in_sec", "source_out_sec"].includes(key))));
}

function settings(card) {
  const { active, ...result } = values(card, false);
  return result;
}

const previewStops = new WeakMap();

async function playMixedPreview(card) {
  const active = card.querySelector("[data-ts-active]")?.value || "tts";
  if (active === "original" || card.dataset.tsCanGenerate !== "true") throw new Error("Для совместного предпрослушивания сначала включите перевод у подтверждённого СНХ.");
  const cfg = values(card); const sourceIn = Number(card.dataset.tsSourceIn); const sourceOut = Number(card.dataset.tsSourceOut);
  const speechIn = Number(card.dataset.tsSpeechIn); const delay = Number(cfg.delay_sec || 0);
  if (!active || !Number.isFinite(sourceIn) || !Number.isFinite(sourceOut) || sourceOut <= sourceIn) throw new Error("Сначала подтвердите диапазон исходной реплики и выберите вариант перевода");
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) throw new Error("Совместный предпросмотр недоступен в этом браузере");
  const inField = Number(card.querySelector("[data-ts-in]")?.value || 0);
  const outValue = card.querySelector("[data-ts-out]")?.value;
  if (inField < 0 || (outValue !== "" && Number(outValue) <= inField)) throw new Error("Проверьте границы выбранной записи");
  activePreviewStopper?.();
  const originalDuration = sourceOut - sourceIn; const offset = Math.max(0, speechIn - sourceIn) + delay;
  const voiceDuration = outValue === "" ? Infinity : Number(outValue) - inField;
  const guideOffset = Number(card.dataset.tsGuideOffset || 0);
  const guideIn = Math.max(0, sourceIn - guideOffset); const guideOut = Math.max(guideIn, sourceOut - guideOffset);
  if (guideOut <= guideIn) throw new Error("Исходный диапазон не попадает в доступную запись");
  const context = new AudioContextClass();
  const originalMedia = new Audio(card.dataset.tsSourceUrl); const voiceMedia = new Audio(audioFileUrl(card.dataset.tsSync, active));
  originalMedia.preload = "metadata"; voiceMedia.preload = "metadata";
  const sourceGain = context.createGain(); const voiceGain = context.createGain();
  context.createMediaElementSource(originalMedia).connect(sourceGain); sourceGain.connect(context.destination);
  context.createMediaElementSource(voiceMedia).connect(voiceGain); voiceGain.connect(context.destination);
  originalMedia.currentTime = guideIn; voiceMedia.currentTime = inField;
  let stopped = false; let sourceDone = false; let voiceDone = false; const timers = [];
  const stop = () => {
    if (stopped) return; stopped = true;
    timers.forEach((timer) => clearTimeout(timer));
    originalMedia.pause(); voiceMedia.pause(); card.querySelector("[data-ts-stop-preview]").hidden = true;
    card.querySelector("[data-ts-mix-preview]").disabled = false; previewStops.delete(card);
    if (activePreviewStopper === stop) activePreviewStopper = null;
    void context.close();
  };
  const finishIfDone = () => { if (sourceDone && voiceDone) stop(); };
  previewStops.set(card, stop); activePreviewStopper = stop; card.querySelector("[data-ts-stop-preview]").hidden = false;
  card.querySelector("[data-ts-mix-preview]").disabled = true;
  originalMedia.ontimeupdate = () => { if (originalMedia.currentTime >= guideOut) { sourceDone = true; originalMedia.pause(); finishIfDone(); } };
  voiceMedia.ontimeupdate = () => { if (outValue !== "" && voiceMedia.currentTime >= Number(outValue)) { voiceDone = true; voiceMedia.pause(); finishIfDone(); } };
  voiceMedia.onended = () => { voiceDone = true; finishIfDone(); };
  originalMedia.onended = () => { sourceDone = true; finishIfDone(); };
  originalMedia.onerror = voiceMedia.onerror = () => { setError(card, new Error("Не удалось воспроизвести выбранный диапазон")); stop(); };
  const enforceBound = (media, bound, markDone) => {
    if (stopped) return;
    if (media.currentTime >= bound - 0.01) { markDone(); media.pause(); finishIfDone(); return; }
    timers.push(setTimeout(() => enforceBound(media, bound, markDone), 25));
  };
  try {
    await context.resume(); await originalMedia.play();
    const now = context.currentTime; const duck = 10 ** (Number(cfg.duck_db ?? -18) / 20);
    const overlapEnd = Math.min(originalDuration, offset + voiceDuration);
    const fade = Math.min(Number(cfg.fade_sec ?? 0.2), Math.max(0, (overlapEnd - offset) / 2));
    sourceGain.gain.setValueAtTime(1, now);
    if (overlapEnd > offset) {
      const duckStart = Math.max(0, offset - fade);
      sourceGain.gain.setValueAtTime(1, now + duckStart);
      sourceGain.gain.linearRampToValueAtTime(duck, now + offset);
      if (offset + voiceDuration < originalDuration) {
        const restoreStart = Math.max(offset, overlapEnd - fade);
        sourceGain.gain.setValueAtTime(duck, now + restoreStart);
        sourceGain.gain.linearRampToValueAtTime(1, now + overlapEnd);
      } else {
        sourceGain.gain.setValueAtTime(duck, now + originalDuration);
      }
    }
    voiceGain.gain.setValueAtTime(10 ** (Number(cfg.gain_db || 0) / 20), now);
    timers.push(setTimeout(() => {
      void voiceMedia.play().catch((error) => { setError(card, error); stop(); });
      if (Number.isFinite(voiceDuration)) timers.push(setTimeout(() => enforceBound(voiceMedia, inField + voiceDuration, () => { voiceDone = true; }), voiceDuration * 1000));
    }, offset * 1000));
    timers.push(setTimeout(() => enforceBound(originalMedia, guideOut, () => { sourceDone = true; }), originalDuration * 1000));
  }
  catch (error) { stop(); throw error; }
}

async function handleReview(event, callback) {
  const button = event.target.closest?.("[data-ts-generate],[data-ts-upload-audio],[data-ts-save],[data-ts-mix-preview],[data-ts-stop-preview]"); if (!button) return;
  const card = button.closest("[data-ts-sync]"); const id = card?.dataset.tsSync; if (!id) return;
  try {
    let result;
    if (button.matches?.("[data-ts-stop-preview]")) { previewStops.get(card)?.(); return; }
    if (button.matches?.("[data-ts-mix-preview]")) { await playMixedPreview(card); return; }
    if ((button.matches?.("[data-ts-generate]") || button.matches?.("[data-ts-upload-audio]")) && card.dataset.tsCanGenerate !== "true") {
      throw new Error("Сначала назначьте источник, проверьте диапазон и подтвердите фрагмент СНХ.");
    }
    if (button.matches?.("[data-ts-generate]")) {
      const defaults = formData();
      result = await postJSON(`/stage2/translated-sync/${encodeURIComponent(id)}/tts`, { project, provider: defaults.provider, tts_model: defaults.tts_model, voice_name: card.querySelector("[data-ts-voice]")?.value || null, gender: defaults.gender, speed: defaults.speed, tts_style: defaults.tts_style }, PHRASE_TIMEOUT_MS);
    } else if (button.matches?.("[data-ts-upload-audio]")) {
      const file = card.querySelector("[data-ts-audio]")?.files?.[0]; if (!file) throw new Error("Выберите аудиофайл");
      const body = new FormData(); body.set("project", project); body.set("audio", file);
      const response = await fetch(`/stage2/translated-sync/${encodeURIComponent(id)}/recording`, { method: "POST", body, credentials: "same-origin" }); const data = await response.json(); if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`); result = data.result;
    } else {
      const selected = card.querySelector("[data-ts-active]")?.value || "original";
      const payload = selected === "original"
        ? { ...settings(card), enabled: false }
        : { ...values(card), enabled: true, active: selected };
      result = await jsonRequest(`/stage2/translated-sync/${encodeURIComponent(id)}/audio`, "PATCH", payload);
    }
    toast("Русская озвучка обновлена", "success"); await refresh(callback); return result;
  } catch (error) { setError(card, error); }
}

export function bindTranslatedSync({ sourceList, reviewList, getSources, onRefresh }) {
  if (sourceList && !sourceList.dataset.tsBound) {
    sourceList.dataset.tsBound = "1";
    sourceList.addEventListener("click", async (event) => {
      const button = event.target.closest?.("[data-ts-upload],[data-ts-find]"); if (!button) return;
      const card = button.closest("[data-ts-source]"); const source = (getSources?.() || []).find((item) => sourceId(item) === card?.dataset.tsSource);
      button.disabled = true;
      const oldLabel = button.textContent; button.textContent = "Поиск…";
      try { if (button.matches("[data-ts-upload]")) await uploadWordTranscript(source, card, onRefresh); else await findByWord([source], onRefresh, Boolean(card.querySelector("[data-ts-semantic]")?.checked)); }
      catch (error) { setError(card, error); }
      finally { button.disabled = false; button.textContent = oldLabel; }
    });
  }
  if (reviewList && !reviewList.dataset.tsBound) { reviewList.dataset.tsBound = "1"; reviewList.addEventListener("click", (event) => { void handleReview(event, onRefresh); });
    reviewList.addEventListener("toggle", async (event) => {
      const details = event.target.closest?.("[data-ts-sync]"); if (!details || !details.open || details.dataset.tsLoaded) return;
      if (details.dataset.tsCanGenerate !== "true") return;
      details.dataset.tsLoaded = "1";
      try {
        const response = await fetch(`/stage2/translated-sync/${encodeURIComponent(details.dataset.tsSync)}/audio?project=${encodeURIComponent(project)}`, { credentials: "same-origin" });
        const payload = await response.json(); if (!response.ok || payload.ok === false) throw new Error(payload.error || `HTTP ${response.status}`);
        const select = details.querySelector("[data-ts-recording]");
        for (const recording of payload.result?.recordings || []) {
          const option = document.createElement("option"); option.value = recording.id;
          option.textContent = recording.part_name || recording.name || recording.filename || "Запись";
          if (recording.id === select?.dataset.tsRecordingCurrent) option.selected = true;
          select?.appendChild(option);
        }
      } catch (error) { details.dataset.tsLoaded = ""; setError(details, error); }
    }, true);
    reviewList.addEventListener("change", async (event) => {
      const active = event.target.closest?.("[data-ts-active]"); if (!active) return;
      const card = active.closest("[data-ts-sync]");
      try {
        const payload = active.value === "original"
          ? { ...settings(card), enabled: false }
          : { ...settings(card), enabled: true, active: active.value };
        const result = await jsonRequest(`/stage2/translated-sync/${encodeURIComponent(card.dataset.tsSync)}/audio`, "PATCH", payload);
        await refresh(onRefresh); return result;
      } catch (error) { setError(card, error); }
    });
    reviewList.addEventListener("change", async (event) => {
      const selection = event.target.closest?.("[data-ts-recording]"); if (!selection?.value) return;
      const card = selection.closest("[data-ts-sync]");
      try { await jsonRequest(`/stage2/translated-sync/${encodeURIComponent(card.dataset.tsSync)}/audio`, "PATCH", { ...settings(card), recording_id: selection.value, enabled: true, active: "recorded" }); await refresh(onRefresh); }
      catch (error) { setError(card, error); }
    });
    reviewList.addEventListener("change", (event) => {
      const card = event.target.closest?.("[data-ts-sync]"); if (card) previewStops.get(card)?.();
    }, true);
    reviewList.addEventListener("input", (event) => {
      const card = event.target.closest?.("[data-ts-sync]"); if (card) previewStops.get(card)?.();
    }, true);
    reviewList.addEventListener("timeupdate", (event) => { const audio = event.target.closest?.("[data-ts-preview]"); if (!audio) return; const end = Number(audio.dataset.tsOut); if (Number.isFinite(end) && end > 0 && audio.currentTime >= end) { audio.pause(); audio.currentTime = Number(audio.dataset.tsIn) || 0; } }, true);
    reviewList.addEventListener("play", (event) => { const audio = event.target.closest?.("[data-ts-preview]"); if (audio && Number(audio.dataset.tsIn) > audio.currentTime) audio.currentTime = Number(audio.dataset.tsIn); }, true);
  }
}
