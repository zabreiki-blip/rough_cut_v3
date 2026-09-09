// Scenario normalization: draft editing, memory rules and the shared
// Stage 1 status refresh (which also updates the normalization badge).

import { ids, appState, project } from "./state.js";
import {
  toast, appendLog, requestJSON, confirmDialog, escaped,
  blockTypeLabels, sourceLabels, setStepState, updateActionAvailability,
  assertRequiredPath, safely,
} from "./core.js";
import { renderOpenRouterUsage } from "./usage.js";

let memoryRules = [];
let normalizationEditGeneration = 0;
let issueFilterEnabled = false;
let activeIssueUid = "";

function canonicalDocument(value) {
  if (!value) return null;
  const doc = value.normalization || value.document || value;
  if (!Array.isArray(doc.blocks)) doc.blocks = [];
  if (!doc.status) doc.status = "draft";
  if (doc.revision == null) doc.revision = 0;
  doc.warnings = Array.isArray(doc.warnings) ? doc.warnings : [];
  return doc;
}

export function hasUnsavedChanges() {
  return Boolean(appState.normalizationDirty);
}

export function markNormalizationDirty() {
  if (!appState.normalization) return;
  normalizationEditGeneration += 1;
  appState.normalizationDirty = true;
  appState.normalizationEligible = false;
  if (appState.normalization.status === "confirmed") appState.normalization.status = "draft";
  renderNormalizationStatus();
  updateActionAvailability();
}

function blockValue(block, ...names) {
  for (const name of names) if (block[name] != null) return block[name];
  return "";
}

function blockNeedsReview(block) {
  const type = block.type || block.block_type || "unknown";
  const status = String(block.status || block.review_status || "").trim().toLowerCase();
  const warnings = Array.isArray(block.warnings) ? block.warnings : [];
  const role = String(block.role || "").trim().toLocaleLowerCase("ru");
  return type === "unknown"
    || status === "needs_review"
    || warnings.length > 0
    || (type === "voiceover" && !String(block.text_for_tts || "").trim())
    || ["sync", "снх", "синхрон"].includes(role);
}

function issueBlocks(blocks) {
  return blocks.filter(blockNeedsReview);
}

function updateIssueNavigation(blocks) {
  const issues = issueBlocks(blocks);
  if (!issues.length) issueFilterEnabled = false;
  if (!issues.some((block) => block.uid === activeIssueUid)) {
    activeIssueUid = issues[0]?.uid || "";
  }
  const position = issues.findIndex((block) => block.uid === activeIssueUid);
  ids.norm_issue_position.textContent = issues.length
    ? `Проблема ${position + 1} из ${issues.length}`
    : "Проблем нет";
  ids.btn_norm_issue_prev.disabled = issues.length === 0;
  ids.btn_norm_issue_next.disabled = issues.length === 0;
  ids.btn_norm_issue_filter.disabled = issues.length === 0;
  ids.btn_norm_issue_filter.setAttribute("aria-pressed", String(issueFilterEnabled));
  ids.btn_norm_issue_filter.textContent = issueFilterEnabled
    ? "Показать все блоки"
    : "Показать только требующие проверки";
}

function focusIssue(offset) {
  const blocks = appState.normalization?.blocks || [];
  const issues = issueBlocks(blocks);
  if (!issues.length) return;
  let position = issues.findIndex((block) => block.uid === activeIssueUid);
  if (position < 0) position = 0;
  position = (position + offset + issues.length) % issues.length;
  activeIssueUid = issues[position].uid;
  renderNormalization();
  requestAnimationFrame(() => {
    const card = Array.from(ids.normalization_blocks.querySelectorAll(".normalization-block"))
      .find((item) => item.dataset.uid === activeIssueUid);
    if (!card) return;
    card.scrollIntoView({ behavior: "smooth", block: "center" });
    const control = card.querySelector("select, textarea:not([readonly]), input:not([readonly])");
    if (control) control.focus({ preventScroll: true });
  });
}

