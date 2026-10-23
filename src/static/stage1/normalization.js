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
// Block opened by an explicit jump (scene opener, confirmation reason) that may not be an issue itself.
let jumpedUid = "";
// Structured reasons from the last confirmation preview / rejected confirm
// ({code, uid, order, message, severity}); blocking ones keep their block in the review queue.
let confirmationIssues = [];
let blockingIssueUids = new Set();
// Blocks explicitly ticked for a group action; nothing is selected by default.
const selectedUids = new Set();

function canonicalDocument(value) {
  if (!value) return null;
  const doc = value.normalization || value.document || value;
  if (!Array.isArray(doc.blocks)) doc.blocks = [];
  if (!doc.status) doc.status = "draft";
  if (doc.revision == null) doc.revision = 0;
  doc.warnings = Array.isArray(doc.warnings) ? doc.warnings : [];
  return doc;
}

// A response arrived for an older snapshot while the user kept typing: accept the
// server revision but keep the newer local text as an unconfirmed dirty draft.
function keepLocalDraftWithRevision(result) {
  const serverDoc = canonicalDocument(result);
  if (!appState.normalization || !serverDoc) return;
  appState.normalization.revision = serverDoc.revision;
  if (appState.normalization.status === "confirmed") appState.normalization.status = "draft";
  appState.normalizationDirty = true;
  appState.normalizationEligible = false;
  renderNormalizationStatus();
  updateActionAvailability();
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
    || ["sync", "снх", "синхрон"].includes(role)
    || (Boolean(block.uid) && blockingIssueUids.has(block.uid));
}

function issueBlocks(blocks) {
  return blocks.filter(blockNeedsReview);
}

function updateIssueNavigation(blocks) {
  const issues = issueBlocks(blocks);
  if (!issues.length) issueFilterEnabled = false;
  const jumpedHere = Boolean(jumpedUid) && jumpedUid === activeIssueUid
    && blocks.some((block) => block.uid === jumpedUid);
  if (!jumpedHere && !issues.some((block) => block.uid === activeIssueUid)) {
    activeIssueUid = issues[0]?.uid || "";
  }
  const position = issues.findIndex((block) => block.uid === activeIssueUid);
  ids.norm_issue_position.textContent = issues.length
    ? (position >= 0 ? `На проверке: блок ${position + 1} из ${issues.length}` : `На проверке блоков: ${issues.length}`)
    : "Нет блоков для проверки";
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
  jumpedUid = "";
  activeIssueUid = issues[position].uid;
  renderNormalization();
  scrollToActiveCard();
}

function scrollToActiveCard() {
  requestAnimationFrame(() => {
    const card = Array.from(ids.normalization_blocks.querySelectorAll(".normalization-block"))
      .find((item) => item.dataset.uid === activeIssueUid);
    if (!card) return;
    card.scrollIntoView({ behavior: "smooth", block: "center" });
    const control = card.querySelector("select, textarea:not([readonly]), input:not([readonly]):not([type=checkbox])");
    if (control) control.focus({ preventScroll: true });
  });
}

// Jump to a concrete block (confirmation reason, scene opener): highlight + scroll.
export function goToBlock(uid) {
  const block = (appState.normalization?.blocks || []).find((item) => item.uid === uid);
  if (!uid || !block) return false;
  if (issueFilterEnabled && !blockNeedsReview(block)) issueFilterEnabled = false;
  activeIssueUid = uid;
  jumpedUid = uid;
  renderNormalization();
  scrollToActiveCard();
  return true;
}

export function renderNormalizationStatus() {
  let state = appState.normalization ? (appState.normalization.status || "draft") : "empty";
  if (state === "confirmed" && !appState.normalizationEligible) state = "stale";
  const labels = { empty: "не создан", draft: "черновик", confirmed: "подтверждён", stale: "нужно подтвердить заново" };
  ids.normalization_status.dataset.state = state;
  ids.normalization_status.textContent = labels[state] || state;
}

