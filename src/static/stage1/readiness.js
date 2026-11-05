// System readiness panel (`/system/readiness`).

import { ids, appState } from "./state.js";
import { appendLog, updateActionAvailability } from "./core.js";

export function renderSelectedServiceReadiness() {
  const warning = document.getElementById("readiness_selection_warning");
  const capabilities = appState.readinessCapabilities;
  if (!warning || !capabilities) return;
  const messages = [];
  if (ids.provider?.value && ids.provider.value !== "macos" && !capabilities.cloud_tts) {
    messages.push("Выбрана облачная озвучка, но она не настроена. Выберите macOS или настройте OpenRouter.");
  }
  if (ids.sync_asr_engine?.value === "cloud" && !capabilities.cloud_asr) {
    messages.push("Выбрано облачное распознавание, но оно не настроено. Выберите локальное или настройте облачный ASR.");
  }
  if (ids.normalization_use_llm?.checked && !capabilities.cloud_llm) {
    messages.push("Облачный разбор LLM не настроен. Выключите LLM для локального разбора.");
  }
  warning.hidden = messages.length === 0;
  warning.textContent = messages.join(" ");
  if (messages.length && ids.readiness_panel) {
    ids.readiness_panel.open = true;
    if (ids.readiness_panel.dataset.state !== "error") ids.readiness_panel.dataset.state = "warn";
  }
}
for (const control of [ids.provider, ids.sync_asr_engine, ids.normalization_use_llm]) {
  control?.addEventListener("change", renderSelectedServiceReadiness);
}

export function readinessStatusLabel(status) {
  const map = { ok: "всё готово", warn: "есть предупреждения", error: "есть проблемы" };
  return map[status] || status || "—";
}

export function renderReadiness(data) {
  if (!ids.readiness_panel || !ids.readiness_list) return;
  const checks = Array.isArray(data.checks) ? data.checks : [];
  const capabilities = data.capabilities || null;
  appState.readinessCapabilities = capabilities;
  if (capabilities && !capabilities.cloud_llm) {
    if (ids.normalization_use_llm) ids.normalization_use_llm.checked = false;
    if (ids.use_llm) ids.use_llm.checked = false;
  }
  const compact = document.getElementById("readiness_capabilities");
  if (compact) compact.textContent = capabilities
    ? `Локальная озвучка: ${capabilities.local_tts ? "доступна" : "недоступна"} · Локальное распознавание: ${capabilities.local_asr ? "доступно" : "недоступно"}. Облако: ${capabilities.cloud_llm || capabilities.cloud_tts || capabilities.cloud_asr ? "настроено; связь не проверена" : "не настроено, необязательно"}.`
    : "Возможности не проверены. Откройте диагностику.";
  ids.readiness_list.innerHTML = "";
  checks.forEach((check) => {
    const item = document.createElement("li");
    item.className = "readiness-item";
    item.dataset.state = check.status || "warn";

    const head = document.createElement("div");
    head.className = "readiness-item-head";
    const title = document.createElement("span");
    title.className = "readiness-item-label";
    title.textContent = check.label || check.id || "";
    const badge = document.createElement("span");
    badge.className = "status-badge";
    badge.dataset.state = check.status || "warn";
    badge.textContent = readinessStatusLabel(check.status);
    head.appendChild(title);
    head.appendChild(badge);
    item.appendChild(head);

    const detailText = [check.detail, check.hint].filter(Boolean).join(" ");
    if (detailText) {
      const detail = document.createElement("p");
      detail.className = "readiness-item-detail";
      detail.textContent = detailText;
      item.appendChild(detail);
    }
    ids.readiness_list.appendChild(item);
  });

  const actionable = checks.filter(check => check.id !== "openrouter_key" && check.status !== "ok");
  const overall = actionable.some(check => check.status === "error") ? "error" : actionable.length ? "warn" : capabilities ? "ok" : (data.overall || "warn");
  if (ids.readiness_summary_badge) {
    ids.readiness_summary_badge.dataset.state = overall;
    ids.readiness_summary_badge.textContent = overall === "ok" && capabilities?.local_tts && capabilities?.local_asr
      ? "локальная работа доступна" : readinessStatusLabel(overall);
  }
  ids.readiness_panel.dataset.state = overall;
  if (overall !== "ok") ids.readiness_panel.open = true;
  renderSelectedServiceReadiness();
  updateActionAvailability();
}

export async function loadReadiness() {
  try {
    const response = await fetch("/system/readiness");
    const data = await response.json();
    if (!response.ok || data.ok !== true || !Array.isArray(data.checks) || !["ok", "warn", "error"].includes(data.overall) || !["local_tts", "local_asr", "cloud_llm", "cloud_tts", "cloud_asr"].every(key => typeof data.capabilities?.[key] === "boolean")) {
      throw new Error(`HTTP ${response.status}: некорректный ответ готовности`);
    }
    renderReadiness(data);
  } catch (error) {
    appState.readinessCapabilities = null;
    if (ids.readiness_panel) {
      ids.readiness_panel.dataset.state = "error";
      ids.readiness_panel.open = true;
    }
    if (ids.readiness_summary_badge) {
      ids.readiness_summary_badge.dataset.state = "error";
      ids.readiness_summary_badge.textContent = "не удалось проверить готовность";
    }
    const selectionWarning = document.getElementById("readiness_selection_warning");
    if (selectionWarning) selectionWarning.hidden = true;
    if (ids.readiness_list) ids.readiness_list.textContent = "Проверка недоступна. Обновите страницу и проверьте журнал; доступность функций пока неизвестна.";
    const compact = document.getElementById("readiness_capabilities");
    if (compact) compact.textContent = "Доступность функций пока неизвестна.";
    appendLog(`Готовность окружения: ${error.message || error}`, "warn");
    updateActionAvailability();
  }
}