export function renderNormalizationStatus() {
  let state = appState.normalization ? (appState.normalization.status || "draft") : "empty";
  if (state === "confirmed" && !appState.normalizationEligible) state = "stale";
  const labels = { empty: "не создан", draft: "черновик", confirmed: "подтверждён", stale: "нужно подтвердить заново" };
  ids.normalization_status.dataset.state = state;
  ids.normalization_status.textContent = labels[state] || state;
}

export function renderNormalization() {
  renderNormalizationStatus();
  const blocks = appState.normalization && Array.isArray(appState.normalization.blocks) ? appState.normalization.blocks : [];
  const counts = blocks.reduce((acc, block) => {
    const type = block.type || block.block_type || "unknown";
    acc[type] = (acc[type] || 0) + 1;
    return acc;
  }, {});
  ids.norm_count_total.textContent = String(blocks.length);
  ids.norm_count_voiceover.textContent = String(counts.voiceover || 0);
  ids.norm_count_sync.textContent = String(counts.sync || 0);
  ids.norm_count_life.textContent = String(counts.life || 0);
  ids.norm_count_unknown.textContent = String(counts.unknown || 0);
  updateIssueNavigation(blocks);

  const warnings = (appState.normalization && appState.normalization.warnings ? appState.normalization.warnings : []).concat(
    blocks.flatMap((block) => Array.isArray(block.warnings) ? block.warnings : [])
  ).filter(Boolean);
  const warningGroups = new Map();
  warnings.forEach((warning) => {
    const raw = String(warning || "").trim();
    let message = raw;
    if (/^LLM classification failed:\s*JSONDecodeError/i.test(raw)) {
      message = "LLM вернул некорректный JSON — автоклассификация не применена";
    } else {
      const confidence = raw.match(/^LLM confidence\s+([^\s]+)\s+is below/i);
      if (confidence) {
        message = `Низкая уверенность LLM (${confidence[1]}) — требуется проверка`;
      }
    }
    warningGroups.set(message, (warningGroups.get(message) || 0) + 1);
  });
  const groupedWarnings = Array.from(warningGroups.entries());
  const visibleWarnings = groupedWarnings.slice(0, 8);
  ids.normalization_warnings.hidden = groupedWarnings.length === 0;
  ids.normalization_warnings.innerHTML = groupedWarnings.length
    ? `<strong>Требует внимания:</strong><br>${visibleWarnings.map(([message, count]) =>
        `${escaped(message)}${count > 1 ? ` <span class="warning-count">· ${count} блоков</span>` : ""}`
      ).join("<br>")}${groupedWarnings.length > visibleWarnings.length
        ? `<br>Ещё ${groupedWarnings.length - visibleWarnings.length} типов предупреждений`
        : ""}`
    : "";
  ids.normalization_empty.hidden = blocks.length > 0;
  ids.normalization_blocks.innerHTML = "";

  blocks.forEach((block, index) => {
    const type = block.type || block.block_type || "unknown";
    const source = block.source || block.decision_source || "unknown";
    const blockWarnings = Array.isArray(block.warnings) ? block.warnings : [];
    const needsReview = blockNeedsReview(block);
    if (issueFilterEnabled && !needsReview) return;
    const syncFragments = Array.isArray(block.sync_fragments)
      ? block.sync_fragments.filter((fragment) => fragment && typeof fragment === "object")
      : [];
    const hasMultipleFragments = type === "sync" && syncFragments.length > 1;
    const card = document.createElement("article");
    card.className = `normalization-block${needsReview ? " has-warning" : ""}${block.uid === activeIssueUid ? " is-active-issue" : ""}`;
    card.dataset.index = String(index);
    card.dataset.uid = String(block.uid || "");
    card.innerHTML = `
      <div class="block-head">
        <span class="block-order">#${index + 1}</span>
        <select data-field="type" aria-label="Тип блока">
          ${Object.entries(blockTypeLabels).map(([value, label]) => `<option value="${value}"${value === type ? " selected" : ""}>${label}</option>`).join("")}
        </select>
        <span class="decision-badge" data-source="${escaped(source)}">${escaped(sourceLabels[source] || source)}</span>
        <button class="btn btn-sm" data-action="remember-block" type="button" title="Добавить обозначение в память">Запомнить</button>
        <button class="btn btn-sm" data-action="move-up" type="button" title="Переместить выше"${index === 0 ? " disabled" : ""}>↑</button>
        <button class="btn btn-sm" data-action="move-down" type="button" title="Переместить ниже"${index === blocks.length - 1 ? " disabled" : ""}>↓</button>
        <button class="btn block-delete" data-action="delete-block" type="button" title="Удалить блок">×</button>
      </div>
      <div class="block-fields">
        <label class="field block-original"><span>Исходный текст</span><textarea data-field="original_text"${blockValue(block, "original_text", "text_original", "source_text") ? " readonly" : ""}>${escaped(blockValue(block, "original_text", "text_original", "source_text"))}</textarea></label>
        <label class="field"><span>Нормализованный текст</span><textarea data-field="normalized_text"${hasMultipleFragments ? " readonly title=\"Редактируйте отдельные фрагменты ниже\"" : ""}>${escaped(blockValue(block, "normalized_text", "text"))}</textarea></label>
        <label class="field"><span>Текст для TTS</span><textarea data-field="text_for_tts">${escaped(blockValue(block, "text_for_tts", "tts_text"))}</textarea></label>
      </div>
      <div class="block-meta">
        <label class="field"><span>Исходное обозначение</span><input data-field="original_label" value="${escaped(blockValue(block, "original_label", "original_marker", "marker"))}"></label>
        <label class="field"><span>Спикер</span><input data-field="speaker" value="${escaped(blockValue(block, "speaker", "speaker_name"))}"></label>
        <label class="field"><span>Роль / должность</span><input data-field="role" value="${escaped(blockValue(block, "role"))}"></label>
        <label class="field"><span>Описание источника</span><input data-field="source_description" value="${escaped(blockValue(block, "source_description"))}"></label>
        <label class="field"><span>Таймкод начала</span><input data-field="timecode_start" value="${escaped(blockValue(block, "timecode_start", "timecode", "start_timecode"))}"${hasMultipleFragments ? " readonly title=\"Редактируйте таймкоды отдельных фрагментов ниже\"" : ""}></label>
        <label class="field"><span>Таймкод конца</span><input data-field="timecode_end" value="${escaped(blockValue(block, "timecode_end", "end_timecode"))}"${hasMultipleFragments ? " readonly title=\"Редактируйте таймкоды отдельных фрагментов ниже\"" : ""}></label>
        <label class="field"><span>Заметка</span><input data-field="notes" value="${escaped(blockValue(block, "notes"))}"></label>
      </div>
      ${hasMultipleFragments ? `<div class="sync-fragment-editor">
        <div class="sync-fragment-heading">Фрагменты спикера · ${syncFragments.length}</div>
        ${syncFragments.map((fragment, fragmentIndex) => `
          <div class="sync-fragment-row" data-fragment-index="${fragmentIndex}">
            <span class="sync-fragment-number">${fragmentIndex + 1}</span>
            <label class="field"><span>Начало</span><input data-fragment-field="timecode_start" value="${escaped(fragment.timecode_start || "")}"></label>
            <label class="field"><span>Конец</span><input data-fragment-field="timecode_end" value="${escaped(fragment.timecode_end || "")}"></label>
            <label class="field sync-fragment-text"><span>Текст фрагмента</span><textarea data-fragment-field="text">${escaped(fragment.text || "")}</textarea></label>
          </div>`).join("")}
      </div>` : ""}
      ${blockWarnings.length ? `<div class="block-warning">${blockWarnings.map(escaped).join(" · ")}</div>` : ""}`;
    ids.normalization_blocks.appendChild(card);
  });
  updateActionAvailability();
}

