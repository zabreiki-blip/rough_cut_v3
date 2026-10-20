// Stage 4: scenario revisions — version history, word-level diff and the
// revision comparison dashboard (map view).

import { ids, appState, project } from "./state.js";
import {
  toast, appendLog, escaped, blockTypeLabels, postJSON, getJSON,
  confirmDialog, openTab, safely,
} from "./core.js";
import { saveNormalization } from "./normalization.js";

const versionStatusLabels = {
  unchanged: "без изменений", moved: "переставлен", edited: "изменён",
  added: "добавлен", removed: "удалён", type_changed: "сменён тип",
  speaker_changed: "сменён спикер",
};
const changeStatusMeta = {
  unchanged: { icon: "＝", label: "Без изменений" },
  added: { icon: "＋", label: "Добавлено" },
  removed: { icon: "−", label: "Удалено" },
  edited: { icon: "✎", label: "Изменено" },
  moved: { icon: "↕", label: "Перемещено" },
  type_changed: { icon: "⇄", label: "Сменился тип" },
  speaker_changed: { icon: "⇄", label: "Сменился спикер" },
};
const dashboardStatusTextColors = {
  unchanged: "#4ade80", edited: "#facc15", moved: "#60a5fa",
  added: "#2dd4bf", removed: "#f87171", type_changed: "#fb923c", speaker_changed: "#fb923c",
};
const revisionFilterLabels = {
  all: "Все", edited: "Изменено", added: "Добавлено", removed: "Удалено", moved: "Перемещено",
};

const revisionsState = {
  revisions: [], changes: [], mode: "list", oldLabel: "", newLabel: "",
  filter: "all", search: "", activeCardIndex: -1, integrity: null,
};

// ── Word-level diff (LCS) for "Было/Стало" highlighting ──────────────────
function tokenizeWords(text) {
  return String(text || "").split(/(\s+)/).filter((tok) => tok.length > 0);
}

function diffWordsLCS(oldWords, newWords) {
  const n = oldWords.length;
  const m = newWords.length;
  const dp = new Array(n + 1);
  for (let i = 0; i <= n; i += 1) dp[i] = new Uint32Array(m + 1);
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      dp[i][j] = oldWords[i] === newWords[j]
        ? dp[i + 1][j + 1] + 1
        : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const oldOps = [];
  const newOps = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (oldWords[i] === newWords[j]) {
      oldOps.push({ text: oldWords[i], changed: false });
      newOps.push({ text: newWords[j], changed: false });
      i += 1; j += 1;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      oldOps.push({ text: oldWords[i], changed: true });
      i += 1;
    } else {
      newOps.push({ text: newWords[j], changed: true });
      j += 1;
    }
  }
  while (i < n) { oldOps.push({ text: oldWords[i], changed: true }); i += 1; }
  while (j < m) { newOps.push({ text: newWords[j], changed: true }); j += 1; }
  return { oldOps, newOps };
}

function renderWordDiffHtml(ops, tagName, className) {
  return ops.map((op) => (
    op.changed ? `<${tagName} class="${className}">${escaped(op.text)}</${tagName}>` : escaped(op.text)
  )).join("");
}

const WORD_DIFF_MAX_CHARS = 2000;

function revisionCardTextHtml(item) {
  const status = item.status;
  const oldText = item.old_text || "";
  const newText = item.new_text || "";
  const useDiff = (status === "edited" || status === "type_changed" || status === "speaker_changed")
    && oldText && newText
    && (oldText.length + newText.length) <= WORD_DIFF_MAX_CHARS;
  if (useDiff) {
    const { oldOps, newOps } = diffWordsLCS(tokenizeWords(oldText), tokenizeWords(newText));
    return {
      oldHtml: renderWordDiffHtml(oldOps, "del", "word-removed"),
      newHtml: renderWordDiffHtml(newOps, "ins", "word-added"),
    };
  }
  return {
    oldHtml: oldText ? escaped(oldText) : "",
    newHtml: newText ? escaped(newText) : "",
  };
}

function revisionNumber(revisionId) {
  const match = /(\d+)/.exec(String(revisionId || ""));
  return match ? parseInt(match[1], 10) : null;
}

function revisionTitle(revision) {
  if (!revision) return "";
  const num = revisionNumber(revision.revision_id);
  return num != null ? `Редакция ${num}` : "Редакция";
}

function revisionDate(revision) {
  if (!revision || !revision.created_at) return "";
  const date = new Date(revision.created_at);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("ru-RU", {
    day: "numeric", month: "long", hour: "2-digit", minute: "2-digit",
  });
}

function revisionDateShort(revision) {
  return revisionDate(revision).split(",")[0];
}

export async function loadProjectInbox(quiet) {
  if (!ids.inbox_scenario_select) return;
  try {
    const result = await getJSON(`/stage4/inbox?project=${encodeURIComponent(project)}`);
    const knownHashes = new Set(
      revisionsState.revisions.map((item) => String(item.source_sha256 || ""))
    );
    const scenarios = (Array.isArray(result.scenario_files) ? result.scenario_files : [])
      .filter((item) => !knownHashes.has(String(item.fingerprint || "").replace(/^sha256:/, "")));
    const previous = ids.inbox_scenario_select.value;
    ids.inbox_scenario_select.innerHTML = scenarios.length
      ? scenarios.map((item) => (
        `<option value="${escaped(item.relative_path)}">${escaped(item.name)}</option>`
      )).join("")
      : '<option value="">Положите файл в ЗАГРУЗИТЬ/Сценарии</option>';
    if (scenarios.some((item) => item.relative_path === previous)) {
      ids.inbox_scenario_select.value = previous;
    }
    const reviews = Array.isArray(result.review_files) ? result.review_files : [];
    if (ids.inbox_review_summary) {
      ids.inbox_review_summary.textContent = reviews.length
        ? `Просмотровки: найдено ${reviews.length} файл(ов).`
        : "Просмотровки: положите видео/WAV/MP3 в ЗАГРУЗИТЬ/Просмотровки.";
    }
  } catch (err) {
    if (!quiet) throw err;
  }
}

