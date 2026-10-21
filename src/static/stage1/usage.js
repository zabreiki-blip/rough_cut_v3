import { ids, initialStatus, project } from "./state.js";


export function renderOpenRouterUsage(summary) {
  if (!ids.openrouter_usage) return;
  const totalElement = ids.openrouter_usage.querySelector(".openrouter-usage-total");
  const pendingElement = ids.openrouter_usage.querySelector(".openrouter-usage-pending");
  if (!totalElement || !pendingElement) return;
  if (!summary || summary.status === "unavailable") {
    ids.openrouter_usage.dataset.state = "unavailable";
    totalElement.textContent = "OpenRouter: учёт недоступен";
    pendingElement.textContent = "";
    pendingElement.hidden = true;
    return;
  }
  ids.openrouter_usage.dataset.state = "ok";
  const total = Number(summary.total_cost_usd || 0);
  const pending = Math.max(0, Number(summary.pending_count || 0));
  totalElement.textContent = `OpenRouter: $${total.toFixed(4)}`;
  pendingElement.textContent = pending > 0 ? `уточняется ${pending}` : "";
  pendingElement.hidden = pending <= 0;
}


export const USAGE_POLL_INTERVAL_MS = 5000;
export const USAGE_FETCH_TIMEOUT_MS = 10000;

let pollingActive = false;


export async function refreshOpenRouterUsage() {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), USAGE_FETCH_TIMEOUT_MS);
  try {
    const response = await fetch(
      `/stage1/openrouter-usage?project=${encodeURIComponent(project)}`,
      { signal: controller.signal },
    );
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    renderOpenRouterUsage(payload.result || {});
  } finally {
    window.clearTimeout(timer);
  }
}


// Один активный опрос: следующий запрос планируется только после завершения
// (успеха, ошибки или таймаута) предыдущего, поэтому запросы не накапливаются.
export function startOpenRouterUsagePolling() {
  renderOpenRouterUsage(initialStatus.openrouter_usage || {});
  if (pollingActive) return;
  pollingActive = true;
  const poll = async () => {
    try {
      if (!document.hidden) {
        await refreshOpenRouterUsage();
      }
    } catch (_error) {
      try {
        renderOpenRouterUsage({ status: "unavailable" });
      } catch (_renderError) {
        // Ошибка отрисовки не должна останавливать опрос.
      }
    } finally {
      window.setTimeout(poll, USAGE_POLL_INTERVAL_MS);
    }
  };
  poll();
}