export function updateBlockFromControl(control) {
  const card = control.closest(".normalization-block");
  if (!card || !appState.normalization) return;
  const block = appState.normalization.blocks[Number(card.dataset.index)];
  if (!block) return;
  if (control.dataset.fragmentField) {
    const fragmentIndex = Number(control.closest("[data-fragment-index]")?.dataset.fragmentIndex);
    const fragments = Array.isArray(block.sync_fragments) ? block.sync_fragments : [];
    const fragment = fragments[fragmentIndex];
    if (!fragment) return;
    fragment[control.dataset.fragmentField] = control.value;
    block.normalized_text = fragments.map((item) => String(item.text || "").trim())
      .filter(Boolean).join(" ");
    const firstTimed = fragments.find((item) => String(item.timecode_start || "").trim()) || fragments[0];
    block.timecode_start = String(firstTimed?.timecode_start || "");
    block.timecode_end = String(firstTimed?.timecode_end || "");
    const normalizedControl = card.querySelector('[data-field="normalized_text"]');
    const startControl = card.querySelector('[data-field="timecode_start"]');
    const endControl = card.querySelector('[data-field="timecode_end"]');
    if (normalizedControl) normalizedControl.value = block.normalized_text;
    if (startControl) startControl.value = block.timecode_start;
    if (endControl) endControl.value = block.timecode_end;
    markNormalizationDirty();
    return;
  }
  block[control.dataset.field] = control.value;
  if (Array.isArray(block.sync_fragments) && block.sync_fragments.length === 1) {
    if (control.dataset.field === "normalized_text") {
      block.sync_fragments[0].text = control.value;
    } else if (control.dataset.field === "timecode_start") {
      block.sync_fragments[0].timecode_start = control.value;
    } else if (control.dataset.field === "timecode_end") {
      block.sync_fragments[0].timecode_end = control.value;
    }
  }
  if (control.dataset.field === "type") block.block_type = control.value;
  if (["type", "original_label"].includes(control.dataset.field)) {
    block.source = "manual";
    block.decision_source = "manual";
  }
  if (control.dataset.field === "type") {
    block.warnings = [];
    if (control.value === "voiceover" && !block.text_for_tts) {
      block.text_for_tts = block.normalized_text || "";
    }
    if (control.value === "life") {
      block.text_for_tts = "";
      block.speaker = "";
      block.role = "";
    }
  }
  markNormalizationDirty();
  if (control.dataset.field === "type") renderNormalization();
}