function updateActiveRevisionHeader(revisions) {
  if (!ids.revisions_active_line) return;
  if (revisionsState.integrity?.blocking) {
    ids.revisions_active_line.textContent = revisionsState.integrity.message
      || "Состояние редакций требует восстановления.";
    return;
  }
  const active = revisions.find((item) => item.active);
  if (!revisions.length) {
    ids.revisions_active_line.textContent = "Редакции ещё не созданы.";
    return;
  }
  if (!active) {
    ids.revisions_active_line.textContent = "Активная редакция не определена. Откройте нужную редакцию из истории.";
    return;
  }
  const parts = [revisionTitle(active), active.source_file || "сценарий"];
  const date = revisionDate(active);
  if (date) parts.push(date);
  ids.revisions_active_line.textContent = `Текущая: ${parts.join(" · ")}`;
}

function renderRevisionList(revisions) {
  if (!ids.revision_list) return;
  const items = Array.isArray(revisions) ? revisions : [];
  ids.versioning_status.dataset.state = items.length ? "confirmed" : "empty";
  ids.versioning_status.textContent = items.length ? `${items.length} редакц.` : "нет версий";
  ids.revision_list.innerHTML = items.length ? items.slice().reverse().map((item) => {
    // Any blocking integrity state makes normal activation unsafe.  In
    // particular, an active_revision.json that no longer points to a
    // manifest in history cannot be corrected by activate_revision(): the
    // current work projection must first be backed up and a known revision
    // restored through reconcileRevision().
    const recoveryRequired = revisionsState.integrity?.blocking === true;
    const canActivate = !item.active && !recoveryRequired;
    const date = revisionDate(item);
    const action = recoveryRequired
      ? `<button class="btn btn-sm" data-action="reconcile-revision" data-revision-id="${escaped(item.revision_id)}">Восстановить</button>`
      : (item.active
        ? '<span class="status-badge" data-state="confirmed">активна</span>'
        : (canActivate
        ? `<button class="btn btn-sm" data-action="activate-revision" data-revision-id="${escaped(item.revision_id)}">Открыть</button>`
        : '<span class="status-badge" data-state="stale">заблокировано</span>'));
    return `<article class="revision-row">
      <div><strong>${escaped(revisionTitle(item))} · ${escaped(item.source_file || "сценарий")}</strong>
      <small>${date ? `${escaped(date)} · ` : ""}${Number(item.block_count || 0)} блоков</small></div>
      ${action}
    </article>`;
  }).join("") : '<div class="empty-state compact">Версии ещё не созданы.</div>';
}

function renderSummaryChips(summary) {
  if (!ids.revisions_summary_chips) return;
  const value = summary || {};
  const edited = Number(value.edited || 0) + Number(value.type_changed || 0) + Number(value.speaker_changed || 0);
  const chips = [
    { key: "edited", label: "изменено", count: edited },
    { key: "added", label: "добавлено", count: Number(value.added || 0) },
    { key: "removed", label: "удалено", count: Number(value.removed || 0) },
    { key: "moved", label: "перемещено", count: Number(value.moved || 0) },
  ].filter((chip) => chip.count > 0);
  chips.push({ key: "unchanged", label: "без изменений", count: Number(value.unchanged || 0) });
  ids.revisions_summary_chips.innerHTML = chips.map((chip) => (
    `<span class="revision-chip" data-status="${chip.key}">${chip.count} ${chip.label}</span>`
  )).join("");
}

function changeBlockNumber(item) {
  const idx = (item.new_index !== null && item.new_index !== undefined) ? item.new_index : item.old_index;
  return Number.isFinite(idx) ? idx + 1 : "?";
}

function revisionChangeCard(item) {
  const meta = changeStatusMeta[item.status] || { icon: "•", label: versionStatusLabels[item.status] || item.status || "" };
  const speaker = item.new_speaker || item.old_speaker || "—";
  const typeValue = item.new_type || item.old_type;
  const typeLabel = blockTypeLabels[typeValue] || typeValue || "—";
  const snippet = dashboardSnippet(item.new_text || item.old_text || "", 120);
  const showOld = item.status !== "added";
  const showNew = item.status !== "removed";
  const textHtml = revisionCardTextHtml(item);
  const cols = [];
  if (showOld) {
    cols.push(`<div class="revision-card-expand-col"><span>Было — ${escaped(revisionsState.oldLabel)}</span><p>${item.old_text ? textHtml.oldHtml : "—"}</p></div>`);
  }
  if (showNew) {
    cols.push(`<div class="revision-card-expand-col"><span>Стало — ${escaped(revisionsState.newLabel)}</span><p>${item.new_text ? textHtml.newHtml : "—"}</p></div>`);
  }
  return `<article class="revision-card" data-status="${escaped(item.status || "")}" tabindex="0">
    <div class="revision-card-head">
      <span class="revision-card-status"><span class="revision-card-icon" aria-hidden="true">${meta.icon}</span>${escaped(meta.label)}</span>
      <span class="revision-card-num">#${changeBlockNumber(item)}</span>
    </div>
    <div class="revision-card-meta">${escaped(speaker)} · ${escaped(typeLabel)}</div>
    <p class="revision-card-snippet">${escaped(snippet)}</p>
    <div class="revision-card-expand">${cols.join("")}</div>
  </article>`;
}

function revisionStatusMatchesFilter(status, filter) {
  if (filter === "all") return true;
  if (filter === "edited") return status === "edited" || status === "type_changed" || status === "speaker_changed";
  return status === filter;
}