function normalizationWarningMessage(raw) {
  const messages = {
    "LLM returned an unknown type": "Не удалось определить тип блока — выберите его после проверки исходника",
    "Stand-up label: speaker is not identified, check manually": "В стендапе не определён ведущий — укажите имя",
    "Lead-in label: speaker is not identified, check manually": "В подводке не определён ведущий — укажите имя",
    "all-caps section heading treated as technical (not spoken): check manually": "Строка распознана как заголовок раздела и не озвучивается — проверьте",
    "all-caps line: heading or spoken text? check manually": "Уточните, является ли строка заголовком или речью",
    "leading '+' splice has no previous LIFE/SYNC block to continue: check manually": "Не удалось связать склейку «+» с предыдущей сценой — проверьте границу блоков",
    "leading '+' splice continues previous block: check manually": "Склейка «+» связана с предыдущей сценой — проверьте переход",
    "Capitalised lead-in line is kept as description, not speech: check manually": "Подпись перед таймкодом сохранена как описание, вне речи — проверьте",
    "scene opener changed: check reply type": "Начало сцены изменилось — проверьте, к какой сцене относятся реплики ниже, и при необходимости смените тип блока",
    "Sync credit has no source media file": "У подписи синхрона нет исходного видеофайла — проверьте «Описание источника»",
    "LLM did not return a classification for this block": "LLM не классифицировал блок — выберите тип вручную",
    "Technical sync type was removed from role": "Техническая пометка синхрона убрана из роли — проверьте поле «Роль»",
    "Imported legacy data requires confirmation": "Данные импортированы из старой версии — проверьте блоки и подтвердите заново",
    "LLM voiceover suggestion contradicts the preceding LIFE scene: check manually": "LLM предложил закадр внутри лайфа — проверьте тип блока",
    "LLM sync suggestion rejected: no timecode or recognizable speaker label": "Синхрон от LLM отклонён: нет таймкода и понятной подписи героя — выберите тип вручную",
    "LLM text_for_tts differs from the source text and was ignored": "Текст для TTS от LLM отличается от исходника и не применён — проверьте «Текст для TTS»",
    "LLM classified a truncated excerpt of this oversized block (head and tail only)": "Слишком длинный блок классифицирован по началу и концу — проверьте тип и текст целиком",
  };
  if (messages[raw]) return messages[raw];
  const repeated = raw.match(/^Repeated author number (.+) \((\d+) occurrences, first in block (\d+)\)/);
  if (repeated) return `Повтор авторского номера ${repeated[1]}: ${repeated[2]} вхождения, первое в блоке ${repeated[3]} — проверьте нумерацию`;
  const timecode = raw.match(/^Ambiguous timecode '(.+)' read as (.+): check manually$/);
  if (timecode) return `Неоднозначный таймкод «${timecode[1]}» прочитан как ${timecode[2]} — уточните время`;
  const label = raw.match(/^Ambiguous label combination '(.+)':/);
  if (label) return `Противоречивые метки «${label[1]}» — выберите тип блока`;
  const scene = (name) => blockTypeLabels[String(name).toLowerCase()] || name;
  const replyNoSpeaker = raw.match(/^reply joined to the previous (\w+) scene; speaker is not stated/);
  if (replyNoSpeaker) return `Реплика присоединена к предыдущей сцене («${scene(replyNoSpeaker[1])}»), говорящий не указан — укажите спикера или смените тип блока`;
  const longReply = raw.match(/^long dash paragraph joined to the (\w+) scene as a reply/);
  if (longReply) return `Длинный абзац с тире присоединён к сцене «${scene(longReply[1])}» как реплика — если это закадровый текст, смените тип блока`;
  const spliceNoText = raw.match(/^Timecode (.+) in a '\+' splice has no text/);
  if (spliceNoText) return `У таймкода ${spliceNoText[1]} в склейке «+» нет текста — заполните фрагмент или удалите его`;
  const alias = raw.match(/^Possible learned alias '(.+)' \((.+)\)/);
  if (alias) return `Обозначение похоже на запомненное «${alias[1]}» (${alias[2]}) — проверьте тип блока`;
  const conflicting = raw.match(/^Conflicting learned rules for '(.+)'/);
  if (conflicting) return `Для обозначения «${conflicting[1]}» в памяти есть противоречивые правила — выберите тип блока вручную`;
  const lowIssue = raw.match(/^Block \d+ LLM confidence (\S+) is low/);
  if (lowIssue) return `Низкая уверенность LLM (${lowIssue[1]}) — проверьте тип и текст блока`;
  const confidence = raw.match(/^LLM confidence\s+([^\s]+)\s+is below/i);
  if (confidence) return `Низкая уверенность LLM (${confidence[1]}) — требуется проверка`;
  if (/^LLM classification failed:\s*JSONDecodeError/i.test(raw)) {
    return "LLM вернул некорректный JSON — автоклассификация не применена";
  }
  return raw;
}

const ISSUE_FIELD_LABELS = {
  speaker: "Спикер",
  role: "Роль / должность",
  source_description: "Описание источника",
};