export async function loadNormalization(quiet) {
  try {
    const result = await requestJSON(`/stage1/normalization?project=${encodeURIComponent(project)}`, "GET");
    appState.normalization = canonicalDocument(result);
    appState.normalizationDirty = false;
    renderNormalization();
  } catch (err) {
    if (!quiet && !String(err.message).includes("404")) throw err;
    appState.normalization = null;
    renderNormalization();
  }
}

export async function generateNormalization() {
  const inputPath = ids.normalization_input_path.value.trim();
  assertRequiredPath(inputPath, "input_path");
  appendLog(`Нормализация: ${inputPath}`);
  const started = await requestJSON("/stage1/normalization-jobs/start", "POST", {
    project, input_path: inputPath, hint: ids.normalization_hint.value.trim(),
    use_llm: ids.normalization_use_llm.checked,
    llm_preset: ids.normalization_llm_preset?.value || "standard",
  });
  const jobId = String(started.id || "").trim();
  if (!jobId) throw new Error("Сервер не вернул ID задачи нормализации");
  appendLog(`Нормализация поставлена в очередь: ${jobId}`);
  let state = started;
  while (["queued", "running"].includes(String(state.status || "").toLowerCase())) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    state = await requestJSON(
      `/stage1/normalization-jobs/${encodeURIComponent(jobId)}`,
      "GET",
    );
  }
  if (state.status !== "completed") {
    throw new Error(state.error || state.message || "Нормализация завершилась с ошибкой");
  }
  appState.normalization = canonicalDocument(state.result);
  appState.normalizationEligible = false;
  appState.normalizationDirty = false;
  renderNormalization();
  toast(`Черновик создан: ${appState.normalization.blocks.length} блоков`, "success");
}