function revisionItemMatchesSearch(item, query) {
  if (!query) return true;
  const q = query.toLowerCase();
  return [item.old_text, item.new_text, item.old_speaker, item.new_speaker]
    .some((value) => String(value || "").toLowerCase().includes(q));
}

function getFilteredRevisionChanges() {
  const query = (revisionsState.search || "").trim();
  return (revisionsState.changes || []).filter((item) => (
    revisionStatusMatchesFilter(item.status, revisionsState.filter) && revisionItemMatchesSearch(item, query)
  ));
}

function updateRevisionFilterCounts() {
  if (!ids.revision_filters_bar) return;
  const source = Array.isArray(revisionsState.changes) ? revisionsState.changes : [];
  const counts = { all: source.length, edited: 0, added: 0, removed: 0, moved: 0 };
  source.forEach((item) => {
    if (item.status === "edited" || item.status === "type_changed" || item.status === "speaker_changed") counts.edited += 1;
    else if (item.status === "added") counts.added += 1;
    else if (item.status === "removed") counts.removed += 1;
    else if (item.status === "moved") counts.moved += 1;
  });
  ids.revision_filters_bar.querySelectorAll(".revision-filter").forEach((btn) => {
    const key = btn.dataset.filter;
    const label = revisionFilterLabels[key] || key;
    btn.textContent = `${label} (${counts[key] || 0})`;
  });
}

function renderChangesList(emptyMessage) {
  if (!ids.revision_changes_list) return;
  const rawItems = Array.isArray(revisionsState.changes) ? revisionsState.changes : [];
  updateRevisionFilterCounts();
  if (!rawItems.length) {
    ids.revision_changes_list.innerHTML = `<div class="empty-state compact">${escaped(emptyMessage || "Редакции совпадают — правок нет.")}</div>`;
    revisionsState.activeCardIndex = -1;
    return;
  }
  const items = getFilteredRevisionChanges();
  if (!items.length) {
    ids.revision_changes_list.innerHTML = '<div class="empty-state compact">Ничего не найдено по фильтру.</div>';
    revisionsState.activeCardIndex = -1;
    return;
  }
  const parts = [];
  let runCount = 0;
  const flushRun = () => {
    if (runCount > 0) {
      parts.push(`<div class="revision-run">${runCount} блоков без изменений</div>`);
      runCount = 0;
    }
  };
  items.forEach((item) => {
    if (item.status === "unchanged") {
      runCount += 1;
      return;
    }
    flushRun();
    parts.push(revisionChangeCard(item));
  });
  flushRun();
  ids.revision_changes_list.innerHTML = parts.length
    ? parts.join("")
    : '<div class="empty-state compact">Ничего не найдено по фильтру.</div>';
  revisionsState.activeCardIndex = -1;
}

function revisionCardEls() {
  return ids.revision_changes_list ? Array.from(ids.revision_changes_list.querySelectorAll(".revision-card")) : [];
}

function focusRevisionCard(index) {
  const cards = revisionCardEls();
  if (!cards.length) return;
  const clamped = Math.max(0, Math.min(index, cards.length - 1));
  const target = cards[clamped];
  cards.forEach((card) => card.classList.remove("is-nav-active"));
  target.classList.add("is-expanded", "is-nav-active");
  revisionsState.activeCardIndex = clamped;
  target.scrollIntoView({ block: "nearest" });
  target.focus({ preventScroll: true });
}

function navigateRevisionCards(delta) {
  const cards = revisionCardEls();
  if (!cards.length) return;
  const current = revisionsState.activeCardIndex;
  let next = (current == null || current < 0) ? (delta > 0 ? 0 : cards.length - 1) : current + delta;
  next = Math.max(0, Math.min(next, cards.length - 1));
  focusRevisionCard(next);
}

if (ids.revision_changes_list) {
  ids.revision_changes_list.addEventListener("click", (event) => {
    const card = event.target.closest(".revision-card");
    if (!card) return;
    card.classList.toggle("is-expanded");
    const cards = revisionCardEls();
    const index = cards.indexOf(card);
    if (index >= 0) revisionsState.activeCardIndex = index;
  });
  ids.revision_changes_list.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      navigateRevisionCards(1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      navigateRevisionCards(-1);
    } else if (event.key === "Enter" || event.key === " ") {
      const card = event.target.closest(".revision-card");
      if (card) {
        event.preventDefault();
        card.classList.toggle("is-expanded");
      }
    } else if (event.key === "Escape") {
      const expanded = ids.revision_changes_list.querySelector(".revision-card.is-expanded");
      if (expanded) expanded.classList.remove("is-expanded");
    }
  });
}

if (ids.revision_filters_bar) {
  ids.revision_filters_bar.querySelectorAll(".revision-filter").forEach((btn) => {
    btn.addEventListener("click", () => {
      revisionsState.filter = btn.dataset.filter;
      ids.revision_filters_bar.querySelectorAll(".revision-filter").forEach((other) => {
        const active = other === btn;
        other.classList.toggle("active", active);
        other.setAttribute("aria-pressed", active ? "true" : "false");
      });
      renderChangesList();
    });
  });
}

let revisionSearchTimer = null;
if (ids.revision_search) {
  ids.revision_search.addEventListener("input", () => {
    clearTimeout(revisionSearchTimer);
    revisionSearchTimer = setTimeout(() => {
      revisionsState.search = ids.revision_search.value || "";
      renderChangesList();
    }, 200);
  });
}
if (ids.btn_revision_prev) ids.btn_revision_prev.addEventListener("click", () => navigateRevisionCards(-1));
if (ids.btn_revision_next) ids.btn_revision_next.addEventListener("click", () => navigateRevisionCards(1));