// Русские причины и подсказки, как исправить, по кодам проверки подтверждения.
const issueMessages = {
  unknown_type: () => "Тип блока не определён — выберите тип в заголовке блока",
  invalid_structure: (issue) => `Нарушена структура блока (${issue.message}) — проверьте порядок, источник и тип`,
  invalid_absorbed_link: () => "Некорректная связь «Включён в озвучку блока» — выберите закадровый блок или «Нет»",
  absorbed_block_has_tts: () => "Блок включён в озвучку другого блока, но у него остался свой текст для TTS — очистите поле «Текст для TTS»",
  empty_tts: () => "У закадрового блока пустой текст для TTS — заполните поле или включите блок в озвучку другого блока",
  speech_direction_in_tts: () => "В тексте для TTS осталась ремарка (например «голос за кадром») — удалите её, она не должна озвучиваться",
  editorial_cue_in_voiceover: () => "Закадровый блок содержит монтажную ремарку, а не текст диктора — смените тип на «Технический» или оставьте только текст диктора",
  sync_no_speaker: () => "У синхрона не указан спикер — заполните поле «Спикер»",
  sync_direction_as_speaker: () => "В поле «Спикер» записана ремарка («читает», «поёт»), а не имя — укажите имя героя, ремарку перенесите в «Заметку»",
  sync_only_credit: () => "Блок содержит только подпись героя без речи — добавьте текст синхрона или удалите блок",
  sync_speaker_conflict: () => "Спикер не совпадает с подписью в исходнике — сверьте поля «Спикер» и «Исходное обозначение»",
  sync_empty_fragment: () => "Во фрагментах синхрона есть пустой — заполните его текст или удалите фрагмент",
  invalid_timecode: (issue) => {
    const value = String(issue.message || "").match(/invalid timecode (.+)$/);
    return `Некорректный таймкод${value ? ` «${value[1]}»` : ""} — введите его в формате ЧЧ:ММ:СС`;
  },
  service_label_in_tts: () => "В тексте для TTS осталась служебная метка («ЗК № 3», «ПОДВОДКА» и т. п.) — удалите её из поля «Текст для TTS»",
  timecode_in_role: (issue) => {
    const field = String(issue.message || "").match(/timecode fragment in (\w+)/);
    const label = field ? ISSUE_FIELD_LABELS[field[1]] || field[1] : "метаданных";
    return `В поле «${label}» остался фрагмент таймкода — очистите поле и оставьте только имя или описание`;
  },
  timecode_remnant_in_text: () => "Текст начинается с остатка таймкода (например «)» или «00:12)») — удалите его в начале текста",
  false_hero_from_cue: () => "Спикером записана техническая подпись (титры, хронометраж и т. п.), а не человек — укажите имя героя или смените тип блока",
  lost_splice: (issue) => {
    const missing = String(issue.message || "").match(/no fragment starts at (.+)$/);
    return `Потерян фрагмент склейки «+»${missing ? ` с таймкодом ${missing[1]}` : ""} — добавьте фрагмент с этим таймкодом в «Фрагменты спикера»`;
  },
  unmatched_speech: (issue) => {
    const lost = String(issue.message || "").match(/lost (\d+) of (\d+) spoken words(?: \(e\.g\. '(.+)'\))?/);
    const detail = lost ? ` (${lost[1]} из ${lost[2]} слов${lost[3] ? `, например «${lost[3]}»` : ""})` : "";
    const advice = issue.severity === "warning" ? "убедитесь, что сокращение сделано намеренно" : "восстановите текст из «Исходного текста»";
    return `Часть речи из исходника потеряна${detail} — ${advice}`;
  },
};

export function describeIssue(issue) {
  const builder = issueMessages[issue?.code];
  return builder ? builder(issue) : normalizationWarningMessage(String(issue?.message || ""));
}

function issueBlockLabel(issue) {
  const blocks = appState.normalization?.blocks || [];
  const index = issue.uid ? blocks.findIndex((block) => block.uid === issue.uid) : -1;
  const position = index >= 0 ? index + 1 : issue.order;
  return position ? `Блок #${position}` : "Сценарий";
}

function renderIssueItem(issue) {
  const label = issueBlockLabel(issue);
  const head = issue.uid
    ? `<button class="btn btn-sm issue-goto" type="button" data-goto-uid="${escaped(issue.uid)}" title="Перейти к блоку">${escaped(label)}</button>`
    : `<strong>${escaped(label)}</strong>`;
  return `<li>${head} <span>${escaped(describeIssue(issue))}</span></li>`;
}

function setConfirmationFeedback(issues) {
  confirmationIssues = Array.isArray(issues) ? issues.filter((issue) => issue && typeof issue === "object") : [];
  blockingIssueUids = new Set(confirmationIssues
    .filter((issue) => issue.severity === "error" && issue.uid).map((issue) => issue.uid));
}

function clearConfirmationFeedback() {
  setConfirmationFeedback([]);
  ids.normalization_confirm_summary.hidden = true;
  ids.normalization_confirm_summary.innerHTML = "";
}