function normalizationPayload() {
  appState.normalization.blocks.forEach((block, index) => { block.order = index + 1; });
  return { project, revision: appState.normalization.revision, status: "draft", blocks: appState.normalization.blocks };
}

export async function saveNormalization() {
  const savedGeneration = normalizationEditGeneration;
  const result = await requestJSON("/stage1/normalization", "PUT", normalizationPayload());
  if (normalizationEditGeneration !== savedGeneration) {
    toast("Черновик сохранён; новые правки ещё не сохранены", "success");
    return appState.normalization;
  }
  appState.normalization = canonicalDocument(result);
  appState.normalizationDirty = false;
  renderNormalization();
  toast("Черновик сохранён", "success");
  return appState.normalization;
}

function chooseLearningDecisions(candidates) {
  ids.learning_rule_list.innerHTML = candidates.map((candidate) => {
    const fromLabel = blockTypeLabels[candidate.from_type] || candidate.from_type;
    const toLabel = blockTypeLabels[candidate.target_type] || candidate.target_type;
    const snippet = (candidate.snippets || [])[0] || "";
    const progress = Number(candidate.confirmations || 0);
    return `<div class="learning-rule" data-candidate-id="${escaped(candidate.id)}">
      <div><strong>«${escaped(candidate.marker)}» · ${escaped(fromLabel)} → ${escaped(toLabel)}</strong>
      <small>${escaped(snippet)}${progress ? `<br>Подтверждений: ${progress}/2` : ""}</small></div>
      <select data-learning-action aria-label="Действие с правилом ${escaped(candidate.marker)}">
        <option value="this_time" selected>Только в этом сценарии</option>
        <option value="remember">Запомнить</option>
      </select>
    </div>`;
  }).join("");
  return new Promise((resolve) => {
    const cancel = () => {
      ids.learning_dialog.close();
      resolve(null);
    };
    ids.btn_cancel_learning.onclick = cancel;
    ids.learning_dialog.oncancel = (event) => { event.preventDefault(); cancel(); };
    ids.btn_confirm_with_learning.onclick = () => {
      const decisions = Array.from(ids.learning_rule_list.querySelectorAll(".learning-rule")).map((row) => ({
        candidate_id: row.dataset.candidateId,
        action: row.querySelector("[data-learning-action]").value,
      }));
      ids.learning_dialog.close();
      resolve(decisions);
    };
    ids.learning_dialog.showModal();
  });
}

export async function resetNormalization() {
  if (!appState.normalization) return;
  const accepted = await confirmDialog({
    title: "Очистить всю нормализацию?",
    message: "Все блоки и ручные правки будут удалены. " +
      "Исходник, WAV и память обозначений не изменятся.",
    danger: true,
  });
  if (!accepted) return;
  const result = await requestJSON("/stage1/normalization/reset", "POST", {
    project, revision: appState.normalization.revision,
  });
  appState.normalization = canonicalDocument(result);
  appState.normalizationEligible = false;
  appState.normalizationDirty = false;
  ids.normalization_hint.value = "";
  renderNormalization();
  toast("Нормализация очищена. Можно запустить её заново.", "success");
  appendLog("Нормализация очищена: блоки удалены");
  await refreshStatus();
}