export async function importRevision() {
  if (!appState.normalization || !Array.isArray(appState.normalization.blocks)) {
    throw new Error(
      "Сначала нормализуйте и подтвердите исходный сценарий на вкладке «Нормализация». "
      + "«Загрузить новую редакцию» — для следующих версий."
    );
  }
  if (revisionsState.integrity?.blocking) {
    throw new Error(revisionsState.integrity.message || "Сначала восстановите состояние редакций");
  }
  if (!ids.inbox_scenario_select?.value) await loadProjectInbox(false);
  const inputPath = ids.inbox_scenario_select?.value || "";
  if (!inputPath) {
    throw new Error(
      "В папке ЗАГРУЗИТЬ/Сценарии нет новых файлов сценария (уже загруженные редакции скрыты)"
    );
  }
  ids.btn_import_revision.textContent = "Сравнение…";
  try {
    const result = await postJSON("/stage4/revisions/import", {
      project,
      input_path: inputPath,
      use_llm: Boolean(ids.normalization_use_llm?.checked),
      hint: ids.normalization_hint?.value?.trim() || "",
    });
    await loadRevisionsScreen(true);
    openTab("versions");
    toast(`Создана ${revisionTitle(result.revision || {}) || "новая версия"}`, "success");
    appendLog(`Редакции: импортирована ${result.revision?.revision_id}; исходная работа сохранена.`);
  } finally {
    ids.btn_import_revision.textContent = "Загрузить новую редакцию";
  }
}

async function activateRevision(revisionId) {
  const revision = revisionsState.revisions.find((item) => item.revision_id === revisionId);
  if (appState.normalizationDirty) {
    const saveFirst = await confirmDialog({
      title: "Сохранить несохранённые правки?",
      message: "В нормализации есть несохранённые правки. Сохранить их перед переключением редакции?",
    });
    if (!saveFirst) return;
    await saveNormalization();
  }
  const accepted = await confirmDialog({
    title: "Открыть эту редакцию?",
    message: `Открыть ${revisionTitle(revision) || "эту редакцию"} как активный черновик?`,
  });
  if (!accepted) return;
  const result = await postJSON(`/stage4/revisions/${encodeURIComponent(revisionId)}/activate`, { project });
  appendLog(`Редакции: активирована ${revisionId}; сохранено sync matches: ${Number(result.reused_sync_matches || 0)}.`);
  window.location.reload();
}

async function reconcileRevision(revisionId) {
  const revision = revisionsState.revisions.find((item) => item.revision_id === revisionId);
  const accepted = await confirmDialog({
    title: `Восстановить ${revisionTitle(revision) || "эту редакцию"}?`,
    message: "Текущее рассогласованное рабочее состояние будет сохранено в versions/recovery.",
    danger: true,
  });
  if (!accepted) return;
  const result = await postJSON(
    `/stage4/revisions/${encodeURIComponent(revisionId)}/reconcile`,
    { project },
  );
  appendLog(`Редакции: проект восстановлен из ${revisionId}; резервная копия: ${result.recovery_backup || "создана"}.`);
  window.location.reload();
}

// ── Dashboard: revision change graph ────────────────────────────────────
const dashboardStatusColors = {
  unchanged: "#22c55e", edited: "#eab308", moved: "#3b82f6",
  added: "#14b8a6", removed: "#ef4444",
  type_changed: "#f97316", speaker_changed: "#f97316",
};
const DASHBOARD_NODE_W = 260;
const DASHBOARD_NODE_H = 72;
const DASHBOARD_COL_GAP = 460;
const DASHBOARD_ROW_GAP = 92;
const DASHBOARD_MARGIN = 40;
const DASHBOARD_RUN_COLLAPSE_THRESHOLD = 3;

let dashboardTransform = { x: 0, y: 0, k: 1 };
let dashboardBBox = null;
let dashboardPan = null;
let dashboardSelectedNode = null;
let dashboardActiveRunId = null;

function dashboardSnippet(text, max) {
  const value = String(text || "").replace(/\s+/g, " ").trim();
  if (value.length <= max) return value;
  return `${value.slice(0, max)}…`;
}

function dashboardStatusLabel(status) {
  return versionStatusLabels[status] || status || "";
}

function dashboardSetActivePair(pairId) {
  if (!ids.dashboard_svg) return;
  const nodesLayer = ids.dashboard_svg.querySelector(".dashboard-nodes");
  const edgesLayer = ids.dashboard_svg.querySelector(".dashboard-edges");
  if (!nodesLayer || !edgesLayer) return;
  nodesLayer.querySelectorAll(".is-active, .is-linked").forEach((node) => node.classList.remove("is-active", "is-linked"));
  edgesLayer.querySelectorAll(".is-active").forEach((edge) => edge.classList.remove("is-active"));
  if (!pairId) {
    nodesLayer.classList.remove("has-active");
    edgesLayer.classList.remove("has-active");
    return;
  }
  nodesLayer.classList.add("has-active");
  edgesLayer.classList.add("has-active");
  nodesLayer.querySelectorAll(`[data-pair="${pairId}"]`).forEach((node) => node.classList.add("is-active"));
  edgesLayer.querySelectorAll(`[data-pair="${pairId}"]`).forEach((edge) => edge.classList.add("is-active"));
}

function svgEl(tag, attrs) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  if (attrs) {
    Object.keys(attrs).forEach((key) => node.setAttribute(key, attrs[key]));
  }
  return node;
}

function dashboardApplyTransform() {
  const group = ids.dashboard_svg && ids.dashboard_svg.querySelector("#dashboard_graph_root");
  if (!group) return;
  const { x, y, k } = dashboardTransform;
  group.setAttribute("transform", `translate(${x} ${y}) scale(${k})`);
}

