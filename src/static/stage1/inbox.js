// One discovery request, with separate normalization and revision views.
import { ids, project, defaults, initialStatus, appState } from "./state.js";
import { getJSON, escaped, safely } from "./core.js";

let pending = null;
let requiresSelection = false;
let selectionMissing = false;
let restored = false;
let lastRefresh = 0;
let scenarioFiles = [];
export function normalizationScenarioPath(path) {
  const prefix = `projects/${project}/`;
  return path.startsWith(prefix) || path.startsWith("/") ? path : prefix + path;
}
export function scenarioFingerprint(path) {
  const target = normalizationScenarioPath(String(path || "").trim()).normalize("NFC");
  const item = scenarioFiles.find(file => normalizationScenarioPath(file.relative_path).normalize("NFC") === target);
  return String(item?.fingerprint || "").replace(/^sha256:/, "");
}

function selectScenario() {
  if (!ids.normalization_scenario_select) return;
  const relative = ids.normalization_scenario_select.value;
  ids.normalization_input_path.value = relative ? normalizationScenarioPath(relative) : "";
  ids.normalization_scenario_summary.textContent = relative
    ? `Выбран файл: ${relative}. Разбор начнётся по кнопке «Нормализовать».`
    : "Положите сценарий в ЗАГРУЗИТЬ/Сценарии и нажмите «Обновить список».";
}

function renderScenarios(result) {
  const select = ids.normalization_scenario_select;
  if (!select) return;
  const files = Array.isArray(result.scenario_files) ? result.scenario_files : [];
  scenarioFiles = files;
  const previous = select.value;
  const manual = ids.normalization_input_path.value.trim();
  const preferred = !restored ? String(defaults.normalization_input_path ?? defaults.input_path ?? "") : "";
  const activeHash = String(appState.normalization?.source_hash || "").replace(/^sha256:/, "");
  const hashMatch = !preferred && activeHash ? files.find(item => String(item.fingerprint || "").replace(/^sha256:/, "") === activeHash)?.relative_path || "" : "";
  const effectivePreferred = preferred || (hashMatch ? normalizationScenarioPath(hashMatch) : "");
  if (!restored && defaults.has_normalization && !effectivePreferred) {
    requiresSelection = true;
    selectionMissing = true;
  }
  restored = true;
  let selected = files.find(item => item.relative_path.normalize("NFC") === previous.normalize("NFC"))?.relative_path || "";
  const disappeared = Boolean(previous && !selected);
  if (disappeared) { requiresSelection = true; selectionMissing = true; }
  if (!selected && !requiresSelection) {
    selected = files.find(item => normalizationScenarioPath(item.relative_path).normalize("NFC") === (manual || effectivePreferred).normalize("NFC"))?.relative_path || "";
    if (!selected && !manual && !appState.normalization && !["draft", "confirmed"].includes(initialStatus.normalization_status) && !defaults.has_normalization) selected = files[0]?.relative_path || "";
  }
  select.innerHTML = '<option value="">Выберите сценарий</option>' + files.map(item =>
    `<option value="${escaped(item.relative_path)}">${escaped(item.name)} — ${escaped(item.relative_path)}</option>`
  ).join("");
  select.value = selected;
  if (selected || disappeared || !manual || manual === effectivePreferred) selectScenario();
  if (!selected && manual && !selectionMissing) ids.normalization_scenario_summary.textContent = `Ручной путь: ${manual}`;
  if (selectionMissing) ids.normalization_scenario_summary.textContent = "Выбранный файл исчез или недоступен. Выберите сценарий заново; разметка и правки сохранены.";
}

export async function discoverProjectInbox() {
  if (!pending) pending = getJSON(`/stage4/inbox?project=${encodeURIComponent(project)}`).then(result => {
    renderScenarios(result);
    return result;
  }).catch(err => {
    if (ids.normalization_scenario_summary) ids.normalization_scenario_summary.textContent = `Не удалось обновить список: ${err.message || err}. Повторите обновление.`;
    throw err;
  }).finally(() => { pending = null; });
  return pending;
}
export async function refreshScenarioInbox(quiet = false) {
  try { return await discoverProjectInbox(); } catch (err) { if (!quiet) throw err; }
}
export function refreshScenarioInboxOnReturn() {
  if (document.visibilityState === "hidden") return;
  if (document.querySelector(".workspace-tab.active")?.dataset?.tab !== "normalization") return;
  const now = Date.now();
  if (now - lastRefresh < 2000) return;
  lastRefresh = now;
  return refreshScenarioInbox(true);
}
ids.normalization_scenario_select?.addEventListener("change", () => { requiresSelection = false; selectionMissing = false; selectScenario(); });
ids.normalization_input_path?.addEventListener("input", () => {
  if (ids.normalization_scenario_select) ids.normalization_scenario_select.value = "";
  requiresSelection = true;
  selectionMissing = false;
  if (ids.normalization_scenario_summary) ids.normalization_scenario_summary.textContent = `Ручной путь: ${ids.normalization_input_path.value}`;
});
ids.btn_refresh_scenarios?.addEventListener("click", () => safely(() => refreshScenarioInbox()));
window.addEventListener("focus", () => { refreshScenarioInboxOnReturn(); });
document.addEventListener("visibilitychange", () => { refreshScenarioInboxOnReturn(); });