export async function confirmNormalization() {
  const unknown = appState.normalization.blocks.filter((block) => (block.type || block.block_type) === "unknown").length;
  if (unknown) throw new Error(`Исправьте неопределённые блоки: ${unknown}`);
  if (appState.normalizationDirty) await saveNormalization();
  const preview = await requestJSON("/stage1/normalization/confirm/preview", "POST", {
    project, revision: appState.normalization.revision,
  });
  const candidates = preview.candidates || [];
  const learningDecisions = candidates.length ? await chooseLearningDecisions(candidates) : [];
  if (learningDecisions === null) return;
  const result = await requestJSON("/stage1/normalization/confirm", "POST", {
    project, revision: appState.normalization.revision, learning_decisions: learningDecisions,
  });
  appState.normalization = canonicalDocument(result);
  appState.normalization.status = "confirmed";
  appState.normalizationEligible = true;
  appState.normalizationDirty = false;
  renderNormalization();
  toast("Сценарий подтверждён. Озвучка и сборка доступны.", "success");
  await loadMemory(true);
  await refreshStatus();
}

function renderSpeaker(files) {
  ids.speaker_items.innerHTML = "";
  if (!files.length) {
    ids.speaker_items.innerHTML = '<p style="font-size:12px;color:var(--muted);padding:8px;">Нет файлов</p>';
    return;
  }
  files.forEach((file) => {
    const wrap = document.createElement("div");
    wrap.className = "audio-item";

    const name = document.createElement("span");
    name.className = "audio-name";
    name.textContent = file.replace(".wav", "");

    const audio = document.createElement("audio");
    audio.controls = true;
    audio.preload = "none";
    audio.src = `/speaker/${encodeURIComponent(project)}/${encodeURIComponent(file)}`;

    wrap.appendChild(name);
    wrap.appendChild(audio);
    ids.speaker_items.appendChild(wrap);
  });
}

function renderMemory() {
  const query = (ids.memory_search.value || "").trim().toLocaleLowerCase("ru");
  const typeFilter = ids.memory_filter_type.value;
  const statusFilter = ids.memory_filter_status.value;
  const rules = memoryRules.filter((rule) => {
    const alias = String(rule.alias || rule.marker || rule.normalized_marker || "");
    const type = rule.block_type || rule.target_type || rule.type || "unknown";
    const status = rule.status || "active";
    return (!query || alias.toLocaleLowerCase("ru").includes(query))
      && (!typeFilter || type === typeFilter)
      && (!statusFilter || status === statusFilter);
  });
  ids.memory_rules.innerHTML = "";
  if (!rules.length) {
    ids.memory_rules.innerHTML = '<div class="empty-state compact">Нет подходящих обозначений.</div>';
    return;
  }
  rules.forEach((rule) => {
    const id = rule.id || rule.rule_id;
    const alias = rule.alias || rule.marker || rule.normalized_marker || "";
    const type = rule.block_type || rule.target_type || rule.type || "unknown";
    const status = rule.status || "active";
    const source = rule.source || "manual";
    const confirmations = Number(rule.confirmations || rule.confirmation_count || 0);
    const corrections = Number(rule.corrections || rule.correction_count || 0);
    const row = document.createElement("div");
    row.className = "memory-rule";
    row.dataset.ruleId = String(id || "");
    row.innerHTML = `
      <div class="memory-rule-head">
        <input class="memory-rule-name" data-memory-field="alias" value="${escaped(alias)}" aria-label="Обозначение">
        <select data-memory-field="block_type" aria-label="Тип обозначения">
          ${Object.entries(blockTypeLabels).filter(([value]) => value !== "unknown").map(([value, label]) => `<option value="${value}"${value === type ? " selected" : ""}>${label}</option>`).join("")}
        </select>
      </div>
      <div class="memory-rule-meta">${escaped(sourceLabels[source] || source)} · ${escaped(status)} · подтверждений ${confirmations} · исправлений ${corrections}</div>
      <div class="memory-rule-actions">
        ${status !== "active" ? '<button class="btn btn-sm" data-action="activate-memory" type="button">Активировать</button>' : '<button class="btn btn-sm" data-action="disable-memory" type="button">Отключить</button>'}
        <button class="btn btn-sm" data-action="delete-memory" type="button">Удалить</button>
      </div>`;
    ids.memory_rules.appendChild(row);
  });
}