function dashboardFit() {
  if (!ids.dashboard_canvas || !ids.dashboard_svg || !dashboardBBox) return;
  const rect = ids.dashboard_canvas.getBoundingClientRect();
  const width = Math.max(1, rect.width);
  const height = Math.max(1, rect.height);
  const bboxW = Math.max(1, dashboardBBox.maxX - dashboardBBox.minX);
  const bboxH = Math.max(1, dashboardBBox.maxY - dashboardBBox.minY);
  const scale = Math.min(3, Math.max(0.2, Math.min(width / bboxW, height / bboxH) * 0.92));
  dashboardTransform.k = scale;
  dashboardTransform.x = (width - bboxW * scale) / 2 - dashboardBBox.minX * scale;
  dashboardTransform.y = (height - bboxH * scale) / 2 - dashboardBBox.minY * scale;
  dashboardApplyTransform();
}

function dashboardShowEmpty(message) {
  dashboardBBox = null;
  if (ids.dashboard_svg) {
    ids.dashboard_svg.innerHTML = "";
    const text = svgEl("text", {
      x: "50%", y: "50%", "text-anchor": "middle",
      class: "dashboard-empty-text",
    });
    text.textContent = message;
    ids.dashboard_svg.appendChild(text);
  }
  hideDashboardDetail();
}

function hideDashboardDetail() {
  dashboardSelectedNode = null;
  dashboardActiveRunId = null;
  if (ids.dashboard_detail) ids.dashboard_detail.hidden = true;
  if (ids.dashboard_svg) {
    ids.dashboard_svg.querySelectorAll(".dashboard-node.is-selected").forEach((node) => {
      node.classList.remove("is-selected");
    });
  }
  dashboardSetActivePair(null);
}

function showDashboardDetail(item, nodeEl) {
  if (!ids.dashboard_detail || !ids.dashboard_detail_body) return;
  if (dashboardSelectedNode === nodeEl) {
    hideDashboardDetail();
    return;
  }
  if (ids.dashboard_svg) {
    ids.dashboard_svg.querySelectorAll(".dashboard-node.is-selected").forEach((node) => {
      node.classList.remove("is-selected");
    });
  }
  nodeEl.classList.add("is-selected");
  dashboardSelectedNode = nodeEl;
  const similarity = Number.isFinite(item.text_similarity)
    ? `${Math.round(item.text_similarity * 100)}%` : "—";
  ids.dashboard_detail_body.innerHTML = `
    <div class="dashboard-detail-status" data-status="${escaped(item.status || "")}">${escaped(dashboardStatusLabel(item.status))}</div>
    <dl class="dashboard-detail-fields">
      <div><dt>Спикер</dt><dd>${escaped(item.old_speaker || "—")} → ${escaped(item.new_speaker || "—")}</dd></div>
      <div><dt>Тип</dt><dd>${escaped(blockTypeLabels[item.old_type] || item.old_type || "—")} → ${escaped(blockTypeLabels[item.new_type] || item.new_type || "—")}</dd></div>
      <div><dt>Похожесть текста</dt><dd>${escaped(similarity)}</dd></div>
    </dl>
    <div class="dashboard-detail-text">
      <span>Старый текст</span>
      <p>${item.old_text ? escaped(item.old_text) : "—"}</p>
    </div>
    <div class="dashboard-detail-text">
      <span>Новый текст</span>
      <p>${item.new_text ? escaped(item.new_text) : "—"}</p>
    </div>
  `;
  ids.dashboard_detail.hidden = false;
}

function dashboardMakeNode(item, x, y, side, pairId) {
  const status = item.status || "unchanged";
  const color = dashboardStatusColors[status] || "#8b8fa7";
  const meta = changeStatusMeta[status] || { icon: "•", label: dashboardStatusLabel(status) };
  const group = svgEl("g", { class: "dashboard-node", "data-status": status, "data-pair": pairId });
  const rect = svgEl("rect", {
    x, y, width: DASHBOARD_NODE_W, height: DASHBOARD_NODE_H, rx: 10, ry: 10,
    fill: `${color}26`, stroke: color, "stroke-width": "1.6",
  });
  group.appendChild(rect);

  const index = side === "old" ? item.old_index : item.new_index;
  const statusLabel = svgEl("text", {
    x: x + 12, y: y + 23, class: "dashboard-node-status", fill: dashboardStatusTextColors[status] || "#e8eaf0",
  });
  statusLabel.textContent = `${meta.icon} ${meta.label} · #${Number.isFinite(index) ? index + 1 : "?"}`;
  group.appendChild(statusLabel);

  const speaker = side === "old" ? item.old_speaker : item.new_speaker;
  const type = side === "old" ? item.old_type : item.new_type;
  const metaLabel = svgEl("text", { x: x + 12, y: y + 41, class: "dashboard-node-meta" });
  metaLabel.textContent = `${speaker || "—"} · ${blockTypeLabels[type] || type || "—"}`;
  group.appendChild(metaLabel);

  const text = side === "old" ? item.old_text : item.new_text;
  const snippetLabel = svgEl("text", { x: x + 12, y: y + 58, class: "dashboard-node-snippet" });
  snippetLabel.textContent = dashboardSnippet(text, 48);
  group.appendChild(snippetLabel);

  group.addEventListener("click", (event) => {
    event.stopPropagation();
    const wasSelected = dashboardSelectedNode === group;
    showDashboardDetail(item, group);
    dashboardSetActivePair(wasSelected ? null : pairId);
  });
  return group;
}