// Summary shown before the confirm request; a rejected (422) confirm reuses it.
function renderConfirmationSummary({ ttsCount = null, rejected = false } = {}) {
  const errors = confirmationIssues.filter((issue) => issue.severity === "error");
  const warnings = confirmationIssues.filter((issue) => issue.severity !== "error");
  const panel = ids.normalization_confirm_summary;
  const status = errors.length
    ? `Блокирующих ошибок: <b>${errors.length}</b> — ${rejected ? "сервер отклонил подтверждение" : "подтверждение не отправлено"}.`
    : "Блокирующих ошибок нет.";
  panel.dataset.state = errors.length ? "blocked" : "ok";
  panel.hidden = false;
  panel.innerHTML = `<strong>Проверка перед подтверждением</strong>
    <div>${ttsCount != null ? `В озвучку пойдёт блоков: <b>${ttsCount}</b>. ` : ""}${status}${warnings.length ? ` Предупреждений: <b>${warnings.length}</b>.` : ""}</div>
    ${errors.length ? `<ul class="confirm-issue-list">${errors.map(renderIssueItem).join("")}</ul>` : ""}
    ${warnings.length ? `<details><summary>Предупреждения (${warnings.length})</summary><ul class="confirm-issue-list">${warnings.map(renderIssueItem).join("")}</ul></details>` : ""}
    <small>Список обновляется при каждой попытке подтверждения.</small>`;
}

function syncBlockFromFragments(block) {
  const fragments = Array.isArray(block.sync_fragments) ? block.sync_fragments : [];
  block.normalized_text = fragments.map((item) => String(item.text || "").trim())
    .filter(Boolean).join(" ");
  const firstTimed = fragments.find((item) => String(item.timecode_start || "").trim()) || fragments[0];
  block.timecode_start = String(firstTimed?.timecode_start || "");
  block.timecode_end = String(firstTimed?.timecode_end || "");
}

export function addSyncFragment(block) {
  if (!Array.isArray(block.sync_fragments)) block.sync_fragments = [];
  if (block.sync_fragments.length === 0) {
    block.sync_fragments.push({
      text: block.normalized_text || "",
      timecode_start: block.timecode_start || "",
      timecode_end: block.timecode_end || "",
    });
  }
  block.sync_fragments.push({ text: "", timecode_start: "", timecode_end: "" });
  syncBlockFromFragments(block);
  markNormalizationDirty();
  return true;
}

export function removeSyncFragment(block, fragmentIndex) {
  const fragments = Array.isArray(block.sync_fragments) ? block.sync_fragments : [];
  if (fragments.length < 2 || !fragments[fragmentIndex]) return false;
  fragments.splice(fragmentIndex, 1);
  syncBlockFromFragments(block);
  markNormalizationDirty();
  return true;
}

// Split one fragment at the text cursor; the tail gets empty timecodes to be filled in.
export function splitSyncFragment(block, fragmentIndex, position) {
  const fragments = Array.isArray(block.sync_fragments) ? block.sync_fragments : [];
  const fragment = fragments[fragmentIndex];
  if (!fragment) return false;
  const text = String(fragment.text || "");
  const cursor = Number.isInteger(position) ? position : -1;
  const head = text.slice(0, Math.max(cursor, 0)).trim();
  const tail = text.slice(Math.max(cursor, 0)).trim();
  if (cursor < 0 || !head || !tail) return false;
  fragments.splice(fragmentIndex, 1,
    { ...fragment, text: head },
    { text: tail, timecode_start: "", timecode_end: "" });
  syncBlockFromFragments(block);
  markNormalizationDirty();
  return true;
}

function sceneLabelHtml(block, blocks) {
  const openerIndex = block.scene_uid ? blocks.findIndex((item) => item.uid === block.scene_uid) : -1;
  if (openerIndex < 0 || blocks[openerIndex] === block) return "";
  const opener = blocks[openerIndex];
  const openerType = opener.type || opener.block_type || "unknown";
  const description = String(opener.source_description || opener.speaker
    || opener.normalized_text || opener.original_text || "").trim().slice(0, 40);
  return `<div class="block-scene">Сцена: блок #${openerIndex + 1} (${escaped(blockTypeLabels[openerType] || openerType)}${description ? `, ${escaped(description)}` : ""})
    <button class="btn btn-sm" type="button" data-action="goto-scene" data-goto-uid="${escaped(opener.uid)}" title="Перейти к блоку, открывающему сцену">К сцене</button></div>`;
}

function updateGroupBar(blocks) {
  const present = new Set(blocks.map((block) => block.uid).filter(Boolean));
  Array.from(selectedUids).forEach((uid) => { if (!present.has(uid)) selectedUids.delete(uid); });
  ids.norm_selected_count.textContent = String(selectedUids.size);
  ids.btn_norm_group_review.disabled = selectedUids.size === 0;
  ids.btn_norm_group_clear.disabled = selectedUids.size === 0;
}