export async function loadMemory(quiet) {
  try {
    const result = await requestJSON(`/stage1/normalization/memory?project=${encodeURIComponent(project)}`, "GET");
    memoryRules = Array.isArray(result) ? result : (result.rules || result.items || []);
    renderMemory();
  } catch (err) {
    if (!quiet) throw err;
    memoryRules = [];
    renderMemory();
  }
}

async function addMemoryRule() {
  const alias = ids.memory_alias.value.trim();
  if (!alias) {
    ids.memory_alias.focus();
    throw new Error("Введите обозначение");
  }
  await requestJSON("/stage1/normalization/memory", "POST", {
    project, alias, marker: alias, block_type: ids.memory_type.value, status: "active", source: "manual",
  });
  ids.memory_alias.value = "";
  await loadMemory(false);
  toast("Обозначение добавлено в память", "success");
}

async function updateMemoryRule(ruleId, patch) {
  await requestJSON(`/stage1/normalization/memory/${encodeURIComponent(ruleId)}`, "PATCH", patch);
  await loadMemory(false);
}

async function deleteMemoryRule(ruleId) {
  const accepted = await confirmDialog({
    title: "Удалить правило из памяти?",
    message: "Удалить это правило из общей памяти обозначений?",
    danger: true,
  });
  if (!accepted) return;
  await requestJSON(`/stage1/normalization/memory/${encodeURIComponent(ruleId)}?project=${encodeURIComponent(project)}`, "DELETE");
  await loadMemory(false);
  toast("Правило памяти удалено", "success");
}

export function applyStatus(status) {
  renderOpenRouterUsage(status.openrouter_usage || {});
  const prepared = Number(status.prepared_items_count || 0);
  const wav = Number(status.speaker_files_count || 0);
  const missing = Array.isArray(status.missing_ids) ? status.missing_ids.length : 0;
  const pct = prepared > 0 ? Math.round((wav / prepared) * 100) : 0;

  ids.m_prepared.textContent = String(prepared);
  ids.m_wav.textContent = String(wav);
  ids.m_missing.textContent = String(missing);
  ids.m_progress.textContent = `${pct}%`;
  ids.f_llm.textContent = status.used_llm ? "yes" : "no";
  ids.f_manual.textContent = status.manual_markers_used ? "yes" : "no";
  ids.f_manifest.textContent = status.manifest_exists ? "ready" : "missing";
  ids.progress_bar.style.width = `${Math.max(0, Math.min(100, pct))}%`;
  ids.speaker_hint.textContent = `${wav} файлов`;

  // Update pipeline steps
  if (prepared > 0) {
    setStepState(ids.step_analyze, "completed");
  }
  if (wav > 0 && missing === 0 && prepared > 0) {
    setStepState(ids.step_tts, "completed");
  } else if (wav > 0) {
    setStepState(ids.step_tts, "active");
  }
  if (status.manifest_exists) {
    setStepState(ids.step_validate, "completed");
  }

  renderSpeaker(Array.isArray(status.speaker_files) ? status.speaker_files : []);
  const normalizationStatus = status.normalization_status || (status.normalization && status.normalization.status);
  if (typeof status.normalization_confirmed === "boolean") appState.normalizationEligible = status.normalization_confirmed;
  if (normalizationStatus && appState.normalization && !appState.normalizationDirty) {
    appState.normalization.status = normalizationStatus;
    renderNormalizationStatus();
  }
  updateActionAvailability();
}

export async function refreshStatus() {
  const response = await fetch(`/stage1/status?project=${encodeURIComponent(project)}`);
  const data = await response.json();
  applyStatus(data.result || {});
}

