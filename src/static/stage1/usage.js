import { ids, initialStatus, project } from "./state.js";


export function renderOpenRouterUsage(summary) {
  if (!ids.openrouter_usage) return;
  if (!summary || summary.status === "unavailable") {
    ids.openrouter_usage.dataset.state = "unavailable";
    ids.openrouter_usage.textContent = "OpenRouter: учёт недоступен";
    return;
  }
  ids.openrouter_usage.dataset.state = "ok";
  const total = Number(summary.total_cost_usd || 0);
  const pending = Math.max(0, Number(summary.pending_count || 0));
  ids.openrouter_usage.textContent = `OpenRouter: $${total.toFixed(4)}`
    + (pending > 0 ? ` · уточняется ${pending}` : "");
}


export async function refreshOpenRouterUsage() {
  const response = await fetch(
    `/stage1/openrouter-usage?project=${encodeURIComponent(project)}`,
  );
  const payload = await response.json();
  if (!response.ok || !payload.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  renderOpenRouterUsage(payload.result || {});
}


export function startOpenRouterUsagePolling() {
  renderOpenRouterUsage(initialStatus.openrouter_usage || {});
  refreshOpenRouterUsage().catch(() => renderOpenRouterUsage({ status: "unavailable" }));
  window.setInterval(() => {
    if (document.hidden) return;
    refreshOpenRouterUsage().catch(() => renderOpenRouterUsage({ status: "unavailable" }));
  }, 5000);
}