function dashboardMakeRunNode(entry, x, y) {
  const group = svgEl("g", {
    class: "dashboard-node dashboard-node-run", "data-status": "unchanged", "data-pair": entry.runId,
  });
  const rect = svgEl("rect", {
    x, y, width: DASHBOARD_NODE_W, height: DASHBOARD_NODE_H, rx: 14, ry: 14,
    fill: "rgba(139, 143, 167, 0.1)", stroke: "#8b8fa7", "stroke-width": "1.4", "stroke-dasharray": "5 4",
  });
  group.appendChild(rect);
  const label = svgEl("text", {
    x: x + DASHBOARD_NODE_W / 2, y: y + DASHBOARD_NODE_H / 2 + 5,
    "text-anchor": "middle", class: "dashboard-node-run-label",
  });
  label.textContent = `＝ ${entry.items.length} блоков без изменений`;
  group.appendChild(label);
  group.addEventListener("click", (event) => {
    event.stopPropagation();
    hideDashboardDetail();
    if (dashboardActiveRunId === entry.runId) {
      dashboardActiveRunId = null;
      dashboardSetActivePair(null);
    } else {
      dashboardActiveRunId = entry.runId;
      dashboardSetActivePair(entry.runId);
    }
  });
  return group;
}

function dashboardEdgePath(x1, y1, x2, y2) {
  const dx = Math.max(60, (x2 - x1) / 2);
  return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
}

function dashboardBuildRunGroups(items) {
  const groups = new Map();
  let i = 0;
  let seq = 0;
  while (i < items.length) {
    if (items[i].status === "unchanged") {
      let j = i;
      while (j < items.length && items[j].status === "unchanged") j += 1;
      if (j - i > DASHBOARD_RUN_COLLAPSE_THRESHOLD) {
        const runId = `run-${seq}`;
        seq += 1;
        for (let k = i; k < j; k += 1) groups.set(items[k], runId);
      }
      i = j;
    } else {
      i += 1;
    }
  }
  return groups;
}

function dashboardBuildColumnEntries(sortedItems, runGroups, pairIdByItem) {
  const entries = [];
  let i = 0;
  while (i < sortedItems.length) {
    const item = sortedItems[i];
    const runId = runGroups.get(item);
    if (runId) {
      const group = [item];
      let j = i + 1;
      while (j < sortedItems.length && runGroups.get(sortedItems[j]) === runId) {
        group.push(sortedItems[j]);
        j += 1;
      }
      entries.push({ kind: "run", runId, items: group });
      i = j;
    } else {
      entries.push({ kind: "node", item, pairId: pairIdByItem.get(item) });
      i += 1;
    }
  }
  return entries;
}

function renderDashboardGraph(changes) {
  if (!ids.dashboard_svg) return;
  hideDashboardDetail();
  ids.dashboard_svg.innerHTML = "";
  const items = Array.isArray(changes) ? changes : [];
  if (!items.length) {
    dashboardShowEmpty("Diff пуст — редакции идентичны.");
    return;
  }

  const oldItems = items
    .filter((item) => item.old_index !== null && item.old_index !== undefined)
    .slice()
    .sort((a, b) => a.old_index - b.old_index);
  const newItems = items
    .filter((item) => item.new_index !== null && item.new_index !== undefined)
    .slice()
    .sort((a, b) => a.new_index - b.new_index);

  const pairIdByItem = new Map();
  items.forEach((item, idx) => pairIdByItem.set(item, `pair-${idx}`));
  const runGroups = dashboardBuildRunGroups(items);
  const oldEntries = dashboardBuildColumnEntries(oldItems, runGroups, pairIdByItem);
  const newEntries = dashboardBuildColumnEntries(newItems, runGroups, pairIdByItem);

  const oldX = 0;
  const newX = DASHBOARD_COL_GAP;
  const oldYByIndex = new Map();
  const newYByIndex = new Map();
  const oldRunCenter = new Map();
  const newRunCenter = new Map();

  const root = svgEl("g", { id: "dashboard_graph_root" });
  const edgesLayer = svgEl("g", { class: "dashboard-edges" });
  const nodesLayer = svgEl("g", { class: "dashboard-nodes" });
  root.appendChild(edgesLayer);
  root.appendChild(nodesLayer);

  oldEntries.forEach((entry, position) => {
    const y = DASHBOARD_MARGIN + position * DASHBOARD_ROW_GAP;
    if (entry.kind === "run") {
      entry.items.forEach((item) => oldYByIndex.set(item.old_index, y));
      oldRunCenter.set(entry.runId, { x: oldX + DASHBOARD_NODE_W, y: y + DASHBOARD_NODE_H / 2 });
      nodesLayer.appendChild(dashboardMakeRunNode(entry, oldX, y));
    } else {
      oldYByIndex.set(entry.item.old_index, y);
      nodesLayer.appendChild(dashboardMakeNode(entry.item, oldX, y, "old", entry.pairId));
    }
  });
  newEntries.forEach((entry, position) => {
    const y = DASHBOARD_MARGIN + position * DASHBOARD_ROW_GAP;
    if (entry.kind === "run") {
      entry.items.forEach((item) => newYByIndex.set(item.new_index, y));
      newRunCenter.set(entry.runId, { x: newX, y: y + DASHBOARD_NODE_H / 2 });
      nodesLayer.appendChild(dashboardMakeRunNode(entry, newX, y));
    } else {
      newYByIndex.set(entry.item.new_index, y);
      nodesLayer.appendChild(dashboardMakeNode(entry.item, newX, y, "new", entry.pairId));
    }
  });

  const drawnRunEdges = new Set();
  items.forEach((item) => {
    if (item.old_index === null || item.old_index === undefined) return;
    if (item.new_index === null || item.new_index === undefined) return;
    const runId = runGroups.get(item);
    if (runId) {
      if (drawnRunEdges.has(runId)) return;
      drawnRunEdges.add(runId);
      const from = oldRunCenter.get(runId);
      const to = newRunCenter.get(runId);
      if (!from || !to) return;
      const runPath = svgEl("path", {
        d: dashboardEdgePath(from.x, from.y, to.x, to.y),
        class: "dashboard-edge",
        "data-status": "unchanged",
        "data-pair": runId,
        fill: "none",
        stroke: dashboardStatusColors.unchanged,
        "stroke-width": "1.2",
      });
      edgesLayer.appendChild(runPath);
      return;
    }
    const status = item.status || "unchanged";
    const y1 = oldYByIndex.get(item.old_index) + DASHBOARD_NODE_H / 2;
    const y2 = newYByIndex.get(item.new_index) + DASHBOARD_NODE_H / 2;
    const x1 = oldX + DASHBOARD_NODE_W;
    const x2 = newX;
    const path = svgEl("path", {
      d: dashboardEdgePath(x1, y1, x2, y2),
      class: "dashboard-edge",
      "data-status": status,
      "data-pair": pairIdByItem.get(item),
      fill: "none",
      stroke: dashboardStatusColors[status] || "#8b8fa7",
      "stroke-width": status === "unchanged" ? "1.2" : "2",
    });
    edgesLayer.appendChild(path);
  });

  ids.dashboard_svg.appendChild(root);

  const rowCount = Math.max(oldEntries.length, newEntries.length);
  dashboardBBox = {
    minX: oldX - 20,
    minY: 0,
    maxX: newX + DASHBOARD_NODE_W + 20,
    maxY: DASHBOARD_MARGIN * 2 + Math.max(1, rowCount) * DASHBOARD_ROW_GAP,
  };
  dashboardFit();
}