ids.normalization_blocks.addEventListener("input", (event) => {
  if (event.target.dataset.field || event.target.dataset.fragmentField) updateBlockFromControl(event.target);
});
ids.normalization_blocks.addEventListener("change", (event) => {
  if (event.target.dataset.field || event.target.dataset.fragmentField) updateBlockFromControl(event.target);
});
ids.normalization_blocks.addEventListener("click", (event) => {
  const button = event.target.closest("[data-action]");
  if (!button || !appState.normalization) return;
  const card = button.closest(".normalization-block");
  const block = appState.normalization.blocks[Number(card.dataset.index)];
  if (button.dataset.action === "remember-block") {
    ids.memory_alias.value = block.original_label || "";
    ids.memory_type.value = block.type || "voiceover";
    ids.memory_alias.focus();
    return;
  }
  if (button.dataset.action === "move-up" || button.dataset.action === "move-down") {
    const index = Number(card.dataset.index);
    const target = index + (button.dataset.action === "move-up" ? -1 : 1);
    if (target >= 0 && target < appState.normalization.blocks.length) {
      [appState.normalization.blocks[index], appState.normalization.blocks[target]] =
        [appState.normalization.blocks[target], appState.normalization.blocks[index]];
      markNormalizationDirty();
      renderNormalization();
    }
    return;
  }
  if (button.dataset.action !== "delete-block") return;
  appState.normalization.blocks.splice(Number(card.dataset.index), 1);
  markNormalizationDirty();
  renderNormalization();
});
ids.btn_add_normalization_block.addEventListener("click", () => {
  if (!appState.normalization) return;
  appState.normalization.blocks.push({
    uid: (self.crypto && crypto.randomUUID) ? crypto.randomUUID() : `manual-${Date.now()}`,
    order: appState.normalization.blocks.length + 1,
    type: "unknown", original_text: "", normalized_text: "", text_for_tts: "",
    original_label: "", source: "manual", confidence: 1,
    warnings: ["Укажите тип и текст блока"],
  });
  markNormalizationDirty();
  renderNormalization();
});
ids.btn_norm_issue_prev.addEventListener("click", () => focusIssue(-1));
ids.btn_norm_issue_next.addEventListener("click", () => focusIssue(1));
ids.btn_norm_issue_filter.addEventListener("click", () => {
  issueFilterEnabled = !issueFilterEnabled;
  renderNormalization();
  if (issueFilterEnabled) focusIssue(0);
});
ids.btn_normalize.addEventListener("click", () => safely(generateNormalization));
ids.btn_reset_normalization.addEventListener("click", () => safely(resetNormalization));
ids.btn_save_normalization.addEventListener("click", () => safely(saveNormalization));
ids.btn_confirm_normalization.addEventListener("click", () => safely(confirmNormalization));
ids.btn_add_memory.addEventListener("click", () => safely(addMemoryRule));
ids.memory_search.addEventListener("input", renderMemory);
ids.memory_filter_type.addEventListener("change", renderMemory);
ids.memory_filter_status.addEventListener("change", renderMemory);
ids.memory_rules.addEventListener("click", (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const ruleId = button.closest(".memory-rule").dataset.ruleId;
  if (!ruleId) return;
  if (button.dataset.action === "delete-memory") safely(() => deleteMemoryRule(ruleId));
  if (button.dataset.action === "activate-memory") safely(() => updateMemoryRule(ruleId, { status: "active" }));
  if (button.dataset.action === "disable-memory") safely(() => updateMemoryRule(ruleId, { status: "disabled" }));
});
ids.memory_rules.addEventListener("change", (event) => {
  const control = event.target.closest("[data-memory-field]");
  if (!control) return;
  const ruleId = control.closest(".memory-rule").dataset.ruleId;
  if (!ruleId) return;
  safely(() => updateMemoryRule(ruleId, { [control.dataset.memoryField]: control.value.trim() }));
});