// Group action only for explicitly selected blocks; unknown-type blocks still need a decision.
export function reviewSelectedBlocks() {
  const blocks = appState.normalization?.blocks || [];
  let reviewed = 0;
  let skipped = 0;
  blocks.forEach((block) => {
    if (!block.uid || !selectedUids.has(block.uid)) return;
    if ((block.type || block.block_type || "unknown") === "unknown") {
      skipped += 1;
      return;
    }
    if (Array.isArray(block.warnings) && block.warnings.length) {
      block.warnings = [];
      reviewed += 1;
    }
  });
  selectedUids.clear();
  if (reviewed) markNormalizationDirty();
  renderNormalization();
  toast(
    reviewed || skipped
      ? `Предупреждения проверены в блоках: ${reviewed}${skipped ? `; пропущено без типа: ${skipped}` : ""}`
      : "В выбранных блоках нет предупреждений",
    reviewed ? "success" : "warn",
  );
  return reviewed;
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
  updateGroupBar(blocks);

  const warningGroups = new Map();
  function addWarning(warning, order) {
    const raw = String(warning || "").trim();
    if (!raw) return;
    const message = normalizationWarningMessage(raw);
    if (!warningGroups.has(message)) warningGroups.set(message, new Set());
    if (order != null) warningGroups.get(message).add(order);
  }
  (appState.normalization?.warnings || []).forEach((warning) => addWarning(warning));
  blocks.forEach((block, index) => {
    (block.warnings || []).forEach((warning) => addWarning(warning, block.order ?? index + 1));
  });
  const groupedWarnings = Array.from(warningGroups.entries());
  const renderWarning = ([message, orders]) => `${escaped(message)}${orders.size
    ? ` <span class="warning-count">· ${orders.size === 1 ? "блок" : "блоки"}: ${escaped(Array.from(orders).join(", "))}</span>` : ""}`;
  ids.normalization_warnings.hidden = groupedWarnings.length === 0;
  ids.normalization_warnings.innerHTML = groupedWarnings.length
    ? `<strong>Требует внимания:</strong><br>${groupedWarnings.slice(0, 8).map(renderWarning).join("<br>")}${groupedWarnings.length > 8
      ? `<details><summary>Остальные предупреждения (${groupedWarnings.length - 8})</summary>${groupedWarnings.slice(8).map(renderWarning).join("<br>")}</details>` : ""}`
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
        <input type="checkbox" class="block-select" data-select-block aria-label="Выбрать блок для групповых действий"${block.uid ? "" : " disabled"}${selectedUids.has(block.uid) ? " checked" : ""}>
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
      ${sceneLabelHtml(block, blocks)}
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
        <label class="field"><span>Включён в озвучку блока</span><select data-field="absorbed_by_uid">
          <option value="">Нет</option>
          ${blocks.filter((candidate) => candidate.uid !== block.uid && candidate.type === "voiceover" && !candidate.absorbed_by_uid)
            .map((candidate) => `<option value="${escaped(candidate.uid)}"${candidate.uid === block.absorbed_by_uid ? " selected" : ""}>#${candidate.order} · ${escaped(String(candidate.normalized_text || "").slice(0, 50))}</option>`).join("")}
        </select></label>
      </div>
      ${hasMultipleFragments ? `<div class="sync-fragment-editor">
        <div class="sync-fragment-heading">Фрагменты спикера · ${syncFragments.length}
          <button class="btn btn-sm" type="button" data-action="add-fragment" title="Добавить фрагмент">+ Фрагмент</button></div>
        ${syncFragments.map((fragment, fragmentIndex) => `
          <div class="sync-fragment-row" data-fragment-index="${fragmentIndex}">
            <span class="sync-fragment-number">${fragmentIndex + 1}</span>
            <label class="field"><span>Начало</span><input data-fragment-field="timecode_start" value="${escaped(fragment.timecode_start || "")}"></label>
            <label class="field"><span>Конец</span><input data-fragment-field="timecode_end" value="${escaped(fragment.timecode_end || "")}"></label>
            <label class="field sync-fragment-text"><span>Текст фрагмента</span><textarea data-fragment-field="text">${escaped(fragment.text || "")}</textarea></label>
            <div class="sync-fragment-actions">
              <button class="btn btn-sm" type="button" data-action="split-fragment" title="Разделить текст фрагмента по позиции курсора">Разделить</button>
              <button class="btn btn-sm" type="button" data-action="delete-fragment" title="Удалить фрагмент">Удалить</button>
            </div>
          </div>`).join("")}
      </div>` : (type === "sync" ? `<div class="sync-fragment-editor">
        <div class="sync-fragment-heading">Фрагменты спикера · ${syncFragments.length}
          <button class="btn btn-sm" type="button" data-action="add-fragment" title="Разбить синхрон на несколько фрагментов">+ Фрагмент</button></div>
      </div>` : "")}
      ${blockWarnings.length ? `<div class="block-warning">${blockWarnings.map((warning, warningIndex) => `
        <div class="block-warning-row"><span>${escaped(normalizationWarningMessage(String(warning)))}</span>
          ${type !== "unknown" ? `<button class="btn btn-sm" type="button" data-action="review-warning" data-warning-index="${warningIndex}" title="Подтвердить, что это предупреждение проверено">Проверено</button>` : ""}
        </div>`).join("")}</div>` : ""}`;
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
    syncBlockFromFragments(block);
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
  if (control.dataset.field === "absorbed_by_uid") {
    block.text_for_tts = control.value ? "" : (block.type === "voiceover" ? block.normalized_text || "" : "");
    const ttsControl = card.querySelector('[data-field="text_for_tts"]');
    if (ttsControl) ttsControl.value = block.text_for_tts;
  }
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
    // Choosing a type resolves classification uncertainty, not timecodes or
    // scene transitions. Those require their own explicit review.
    if (control.value !== "unknown") {
      block.warnings = (block.warnings || []).filter((warning) => (
        warning !== "LLM returned an unknown type"
        && warning !== "LLM did not return a classification for this block"
        && !String(warning).startsWith("Ambiguous label combination '")
      ));
    }
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
  const startedGeneration = normalizationEditGeneration;
  try {
    const result = await requestJSON(`/stage1/normalization?project=${encodeURIComponent(project)}`, "GET");
    if (normalizationEditGeneration !== startedGeneration && appState.normalization) {
      keepLocalDraftWithRevision(result);
      return;
    }
    appState.normalization = canonicalDocument(result);
    appState.normalizationDirty = false;
    clearConfirmationFeedback();
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
  if (hasUnsavedChanges()) {
    await saveNormalization();
    if (hasUnsavedChanges()) throw new Error("Во время сохранения появились новые правки. Сохраните их перед повторным разбором.");
  }
  const startedGeneration = normalizationEditGeneration;
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
      `/stage1/normalization-jobs/${encodeURIComponent(jobId)}?project=${encodeURIComponent(project)}`,
      "GET",
    );
  }
  if (state.status !== "completed") {
    throw new Error(state.error || state.message || "Нормализация завершилась с ошибкой");
  }
  if (normalizationEditGeneration !== startedGeneration) {
    keepLocalDraftWithRevision(state.result);
    toast("Разбор завершён; новые правки сохранены в редакторе и ещё не отправлены на сервер", "success");
    return;
  }
  appState.normalization = canonicalDocument(state.result);
  appState.normalizationEligible = false;
  appState.normalizationDirty = false;
  clearConfirmationFeedback();
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
    keepLocalDraftWithRevision(result);
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
  clearConfirmationFeedback();
  renderNormalization();
  toast("Нормализация очищена. Можно запустить её заново.", "success");
  appendLog("Нормализация очищена: блоки удалены");
  await refreshStatus();
}

export async function confirmNormalization() {
  const startGeneration = normalizationEditGeneration;
  const unknown = appState.normalization.blocks.filter((block) => (block.type || block.block_type) === "unknown").length;
  if (unknown) throw new Error(`Исправьте неопределённые блоки: ${unknown}`);
  if (appState.normalizationDirty) await saveNormalization();
  const preview = await requestJSON("/stage1/normalization/confirm/preview", "POST", {
    project, revision: appState.normalization.revision,
  });
  setConfirmationFeedback(preview.validation);
  const ttsCount = Array.isArray(preview.tts_preview) ? preview.tts_preview.length : null;
  renderConfirmationSummary({ ttsCount });
  const blockingCount = confirmationIssues.filter((issue) => issue.severity === "error").length;
  if (blockingCount) {
    renderNormalization();
    throw new Error(`Подтверждение не отправлено: блокирующих ошибок ${blockingCount}. Причины показаны над списком блоков`);
  }
  const candidates = preview.candidates || [];
  const learningDecisions = candidates.length ? await chooseLearningDecisions(candidates) : [];
  if (learningDecisions === null) return;
  let result;
  try {
    result = await requestJSON("/stage1/normalization/confirm", "POST", {
      project, revision: appState.normalization.revision, learning_decisions: learningDecisions,
    });
  } catch (err) {
    const issues = err?.payload?.issues;
    if (err?.status === 422 && Array.isArray(issues) && issues.length) {
      setConfirmationFeedback(issues);
      renderConfirmationSummary({ ttsCount, rejected: true });
      renderNormalization();
      const count = confirmationIssues.filter((issue) => issue.severity === "error").length;
      throw new Error(`Сервер отклонил подтверждение: ошибок ${count}. Причины показаны над списком блоков`);
    }
    throw err;
  }
  if (normalizationEditGeneration !== startGeneration) {
    keepLocalDraftWithRevision(result);
    toast("Подтверждена сохранённая версия; новые правки не подтверждены и не сохранены", "warn");
    await loadMemory(true);
    await refreshStatus();
    return;
  }
  appState.normalization = canonicalDocument(result);
  appState.normalization.status = "confirmed";
  appState.normalizationEligible = true;
  appState.normalizationDirty = false;
  renderNormalization();
  toast("Сценарий подтверждён. Озвучка и сборка доступны.", "success");
  await loadMemory(true);
  await refreshStatus();
}

const historyStatusLabels = { draft: "черновик", confirmed: "подтверждён" };
const historyChangeLabels = {
  edited: "изменён текст", type_changed: "изменён тип", speaker_changed: "изменён говорящий",
  moved: "перемещён", added: "добавлен", removed: "удалён",
};
let historyRequestToken = 0;

function shortText(value, limit = 70) {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

// Snapshot file names carry the moment the revision was replaced: YYYYMMDDTHHMMSS...Z_revN.json
function historyItemDate(item) {
  const match = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})/.exec(String(item.name || ""));
  if (match) return `${match[3]}.${match[2]}.${match[1]} ${match[4]}:${match[5]}`;
  return String(item.created_at || "").slice(0, 16).replace("T", " ");
}

function renderNormalizationHistory(items) {
  if (!items.length) {
    ids.normalization_history_list.innerHTML =
      '<div class="empty-state compact">Сохранённых версий пока нет. Версия появляется при повторном разборе или откате.</div>';
    return;
  }
  ids.normalization_history_list.innerHTML = items.map((item) => `
    <div class="history-item" data-name="${escaped(item.name)}">
      <div class="history-item-meta"><strong>Ревизия ${escaped(item.revision ?? "?")}</strong>
        · ${escaped(historyStatusLabels[item.status] || item.status || "—")}
        · ${escaped(historyItemDate(item))} · блоков: ${escaped(item.blocks ?? 0)}</div>
      <div class="history-item-actions">
        <button class="btn btn-sm" data-history-action="diff" type="button">Сравнить</button>
        <button class="btn btn-sm" data-history-action="restore" type="button">Вернуть эту версию</button>
      </div>
    </div>`).join("");
}

function renderNormalizationHistoryDiff(diff) {
  const summary = diff.summary || {};
  const changed = (diff.changes || []).filter((change) => change.status !== "unchanged");
  const counts = Object.entries(summary)
    .filter(([status, count]) => status !== "unchanged" && count)
    .map(([status, count]) => `${historyChangeLabels[status] || status}: ${count}`);
  const header = `Версия (ревизия ${escaped(diff.snapshot?.revision ?? "?")}) и текущий черновик (ревизия ${escaped(diff.current?.revision ?? "?")})`;
  const rows = changed.map((change) => {
    const order = (change.new_index ?? change.old_index ?? 0) + 1;
    const lines = [];
    if (change.old_type !== change.new_type) {
      lines.push(`тип: ${escaped(blockTypeLabels[change.old_type] || change.old_type || "—")} → ${escaped(blockTypeLabels[change.new_type] || change.new_type || "—")}`);
    }
    if (change.old_speaker !== change.new_speaker) {
      lines.push(`говорящий: ${escaped(change.old_speaker || "—")} → ${escaped(change.new_speaker || "—")}`);
    }
    if (change.old_text !== change.new_text) {
      lines.push(`текст: «${escaped(shortText(change.old_text))}» → «${escaped(shortText(change.new_text))}»`);
    }
    (change.downstream_changed_fields || [])
      .filter((field) => !["type", "speaker", "normalized_text"].includes(field))
      .forEach((field) => lines.push(`поле ${escaped(field)} изменено`));
    return `<li><strong>Блок ${order}</strong> · ${escaped(historyChangeLabels[change.status] || change.status)}
      ${lines.map((line) => `<div class="history-diff-line">${line}</div>`).join("")}</li>`;
  });
  ids.normalization_history_diff.innerHTML = `<p class="section-copy">${header}. ${
    counts.length ? escaped(`Различия — ${counts.join("; ")}.`) : "Различий нет."
  }</p>${rows.length ? `<ul class="history-diff-list">${rows.join("")}</ul>` : ""}`;
  ids.normalization_history_diff.hidden = false;
}

function clearNormalizationHistoryDiff() {
  historyRequestToken += 1;
  ids.normalization_history_diff.innerHTML = "";
  ids.normalization_history_diff.hidden = true;
}

export async function loadNormalizationHistory() {
  const token = ++historyRequestToken;
  const result = await requestJSON(`/stage1/normalization/history?project=${encodeURIComponent(project)}`, "GET");
  if (token !== historyRequestToken) return;
  renderNormalizationHistory(Array.isArray(result.history) ? result.history : []);
}

export async function toggleNormalizationHistory() {
  const opening = ids.normalization_history_panel.hidden;
  ids.normalization_history_panel.hidden = !opening;
  if (!opening) return;
  clearNormalizationHistoryDiff();
  await loadNormalizationHistory();
}

export async function showNormalizationHistoryDiff(name) {
  const token = ++historyRequestToken;
  const result = await requestJSON(
    `/stage1/normalization/history/${encodeURIComponent(name)}/diff?project=${encodeURIComponent(project)}`, "GET",
  );
  if (token !== historyRequestToken) return;
  renderNormalizationHistoryDiff(result);
}

export async function restoreNormalizationSnapshot(name) {
  if (!appState.normalization) return;
  const unsaved = hasUnsavedChanges();
  const accepted = await confirmDialog({
    title: "Вернуть эту версию разбора?",
    message: "Текущий черновик будет сохранён в истории, а выбранная версия станет новым черновиком "
      + "и потребует повторного подтверждения. Подтверждённый сценарий для озвучки не изменится, пока вы не подтвердите новый."
      + (unsaved ? " Несохранённые правки будут сохранены перед откатом." : ""),
    confirmLabel: "Вернуть версию",
  });
  if (!accepted) return;
  if (unsaved) {
    await saveNormalization();
    if (hasUnsavedChanges()) throw new Error("Во время сохранения появились новые правки. Сохраните их перед откатом версии.");
  }
  const startedGeneration = normalizationEditGeneration;
  const result = await requestJSON(
    `/stage1/normalization/history/${encodeURIComponent(name)}/restore`, "POST",
    { project, expected_revision: appState.normalization.revision },
  );
  clearNormalizationHistoryDiff();
  if (normalizationEditGeneration !== startedGeneration) {
    keepLocalDraftWithRevision(result);
    toast("Версия возвращена на сервере; новые правки сохранены в редакторе и ещё не отправлены", "warn");
  } else {
    await loadNormalization();
    appState.normalizationEligible = false;
    toast(`Версия возвращена как черновик (ревизия ${appState.normalization?.revision ?? "?"}). Подтвердите сценарий заново.`, "success");
  }
  appendLog("Нормализация: возвращена версия из истории разбора");
  await loadNormalizationHistory();
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
  if (typeof status.normalization_confirmed === "boolean") {
    appState.normalizationEligible = status.normalization_confirmed && !appState.normalizationDirty;
  }
  if (typeof status.timeline_ready === "boolean") appState.timelineReady = status.timeline_ready;
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
  if (event.target.dataset.selectBlock !== undefined) {
    const uid = event.target.closest(".normalization-block")?.dataset.uid;
    if (!uid) return;
    if (event.target.checked) selectedUids.add(uid);
    else selectedUids.delete(uid);
    updateGroupBar(appState.normalization?.blocks || []);
    return;
  }
  if (event.target.dataset.field || event.target.dataset.fragmentField) updateBlockFromControl(event.target);
});
ids.normalization_blocks.addEventListener("click", (event) => {
  const button = event.target.closest("[data-action]");
  if (!button || !appState.normalization) return;
  const card = button.closest(".normalization-block");
  if (!card) return;
  const block = appState.normalization.blocks[Number(card.dataset.index)];
  if (!block) return;
  if (button.dataset.action === "review-warning") {
    if ((block.type || block.block_type || "unknown") === "unknown") return;
    const index = Number(button.dataset.warningIndex);
    if (!Number.isInteger(index) || index < 0 || index >= (block.warnings || []).length) return;
    block.warnings.splice(index, 1);
    markNormalizationDirty();
    renderNormalization();
    return;
  }
  if (button.dataset.action === "goto-scene") {
    goToBlock(button.dataset.gotoUid);
    return;
  }
  if (["add-fragment", "delete-fragment", "split-fragment"].includes(button.dataset.action)) {
    if ((block.type || block.block_type) !== "sync") return;
    const row = button.closest("[data-fragment-index]");
    const fragmentIndex = Number(row?.dataset.fragmentIndex);
    let changed = false;
    if (button.dataset.action === "add-fragment") {
      changed = addSyncFragment(block);
    } else if (button.dataset.action === "delete-fragment") {
      changed = removeSyncFragment(block, fragmentIndex);
    } else {
      const textControl = row?.querySelector('[data-fragment-field="text"]');
      changed = splitSyncFragment(block, fragmentIndex, textControl?.selectionStart);
      if (!changed) toast("Поставьте курсор внутри текста фрагмента, чтобы разделить его на две части", "warn");
    }
    if (changed) renderNormalization();
    return;
  }
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
ids.normalization_confirm_summary.addEventListener("click", (event) => {
  const target = event.target.closest("[data-goto-uid]");
  if (target) goToBlock(target.dataset.gotoUid);
});
ids.btn_norm_group_review.addEventListener("click", () => reviewSelectedBlocks());
ids.btn_norm_group_clear.addEventListener("click", () => {
  selectedUids.clear();
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
ids.btn_normalization_history.addEventListener("click", () => safely(toggleNormalizationHistory));
ids.btn_normalization_history_close.addEventListener("click", () => {
  clearNormalizationHistoryDiff();
  ids.normalization_history_panel.hidden = true;
});
ids.normalization_history_list.addEventListener("click", (event) => {
  const button = event.target.closest("[data-history-action]");
  const item = button && button.closest(".history-item");
  const name = item && item.dataset.name;
  if (!name) return;
  if (button.dataset.historyAction === "diff") safely(() => showNormalizationHistoryDiff(name));
  if (button.dataset.historyAction === "restore") safely(() => restoreNormalizationSnapshot(name));
});
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