function updateCompareHeading(revision, parentRevision) {
  if (!ids.revisions_compare_title) return;
  if (!revision) {
    ids.revisions_compare_title.textContent = "Сравнение";
    return;
  }
  const fromLabel = parentRevision ? revisionTitle(parentRevision) : "Начало";
  ids.revisions_compare_title.textContent = `${fromLabel} → ${revisionTitle(revision)}`;
}

function updateDashboardMapLabels() {
  if (ids.dashboard_map_old_label) {
    ids.dashboard_map_old_label.textContent = `Было — ${revisionsState.oldLabel || "—"}`;
  }
  if (ids.dashboard_map_new_label) {
    ids.dashboard_map_new_label.textContent = `Стало — ${revisionsState.newLabel || "—"}`;
  }
}

function renderCompareOptions(withDiff) {
  if (!ids.dashboard_revision_select) return;
  const previous = ids.dashboard_revision_select.value;
  ids.dashboard_revision_select.innerHTML = withDiff.map((revision) => (
    `<option value="${escaped(revision.diff_id)}">${escaped(revisionTitle(revision))} · ${escaped(revisionDateShort(revision))}</option>`
  )).join("");
  const stillPresent = withDiff.some((revision) => revision.diff_id === previous);
  if (stillPresent) {
    ids.dashboard_revision_select.value = previous;
  } else if (withDiff.length) {
    ids.dashboard_revision_select.value = withDiff[withDiff.length - 1].diff_id;
  }
}

function setCompareState(state, message) {
  if (!ids.revisions_compare_state) return;
  const isOverlay = state === "loading" || state === "error";
  ids.revisions_compare_state.hidden = !isOverlay;
  if (ids.revisions_compare_state_text) {
    ids.revisions_compare_state_text.textContent = message
      || (state === "loading" ? "Загружаем сравнение…" : "Не удалось открыть сравнение.");
  }
  if (ids.btn_revisions_compare_retry) ids.btn_revisions_compare_retry.hidden = state !== "error";
  if (ids.revision_filters_bar) ids.revision_filters_bar.hidden = isOverlay;
  if (ids.revision_changes_list) ids.revision_changes_list.hidden = isOverlay ? true : revisionsState.mode !== "list";
  if (ids.revisions_map_view) ids.revisions_map_view.hidden = isOverlay ? true : revisionsState.mode !== "map";
}

let selectedDiffRequest = 0;

async function loadSelectedDiff(diffId) {
  if (!diffId) return;
  const requestId = ++selectedDiffRequest;
  const revision = revisionsState.revisions.find((item) => item.diff_id === diffId);
  const parentRevision = revision
    ? revisionsState.revisions.find((item) => item.revision_id === revision.parent_revision_id)
    : null;
  updateCompareHeading(revision, parentRevision);
  revisionsState.oldLabel = parentRevision ? revisionTitle(parentRevision) : "Редакция 1";
  revisionsState.newLabel = revision ? revisionTitle(revision) : "";
  updateDashboardMapLabels();
  setCompareState("loading");
  try {
    const diff = await getJSON(`/stage4/diffs/${encodeURIComponent(diffId)}?project=${encodeURIComponent(project)}`);
    if (requestId !== selectedDiffRequest) return;
    revisionsState.changes = diff.changes || [];
    renderSummaryChips(diff.summary || {});
    setCompareState("idle");
    renderChangesList();
    if (revisionsState.mode === "map") {
      renderDashboardGraph(revisionsState.changes);
    }
  } catch (err) {
    if (requestId !== selectedDiffRequest) return;
    setCompareState("error", "Не удалось открыть сравнение.");
    appendLog(`Редакции: ${err.message || err}`, "warn");
  }
}

export async function loadRevisionsScreen(quiet) {
  if (!ids.revision_list) return;
  try {
    const result = await getJSON(`/stage4/revisions?project=${encodeURIComponent(project)}`);
    const revisions = result.revisions || [];
    revisionsState.revisions = revisions;
    revisionsState.integrity = result.integrity || null;
    await loadProjectInbox(true);
    renderRevisionList(revisions);
    updateActiveRevisionHeader(revisions);
    const withDiff = revisions.filter((revision) => revision.diff_id);
    if (!withDiff.length) {
      if (ids.dashboard_revision_select) ids.dashboard_revision_select.innerHTML = "";
      revisionsState.changes = [];
      revisionsState.oldLabel = "";
      revisionsState.newLabel = "";
      updateCompareHeading(null, null);
      renderSummaryChips({});
      setCompareState("idle");
      renderChangesList("Загрузите следующую редакцию, чтобы увидеть изменения");
      dashboardShowEmpty("Загрузите следующую редакцию, чтобы увидеть изменения");
      return;
    }
    renderCompareOptions(withDiff);
    await loadSelectedDiff(ids.dashboard_revision_select.value);
  } catch (err) {
    setCompareState("error", "Не удалось открыть сравнение.");
    if (!quiet) throw err;
    appendLog(`Редакции: ${err.message || err}`, "warn");
  }
}

