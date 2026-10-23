// Stage 1 UI entry point. Thin bootstrap: import every domain module (each
// wires its own event listeners as an import side effect) and run the
// cross-cutting initialization that touches more than one domain.

import { ids, project, defaults, initialStatus } from "./stage1/state.js";
import { appendLog, openTab, safely, syncTtsStyleAvailability } from "./stage1/core.js";
import { applyStatus, loadNormalization, loadMemory, hasUnsavedChanges, refreshStatus } from "./stage1/normalization.js";
import { loadReadiness } from "./stage1/readiness.js";
import { renderLive, restoreTtsJob } from "./stage1/tts.js";
import { restoreRecordedJob, restoreVoiceMode, scanRecordedInbox } from "./stage1/recorded_voiceover.js";
import { loadSyncWorkspace } from "./stage1/sync.js";
import { scanStage3MediaOnce } from "./stage1/stage3.js";
import { loadRevisionsScreen } from "./stage1/revisions.js";
import { startOpenRouterUsagePolling } from "./stage1/usage.js";

document.querySelectorAll(".workspace-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    openTab(tab.dataset.tab);
    if (tab.dataset.tab === "assembly") {
      scanStage3MediaOnce();
      loadSyncWorkspace(true).catch((err) => {
        appendLog(`Stage 2 sync: ${err.message || err}`, "warn");
      });
    } else if (tab.dataset.tab === "voiceover") {
      scanRecordedInbox().catch((err) => {
        appendLog(`Запись диктора: ${err.message || err}`, "warn");
      });
    } else if (tab.dataset.tab === "versions") {
      loadRevisionsScreen(true);
    }
  });
});

ids.btn_refresh.addEventListener("click", () => safely(async () => {
  await refreshStatus();
  await loadSyncWorkspace(true);
}));

ids.btn_clear_log.addEventListener("click", () => {
  ids.log_box.textContent = "";
});

if (ids.use_llm) ids.use_llm.checked = false;
ids.dry_run.checked = false;
ids.provider.value = defaults.provider === "macos"
  ? "macos"
  : (defaults.tts_model || "microsoft/mai-voice-2");
if (!ids.provider.value) ids.provider.value = "microsoft/mai-voice-2";
syncTtsStyleAvailability();
ids.gender.value = defaults.gender || "female";
applyStatus(initialStatus);
startOpenRouterUsagePolling();
renderLive();
appendLog(`UI ready. project=${project}`);
loadReadiness().catch((err) => appendLog(`Readiness init: ${err.message || err}`, "warn"));
Promise.all([loadNormalization(true), loadMemory(true)]).catch((err) => appendLog(`Normalization init: ${err.message}`, "warn"));
loadRevisionsScreen(true);
restoreVoiceMode();
restoreRecordedJob().catch((err) => appendLog(`Recorded voiceover init: ${err.message || err}`, "warn"));
restoreTtsJob().catch((err) => appendLog(`TTS status init: ${err.message || err}`, "warn"));
loadSyncWorkspace(true).catch((err) => appendLog(`Stage 2 sync init: ${err.message || err}`, "warn"));

window.addEventListener("beforeunload", (event) => {
  if (!hasUnsavedChanges()) return;
  event.preventDefault();
  event.returnValue = "";
});
