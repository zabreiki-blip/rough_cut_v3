// System readiness panel (`/system/readiness`).

import { ids } from "./state.js";

export function readinessStatusLabel(status) {
  const map = { ok: "всё готово", warn: "есть предупреждения", error: "есть проблемы" };
  return map[status] || status || "—";
}

export function renderReadiness(data) {
  if (!ids.readiness_panel || !ids.readiness_list) return;
  const checks = Array.isArray(data.checks) ? data.checks : [];
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

  const overall = data.overall || "ok";
  if (ids.readiness_summary_badge) {
    ids.readiness_summary_badge.dataset.state = overall;
    ids.readiness_summary_badge.textContent = readinessStatusLabel(overall);
  }
  ids.readiness_panel.dataset.state = overall;
  if (overall !== "ok") ids.readiness_panel.open = true;
}

export async function loadReadiness() {
  const response = await fetch("/system/readiness");
  const data = await response.json();
  renderReadiness(data);
}