if (ids.btn_revisions_compare_retry) {
  ids.btn_revisions_compare_retry.addEventListener("click", () => {
    safely(() => loadRevisionsScreen(false));
  });
}

const dashboardMobileQuery = window.matchMedia("(max-width: 760px)");

function setRevisionsMode(mode) {
  if (mode === "map" && dashboardMobileQuery.matches) return;
  revisionsState.mode = mode;
  document.querySelectorAll(".mode-tab").forEach((btn) => {
    const active = btn.dataset.mode === mode;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
  if (ids.revision_filters_bar) ids.revision_filters_bar.hidden = mode !== "list";
  if (ids.revision_changes_list) ids.revision_changes_list.hidden = mode !== "list";
  if (ids.revisions_map_view) ids.revisions_map_view.hidden = mode !== "map";
  if (mode === "map") renderDashboardGraph(revisionsState.changes);
}

document.querySelectorAll(".mode-tab").forEach((tab) => {
  tab.addEventListener("click", () => setRevisionsMode(tab.dataset.mode));
});

function enforceMobileListMode() {
  if (dashboardMobileQuery.matches && revisionsState.mode === "map") {
    setRevisionsMode("list");
  }
}
if (typeof dashboardMobileQuery.addEventListener === "function") {
  dashboardMobileQuery.addEventListener("change", enforceMobileListMode);
}

if (ids.dashboard_revision_select) {
  ids.dashboard_revision_select.addEventListener("change", () => {
    safely(() => loadSelectedDiff(ids.dashboard_revision_select.value));
  });
}
if (ids.btn_dashboard_fit) {
  ids.btn_dashboard_fit.addEventListener("click", dashboardFit);
}
if (ids.btn_dashboard_detail_close) {
  ids.btn_dashboard_detail_close.addEventListener("click", hideDashboardDetail);
}
if (ids.dashboard_canvas) {
  ids.dashboard_canvas.addEventListener("wheel", (event) => {
    if (!dashboardBBox) return;
    event.preventDefault();
    const rect = ids.dashboard_canvas.getBoundingClientRect();
    const cursorX = event.clientX - rect.left;
    const cursorY = event.clientY - rect.top;
    const factor = event.deltaY < 0 ? 1.1 : 1 / 1.1;
    const nextK = Math.min(3, Math.max(0.2, dashboardTransform.k * factor));
    const worldX = (cursorX - dashboardTransform.x) / dashboardTransform.k;
    const worldY = (cursorY - dashboardTransform.y) / dashboardTransform.k;
    dashboardTransform.k = nextK;
    dashboardTransform.x = cursorX - worldX * nextK;
    dashboardTransform.y = cursorY - worldY * nextK;
    dashboardApplyTransform();
  }, { passive: false });

  ids.dashboard_canvas.addEventListener("mousedown", (event) => {
    if (event.button !== 0) return;
    if (event.target.closest(".dashboard-node")) return;
    dashboardPan = {
      startX: event.clientX, startY: event.clientY,
      originX: dashboardTransform.x, originY: dashboardTransform.y,
    };
    ids.dashboard_canvas.classList.add("is-panning");
  });
  window.addEventListener("mousemove", (event) => {
    if (!dashboardPan) return;
    dashboardTransform.x = dashboardPan.originX + (event.clientX - dashboardPan.startX);
    dashboardTransform.y = dashboardPan.originY + (event.clientY - dashboardPan.startY);
    dashboardApplyTransform();
  });
  window.addEventListener("mouseup", () => {
    if (!dashboardPan) return;
    dashboardPan = null;
    ids.dashboard_canvas.classList.remove("is-panning");
  });
  ids.dashboard_canvas.addEventListener("click", (event) => {
    if (!event.target.closest(".dashboard-node")) hideDashboardDetail();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !ids.dashboard_detail.hidden) {
      hideDashboardDetail();
    }
  });
}

// Re-read the inbox when the user returns to the window (e.g. after dropping a
// file into ЗАГРУЗИТЬ/Сценарии), only while the "Редакции" tab is active.
// `focus` and `visibilitychange` often fire together, hence the debounce.
const INBOX_REFRESH_DEBOUNCE_MS = 2000;
let lastInboxRefreshAt = 0;
export function refreshInboxOnReturn() {
  if (document.visibilityState === "hidden") return;
  const activeTab = document.querySelector(".workspace-tab.active");
  if (activeTab?.dataset?.tab !== "versions") return;
  const now = Date.now();
  if (now - lastInboxRefreshAt < INBOX_REFRESH_DEBOUNCE_MS) return;
  lastInboxRefreshAt = now;
  return loadProjectInbox(true);
}
window.addEventListener("focus", () => { refreshInboxOnReturn(); });
document.addEventListener("visibilitychange", () => { refreshInboxOnReturn(); });

if (ids.btn_import_revision) ids.btn_import_revision.addEventListener("click", () => safely(importRevision));
if (ids.revision_list) ids.revision_list.addEventListener("click", (event) => {
  const button = event.target.closest("[data-action='activate-revision']");
  if (button) safely(() => activateRevision(button.dataset.revisionId));
  const reconcileButton = event.target.closest("[data-action='reconcile-revision']");
  if (reconcileButton) safely(() => reconcileRevision(reconcileButton.dataset.revisionId));
});
