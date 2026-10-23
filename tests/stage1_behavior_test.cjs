/* eslint-env node */
"use strict";

// Behavior tests for the client-side Stage 1 UI logic.
//
// src/static/stage1.js is now a thin ES-module entry point that fans out
// into src/static/stage1/{core,state,normalization,...}.js. Rather than
// booting the whole UI (which would require mocking every DOM id touched
// by tts.js/sync.js/stage3.js/revisions.js), this test imports only the
// modules it needs directly via dynamic import(), after installing a
// minimal DOM stub. This keeps the test focused and cheap to maintain.
//
// Coverage, in priority order (see task notes for what was skipped and why):
//   1. computeEtaSeconds  (core.js)      - ETA math edge cases.
//   2. renderEtaCells     (core.js)      - "рассчитывается"/"готово" thresholds.
//   3. hasUnsavedChanges  (normalization.js) - dirty-flag contract that the
//      stage1.js entry point's `beforeunload` handler relies on. The handler
//      itself is two inline lines in stage1.js and is not exported, so we
//      exercise it via a local mirror that calls the real hasUnsavedChanges().
//   4. confirmDialog      (core.js)      - accept/cancel/Esc resolution and
//      protection against a stray repeat click resolving twice.

const assert = require("assert");
const path = require("path");
const { pathToFileURL } = require("url");

const root = path.resolve(__dirname, "..");
const staticDir = path.join(root, "src", "static", "stage1");

function moduleUrl(fileName) {
  return pathToFileURL(path.join(staticDir, fileName)).href;
}

// Minimal DOM Element mock - only what core.js/normalization.js touch.
class Element {
  constructor(id) {
    this.id = id;
    this.value = "";
    this.textContent = "";
    this.className = "";
    this.style = {};
    this.dataset = {};
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.attributes = {};
    this.children = [];
    this.onclick = null;
    this._listeners = {};
    this.classList = {
      toggle() {},
      add() {},
      remove() {},
      contains() { return false; },
    };
  }

  addEventListener(event, handler) {
    if (!this._listeners[event]) this._listeners[event] = [];
    this._listeners[event].push(handler);
  }

  removeEventListener(event, handler) {
    if (!this._listeners[event]) return;
    this._listeners[event] = this._listeners[event].filter((item) => item !== handler);
  }

  dispatch(event, payload) {
    (this._listeners[event] || []).forEach((handler) => handler(payload));
  }

  appendChild(child) { this.children.push(child); return child; }
  removeChild(child) { this.children = this.children.filter((item) => item !== child); }
  // toast() (core.js) schedules `t.remove()` on the toast node it appended;
  // a no-op keeps that fire-and-forget timer harmless in tests that (like
  // testSyncSourcesEditor) exercise code paths calling toast().
  remove() {}
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  focus() {}
  querySelector() { return new Element(); }
  querySelectorAll() { return []; }
}

function installBrowserGlobals() {
  const elements = {};
  const doc = {
    activeElement: null,
    getElementById(id) {
      if (!elements[id]) elements[id] = new Element(id);
      return elements[id];
    },
    createElement() { return new Element(); },
    querySelectorAll() { return []; },
    addEventListener() {},
    removeEventListener() {},
  };
  globalThis.document = doc;
  globalThis.window = globalThis;
  // Node's globalThis has no EventTarget API; revisions.js registers a window
  // "focus" listener at import time.
  globalThis.addEventListener = () => {};
  globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, addListener() {} });
  globalThis.localStorage = {
    values: {},
    getItem(key) { return this.values[key] || null; },
    setItem(key, value) { this.values[key] = String(value); },
  };
}

// stage1/state.js reads document/window at import time (building the `ids`
// map), so browser globals must exist before the first dynamic import().
installBrowserGlobals();

async function loadModules() {
  const state = await import(moduleUrl("state.js"));
  const core = await import(moduleUrl("core.js"));
  const normalization = await import(moduleUrl("normalization.js"));
  const sync = await import(moduleUrl("sync.js"));
  const tts = await import(moduleUrl("tts.js"));
  const revisions = await import(moduleUrl("revisions.js"));
  const usage = await import(moduleUrl("usage.js"));
  const stage3 = await import(moduleUrl("stage3.js"));
  return { state, core, normalization, sync, tts, revisions, usage, stage3 };
}

function testRestoredSpeakerDirectory({ state, tts }) {
  const base = `projects/${state.project}/speaker`;
  assert.strictEqual(
    tts.restoredSpeakerDirectory(`/Users/mac/Documents/AI/rough_cut_v3/${base}`), base,
  );
  assert.strictEqual(
    tts.restoredSpeakerDirectory(`/app/${base}/revisions/rev_0005`), base,
  );
  assert.strictEqual(
    tts.restoredSpeakerDirectory(`/app/${base}/draft`), `${base}/draft`,
  );
  assert.strictEqual(tts.restoredSpeakerDirectory("/tmp/unrelated/speaker"), null);
  assert.strictEqual(tts.restoredSpeakerDirectory(`/app/${base}/../elsewhere`), null);
}

async function testPhraseTtsTimeout({ core, tts }) {
  const originalFetch = globalThis.fetch;
  const originalSetTimeout = window.setTimeout;
  const originalClearTimeout = window.clearTimeout;
  const requestedTimeouts = [];
  try {
    window.setTimeout = (_callback, delay) => { requestedTimeouts.push(delay); return 1; };
    window.clearTimeout = () => {};
    globalThis.fetch = async () => ({ ok: true, json: async () => ({ ok: true, result: {} }) });
    await core.postJSON("/stage1/tts-phrase", {}, tts.PHRASE_TTS_TIMEOUT_MS);
    await core.postJSON("/stage1/tts-jobs/start", {});
    assert.deepStrictEqual(requestedTimeouts, [20 * 60 * 1000, core.FETCH_TIMEOUT_MS]);
  } finally {
    globalThis.fetch = originalFetch;
    window.setTimeout = originalSetTimeout;
    window.clearTimeout = originalClearTimeout;
  }
}

async function testRevisionImportTimeout({ state, revisions }) {
  const { ids, appState } = state;
  const originalFetch = globalThis.fetch;
  const originalSetTimeout = window.setTimeout;
  const originalClearTimeout = window.clearTimeout;
  const originalNormalization = appState.normalization;
  const select = ids.inbox_scenario_select;
  const originalSelectValue = select.value;
  assert.strictEqual(revisions.REVISION_IMPORT_TIMEOUT_MS, 20 * 60 * 1000);
  const requestedTimeouts = [];
  const requests = [];
  try {
    appState.normalization = { blocks: [] };
    select.value = "Сценарии/slow.docx";
    // Fire only the import timer immediately: the mocked fetch then rejects
    // once its AbortSignal is aborted, like a browser giving up on the request.
    window.setTimeout = (callback, delay) => {
      requestedTimeouts.push(delay);
      if (delay === revisions.REVISION_IMPORT_TIMEOUT_MS) callback();
      return 1;
    };
    window.clearTimeout = () => {};
    globalThis.fetch = async (url) => {
      const target = String(url);
      requests.push(target);
      if (target.includes("/stage4/revisions/import")) {
        throw new Error("aborted");
      }
      return { ok: true, json: async () => ({ ok: true, result: { revisions: [] } }) };
    };
    await assert.rejects(revisions.importRevision(), (err) => {
      assert.ok(err.message.includes("импорт мог завершиться"), err.message);
      assert.strictEqual(err.status, 408);
      return true;
    });
    assert.deepStrictEqual(requestedTimeouts.slice(0, 1), [20 * 60 * 1000]);
    const importIndex = requests.findIndex((target) => target.includes("/stage4/revisions/import"));
    assert.ok(importIndex >= 0);
    assert.ok(
      requests.slice(importIndex + 1).some((target) => target.includes("/stage4/revisions?")),
      "timeout must re-read the revision list",
    );
  } finally {
    globalThis.fetch = originalFetch;
    window.setTimeout = originalSetTimeout;
    window.clearTimeout = originalClearTimeout;
    appState.normalization = originalNormalization;
    select.value = originalSelectValue;
  }
}

async function testOpenRouterUsagePollingSinglePending({ usage }) {
  const originalFetch = globalThis.fetch;
  const originalSetTimeout = window.setTimeout;
  const originalClearTimeout = window.clearTimeout;
  const timers = [];
  let nextTimerId = 1;
  let fetchCalls = 0;
  let pending = 0;
  let maxPending = 0;
  const flush = () => new Promise((resolve) => setImmediate(resolve));
  const fire = async (delay) => {
    const index = timers.findIndex((timer) => timer.delay === delay);
    assert.ok(index >= 0, `no timer scheduled with delay ${delay}`);
    const [timer] = timers.splice(index, 1);
    timer.callback();
    await flush();
  };
  try {
    window.setTimeout = (callback, delay) => {
      timers.push({ id: nextTimerId, callback, delay });
      return nextTimerId++;
    };
    window.clearTimeout = (id) => {
      const index = timers.findIndex((timer) => timer.id === id);
      if (index >= 0) timers.splice(index, 1);
    };
    // Сервер «завис»: fetch не завершается, пока его не прервут через signal.
    globalThis.fetch = (_url, options = {}) => {
      fetchCalls += 1;
      pending += 1;
      maxPending = Math.max(maxPending, pending);
      return new Promise((_resolve, reject) => {
        options.signal.addEventListener("abort", () => {
          pending -= 1;
          reject(new Error("aborted"));
        });
      });
    };

    usage.startOpenRouterUsagePolling();
    await flush();
    assert.strictEqual(fetchCalls, 1);
    // Повторный старт не создаёт второй опрос.
    usage.startOpenRouterUsagePolling();
    await flush();
    assert.strictEqual(fetchCalls, 1);
    // Пока запрос висит, интервальных тиков нет: следующий опрос не запланирован.
    assert.ok(!timers.some((timer) => timer.delay === usage.USAGE_POLL_INTERVAL_MS));
    // Таймаут прерывает запрос; только после этого планируется следующий опрос.
    await fire(usage.USAGE_FETCH_TIMEOUT_MS);
    assert.strictEqual(pending, 0);
    assert.strictEqual(fetchCalls, 1);
    for (let round = 2; round <= 4; round += 1) {
      await fire(usage.USAGE_POLL_INTERVAL_MS);
      assert.strictEqual(fetchCalls, round);
      assert.strictEqual(pending, 1);
      await fire(usage.USAGE_FETCH_TIMEOUT_MS);
      assert.strictEqual(pending, 0);
    }
    assert.strictEqual(maxPending, 1);
  } finally {
    globalThis.fetch = originalFetch;
    window.setTimeout = originalSetTimeout;
    window.clearTimeout = originalClearTimeout;
  }
}

async function testOpenRouterUsagePollingSurvivesRenderError({ state }) {
  // Свежий экземпляр модуля: флаг pollingActive у общего экземпляра уже взведён предыдущим тестом.
  const usage = await import(`${moduleUrl("usage.js")}?fresh=polling-render-error`);
  const originalFetch = globalThis.fetch;
  const originalSetTimeout = window.setTimeout;
  const originalClearTimeout = window.clearTimeout;
  const originalUsageElement = state.ids.openrouter_usage;
  const timers = [];
  let nextTimerId = 1;
  let fetchCalls = 0;
  let renderFails = false;
  const flush = () => new Promise((resolve) => setImmediate(resolve));
  const fire = async (delay) => {
    const index = timers.findIndex((timer) => timer.delay === delay);
    assert.ok(index >= 0, `no timer scheduled with delay ${delay}`);
    const [timer] = timers.splice(index, 1);
    timer.callback();
    await flush();
  };
  try {
    // Элемент виджета: рендер падает только после старта, когда уже идёт опрос.
    state.ids.openrouter_usage = {
      dataset: {},
      querySelector() {
        if (renderFails) throw new Error("render failed");
        return null;
      },
    };
    window.setTimeout = (callback, delay) => {
      timers.push({ id: nextTimerId, callback, delay });
      return nextTimerId++;
    };
    window.clearTimeout = (id) => {
      const index = timers.findIndex((timer) => timer.id === id);
      if (index >= 0) timers.splice(index, 1);
    };
    // Сеть падает сразу: запрос завершается ошибкой, затем срабатывает ошибка рендера.
    globalThis.fetch = () => {
      fetchCalls += 1;
      return Promise.reject(new Error("network down"));
    };

    usage.startOpenRouterUsagePolling();
    // Рендер начинает падать уже после старта, когда опрос запущен.
    renderFails = true;
    await flush();
    assert.strictEqual(fetchCalls, 1);
    // Даже при ошибке рендера следующий опрос должен быть запланирован.
    assert.ok(
      timers.some((timer) => timer.delay === usage.USAGE_POLL_INTERVAL_MS),
      "next poll must be scheduled even when render throws",
    );
    for (let round = 2; round <= 3; round += 1) {
      await fire(usage.USAGE_POLL_INTERVAL_MS);
      assert.strictEqual(fetchCalls, round);
    }
  } finally {
    state.ids.openrouter_usage = originalUsageElement;
    globalThis.fetch = originalFetch;
    window.setTimeout = originalSetTimeout;
    window.clearTimeout = originalClearTimeout;
  }
}

function testStyleDegradedWarning({ state, tts }) {
  const { ids, live } = state;
  const warning = ids.live_style_warning;
  const original = { ...live };
  try {
    // Degradation present: visible warning with count, total and block ids.
    Object.assign(live, { status: "completed", total: 5, styleDegradedIds: ["zk1", "<b>x</b>"] });
    tts.renderLive();
    assert.strictEqual(warning.hidden, false);
    assert.ok(warning.textContent.includes("2 из 5"), warning.textContent);
    assert.ok(warning.textContent.includes("zk1, <b>x</b>"), warning.textContent);
    assert.strictEqual(warning.innerHTML, undefined, "ids must go through textContent only");

    // Long list is truncated with a "и ещё N" tail.
    const many = Array.from({ length: 25 }, (_v, i) => `b${i}`);
    const text = tts.styleDegradedMessage(many, 25);
    assert.ok(text.includes("b19") && !text.includes("b20,") && text.includes("и ещё 5"), text);

    // No degradation: nothing shown.
    Object.assign(live, { styleDegradedIds: [] });
    tts.renderLive();
    assert.strictEqual(warning.hidden, true);
    assert.strictEqual(warning.textContent, "");
    assert.strictEqual(tts.styleDegradedMessage(undefined, 3), "");
  } finally {
    Object.assign(live, original);
    tts.renderLive();
  }
}

function testComputeEtaSeconds({ core }) {
  const { computeEtaSeconds } = core;
  const now = Date.now();

  // Invalid start timestamp: no crash, no divide-by-zero.
  assert.deepStrictEqual(computeEtaSeconds(NaN, 0.5), { elapsedSec: 0, etaSec: 0 });

  // fraction === 0: elapsed keeps ticking, ETA withheld.
  const zero = computeEtaSeconds(now - 1000, 0);
  assert.strictEqual(zero.etaSec, 0);
  assert.ok(zero.elapsedSec > 0);

  // fraction below the 5% noise threshold: ETA withheld.
  const tiny = computeEtaSeconds(now - 1000, 0.03);
  assert.strictEqual(tiny.etaSec, 0);

  // fraction === 1 (just completed): must not divide by zero.
  const complete = computeEtaSeconds(now - 1000, 1);
  assert.strictEqual(complete.etaSec, 0);

  // fraction > 1 (defensive against bad server data): still no negative ETA.
  const overshoot = computeEtaSeconds(now - 1000, 1.5);
  assert.strictEqual(overshoot.etaSec, 0);

  // Valid fraction: ~10s elapsed at 50% done -> ~10s remaining.
  const half = computeEtaSeconds(now - 10000, 0.5);
  assert.ok(half.etaSec > 8 && half.etaSec < 12, `etaSec=${half.etaSec}`);

  // startedAtMs in the future (clock skew) must clamp elapsed/eta at 0,
  // never negative.
  const future = computeEtaSeconds(now + 5000, 0.5);
  assert.strictEqual(future.elapsedSec, 0);
  assert.strictEqual(future.etaSec, 0);
}

function testRenderEtaCells({ core }) {
  const { renderEtaCells } = core;
  const now = Date.now();
  const mockEl = () => ({ textContent: "" });

  // completed status always reports "готово", regardless of fraction.
  const doneEta = mockEl();
  renderEtaCells(mockEl(), doneEta, now - 1000, 1, "completed");
  assert.strictEqual(doneEta.textContent, "готово");

  // Below the 5% threshold: "рассчитывается" even while running.
  const earlyEta = mockEl();
  renderEtaCells(mockEl(), earlyEta, now - 100, 0.03, "running");
  assert.strictEqual(earlyEta.textContent, "рассчитывается");

  // At/above threshold with a running job: a numeric ETA is shown.
  const etaCell = mockEl();
  renderEtaCells(mockEl(), etaCell, now - 10000, 0.5, "running");
  assert.match(etaCell.textContent, /^≈ /);

  // fraction === 1 but status still "running" (race before the completed
  // event lands): must not divide by zero, falls back to "рассчитывается".
  const raceEta = mockEl();
  renderEtaCells(mockEl(), raceEta, now - 1000, 1, "running");
  assert.strictEqual(raceEta.textContent, "рассчитывается");

  // Elapsed-time formatting sanity check (independent of the ETA branch).
  const elapsedCell = mockEl();
  renderEtaCells(elapsedCell, mockEl(), now - 3661000, 0.5, "running");
  assert.strictEqual(elapsedCell.textContent, "1:01:01");

  // Cells that are not yet in the DOM (null) must never crash rendering.
  assert.doesNotThrow(() => renderEtaCells(null, null, now - 1000, 0.5, "running"));
}

function testCloudDisablesLocalWhisperModel({ state, core }) {
  const { ids, appState } = state;
  const originalEngine = ids.sync_asr_engine.value;
  const originalRunning = appState.syncJobRunning;
  try {
    appState.syncJobRunning = false;
    ids.sync_asr_engine.value = "cloud";
    ids.sync_asr_engine.dispatch("change");
    assert.strictEqual(ids.sync_whisper_model.disabled, true);

    // The shared availability refresh must preserve the cloud-only lock.
    core.updateActionAvailability();
    assert.strictEqual(ids.sync_whisper_model.disabled, true);

    ids.sync_asr_engine.value = "local";
    ids.sync_asr_engine.dispatch("change");
    assert.strictEqual(ids.sync_whisper_model.disabled, false);
  } finally {
    ids.sync_asr_engine.value = originalEngine;
    appState.syncJobRunning = originalRunning;
    core.updateActionAvailability();
  }
}

function testHasUnsavedChangesAndBeforeunload({ state, normalization }) {
  const { appState } = state;
  const { hasUnsavedChanges, markNormalizationDirty } = normalization;

  // Mirrors the two-line handler wired in stage1.js's entry point:
  //   window.addEventListener("beforeunload", (event) => {
  //     if (!hasUnsavedChanges()) return;
  //     event.preventDefault();
  //     event.returnValue = "";
  //   });
  // That handler is inline and not exported, so this exercises the real,
  // exported hasUnsavedChanges() contract it depends on.
  function beforeunloadHandler(event) {
    if (!hasUnsavedChanges()) return;
    event.preventDefault();
    event.returnValue = "";
  }

  const originalNormalization = appState.normalization;
  const originalDirty = appState.normalizationDirty;
  try {
    appState.normalization = null;
    appState.normalizationDirty = false;
    assert.strictEqual(hasUnsavedChanges(), false);

    const cleanEvent = {
      returnValue: undefined,
      defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; },
    };
    beforeunloadHandler(cleanEvent);
    assert.strictEqual(cleanEvent.defaultPrevented, false, "no warning without unsaved changes");
    assert.strictEqual(cleanEvent.returnValue, undefined);

    // Without a loaded normalization draft, marking dirty is a no-op.
    markNormalizationDirty();
    assert.strictEqual(hasUnsavedChanges(), false);

    // With a draft loaded, marking dirty flips the shared appState flag.
    appState.normalization = { status: "draft", blocks: [] };
    markNormalizationDirty();
    assert.strictEqual(hasUnsavedChanges(), true);

    const dirtyEvent = {
      returnValue: undefined,
      defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; },
    };
    beforeunloadHandler(dirtyEvent);
    assert.strictEqual(dirtyEvent.defaultPrevented, true, "warning must appear with unsaved changes");
    assert.strictEqual(dirtyEvent.returnValue, "");
  } finally {
    appState.normalization = originalNormalization;
    appState.normalizationDirty = originalDirty;
  }
}

async function testConfirmDialog({ state, core }) {
  const { ids } = state;
  const { confirmDialog } = core;
  const dialog = ids.confirm_dialog;
  const acceptBtn = ids.btn_confirm_dialog_accept;
  const cancelBtn = ids.btn_confirm_dialog_cancel;

  // Accept path: resolves true, populates title/message/labels, and detaches
  // its own click handler once settled.
  const acceptPromise = confirmDialog({
    title: "Удалить блок?",
    message: "Действие необратимо.",
    confirmLabel: "Удалить",
    cancelLabel: "Оставить",
  });
  assert.strictEqual(ids.confirm_dialog_title.textContent, "Удалить блок?");
  assert.strictEqual(ids.confirm_dialog_message.textContent, "Действие необратимо.");
  assert.strictEqual(acceptBtn.textContent, "Удалить");
  assert.strictEqual(cancelBtn.textContent, "Оставить");

  const acceptHandler = acceptBtn.onclick;
  acceptHandler();
  assert.strictEqual(await acceptPromise, true);
  assert.strictEqual(acceptBtn.onclick, null, "click handler must be detached once settled");

  // A stray repeat of the captured click handler (e.g. a queued duplicate
  // event) must not throw and must not attempt a second resolve.
  assert.doesNotThrow(() => acceptHandler());

  // Cancel-button path resolves false.
  const cancelPromise = confirmDialog({ title: "T2", message: "M2" });
  cancelBtn.onclick();
  assert.strictEqual(await cancelPromise, false);

  // Esc / native <dialog> "cancel" event resolves false too.
  const escPromise = confirmDialog({ title: "T3", message: "M3" });
  dialog.dispatch("cancel", { preventDefault() {} });
  assert.strictEqual(await escPromise, false);
}

// Card 5: matches whose sources were skipped because the registered
// speaker(s) didn't match the block's speaker (metadata.speaker_filtered_sources,
// see docs/... normalization/matching audit). Operators previously only saw
// "не найдено" with no explanation; the card must now surface why and how to
// fix it, without leaking unescaped Premiere sequence/speaker names into the DOM.
async function testSyncMatchSpeakerFilterHint({ state, sync }) {
  const { ids, appState } = state;
  const originalFetch = globalThis.fetch;
  const originalSyncFeatureAvailable = appState.syncFeatureAvailable;
  const originalSyncSources = appState.syncSources;

  function jsonResponse(body) {
    return { ok: true, status: 200, text: async () => JSON.stringify(body) };
  }

  function installFetch(sourcesDoc, matchesDoc) {
    globalThis.fetch = async (url) => {
      const target = String(url);
      if (target.includes("/sync-sources")) return jsonResponse({ ok: true, result: sourcesDoc });
      if (target.includes("/sync-matches")) return jsonResponse({ ok: true, result: matchesDoc });
      if (target.includes("/sync-jobs/status")) return { ok: false, status: 404, text: async () => "" };
      throw new Error(`testSyncMatchSpeakerFilterHint: unexpected fetch ${target}`);
    };
  }

  try {
    const sourcesDoc = {
      items: [{ id: "media_e4efc08fdb291ba81715", sequence_name: "Церковь, камера 3" }],
    };

    // Case 1: speaker_filtered_sources present -> hint rendered, source name
    // resolved via the sources catalog, and the raw speaker label (which can
    // contain arbitrary Premiere text) is HTML-escaped rather than injected.
    installFetch(sourcesDoc, {
      items: [{
        segment_id: "snh1",
        status: "not_found",
        speaker: "ЕЛЕНА МИНЧЕНКО",
        metadata: {
          speaker_filtered_sources: [
            { source_id: "media_e4efc08fdb291ba81715", source_speakers: ['KOSTEL <SP> 001 & "quote"'] },
          ],
        },
      }],
    });
    await sync.loadSyncWorkspace(true);
    const withHint = ids.sync_review_list.innerHTML;
    assert.match(withHint, /sync-match-speaker-filter/, "hint block must render for not_found matches");
    assert.match(withHint, /Церковь, камера 3/, "hint must show the human-readable source name");
    assert.match(withHint, /все спикеры/, "hint must point to the «все спикеры» fix");
    assert.match(withHint, /&lt;SP&gt;/, "special characters in the speaker label must be escaped");
    assert.ok(!withHint.includes("<SP>"), "raw unescaped speaker markup must never reach the DOM");
    assert.ok(!withHint.includes('"quote"'), "raw quote characters must be escaped, not passed through");

    // Case 2: same shape but status "review" (needs_review) must also show
    // the hint - it is not exclusive to not_found matches.
    installFetch(sourcesDoc, {
      items: [{
        segment_id: "snh2",
        status: "review",
        speaker: "ЕЛЕНА МИНЧЕНКО",
        metadata: {
          speaker_filtered_sources: [
            { source_id: "media_e4efc08fdb291ba81715", source_speakers: ["KOSTEL SP 001"] },
          ],
        },
      }],
    });
    await sync.loadSyncWorkspace(true);
    assert.match(
      ids.sync_review_list.innerHTML,
      /sync-match-speaker-filter/,
      "hint must also render for needs_review matches",
    );

    // Case 3: no speaker_filtered_sources key at all -> no hint block.
    installFetch(sourcesDoc, {
      items: [{
        segment_id: "snh3",
        status: "not_found",
        speaker: "ЕЛЕНА МИНЧЕНКО",
      }],
    });
    await sync.loadSyncWorkspace(true);
    assert.ok(
      !ids.sync_review_list.innerHTML.includes("sync-match-speaker-filter"),
      "hint must be absent when speaker_filtered_sources is missing",
    );

    // Case 4: empty speaker_filtered_sources array -> no hint block.
    installFetch(sourcesDoc, {
      items: [{
        segment_id: "snh4",
        status: "not_found",
        speaker: "ЕЛЕНА МИНЧЕНКО",
        metadata: { speaker_filtered_sources: [] },
      }],
    });
    await sync.loadSyncWorkspace(true);
    assert.ok(
      !ids.sync_review_list.innerHTML.includes("sync-match-speaker-filter"),
      "hint must be absent when speaker_filtered_sources is empty",
    );
  } finally {
    globalThis.fetch = originalFetch;
    appState.syncFeatureAvailable = originalSyncFeatureAvailable;
    appState.syncSources = originalSyncSources;
  }
}

// Audit 2026-10-21 #1: a confirmed match flagged retime_pending_review (word times
// were retimed under kept manual boundaries) must reappear in the review queue,
// be labelled, show the kept boundaries, and count as needing review.
async function testSyncRetimePendingReviewQueue({ state, sync }) {
  const { ids, appState } = state;
  const originalFetch = globalThis.fetch;
  const originalSyncFeatureAvailable = appState.syncFeatureAvailable;
  const originalSyncSources = appState.syncSources;

  function jsonResponse(body) {
    return { ok: true, status: 200, text: async () => JSON.stringify(body) };
  }

  function installFetch(matchesDoc) {
    globalThis.fetch = async (url) => {
      const target = String(url);
      if (target.includes("/sync-sources")) return jsonResponse({ ok: true, result: { items: [] } });
      if (target.includes("/sync-matches")) return jsonResponse({ ok: true, result: matchesDoc });
      if (target.includes("/sync-jobs/status")) return { ok: false, status: 404, text: async () => "" };
      throw new Error(`testSyncRetimePendingReviewQueue: unexpected fetch ${target}`);
    };
  }

  const confirmed = (segmentId, metadata) => ({
    segment_id: segmentId,
    status: "confirmed",
    speaker: "ЕЛЕНА МИНЧЕНКО",
    source_id: "media_a1",
    source_in_sec: 8.4,
    source_out_sec: 14.0,
    metadata,
  });

  try {
    // Flagged confirmed match: in the queue, labelled, kept boundaries visible.
    installFetch({
      items: [confirmed("snh1", { retime_pending_review: true }), confirmed("snh2", {})],
    });
    await sync.loadSyncWorkspace(true);
    const html = ids.sync_review_list.innerHTML;
    assert.match(html, /data-match-id="snh1"/, "flagged confirmed match must be in the review queue");
    assert.ok(!html.includes('data-match-id="snh2"'), "plain confirmed match must stay out of the queue");
    assert.match(html, /границы изменились — проверьте/, "card must carry the retime review label");
    assert.match(html, /data-sync-in[^>]*value="8\.400"/, "kept manual Source in must be shown");
    assert.match(html, /data-sync-out[^>]*value="14\.000"/, "kept manual Source out must be shown");
    assert.match(html, /confirm-sync-match/, "editor must be able to re-confirm explicitly");
    assert.strictEqual(ids.sync_count_review.textContent, "1", "flagged match counts as review");
    assert.strictEqual(ids.sync_count_confirmed.textContent, "1", "only the plain match counts as confirmed");

    // Falsy flag: nothing to review.
    installFetch({ items: [confirmed("snh3", { retime_pending_review: false })] });
    await sync.loadSyncWorkspace(true);
    assert.ok(!ids.sync_review_list.innerHTML.includes("sync-match-card"));

    // Audit 2026-10-21 #3: needs_review caused by unrefined (interpolated) times
    // explains itself on the card.
    installFetch({
      items: [{
        ...confirmed("snh4", { timing_flags: ["timing_unrefined"], review_reason: "timing_unrefined" }),
        status: "needs_review",
      }],
    });
    await sync.loadSyncWorkspace(true);
    assert.match(
      ids.sync_review_list.innerHTML,
      /время слов в расшифровке приблизительное/,
      "unrefined timing reason must be shown on the review card",
    );
  } finally {
    globalThis.fetch = originalFetch;
    appState.syncFeatureAvailable = originalSyncFeatureAvailable;
    appState.syncSources = originalSyncSources;
  }
}

// Card 6: the editable sync-sources list added to the web panel so an
// operator no longer has to reopen the CEP panel in Premiere to fix a
// source's speaker names or "all speakers" flag - a wrong/missing speaker
// on a source silently excludes it from matching (see
// docs/archive/audits/NORMALIZATION_MATCHING_AUDIT_2026-09-07.md).
async function testSyncSourcesEditor({ state, sync }) {
  const { ids, appState, project } = state;
  const originalFetch = globalThis.fetch;
  const originalSyncFeatureAvailable = appState.syncFeatureAvailable;
  const originalSyncSources = appState.syncSources;

  function jsonResponse(body) {
    return { ok: true, status: 200, text: async () => JSON.stringify(body) };
  }

  try {
    // --- Rendering: fields + checkbox reflect the loaded sources, and any
    // Premiere-supplied text (sequence/speaker names) is HTML-escaped. ---
    const sourcesDoc = {
      items: [
        {
          id: "media_a1",
          sequence_name: 'Церковь "А" <camera>',
          speaker: "Иван & Пётр",
          speakers: ["<script>alert(1)</script>"],
          all_speakers: false,
          duration_sec: 125,
        },
        {
          id: "media_b2",
          sequence_name: "Секвенция Б",
          all_speakers: true,
          duration_sec: 0,
        },
      ],
    };
    globalThis.fetch = async (url) => {
      const target = String(url);
      if (target.includes("/sync-sources")) return jsonResponse({ ok: true, result: sourcesDoc });
      if (target.includes("/sync-matches")) return jsonResponse({ ok: true, result: { items: [] } });
      if (target.includes("/sync-jobs/status")) return { ok: false, status: 404, text: async () => "" };
      throw new Error(`testSyncSourcesEditor: unexpected fetch ${target}`);
    };
    await sync.loadSyncWorkspace(true);
    const html = ids.sync_sources_list.innerHTML;

    assert.ok(
      !html.includes("<script>alert(1)</script>"),
      "raw speaker markup must never reach the DOM",
    );
    assert.match(
      html,
      /value="Иван &amp; Пётр, &lt;script&gt;alert\(1\)&lt;\/script&gt;"/,
      "speakers field must join speaker+speakers, escaped",
    );
    assert.match(html, /data-source-id="media_a1"/);
    assert.match(html, /data-source-id="media_b2"/);

    const cardA = html.match(/data-source-id="media_a1"[\s\S]*?<\/article>/)[0];
    assert.ok(
      !/data-sync-source-all[^>]*checked/.test(cardA),
      "all_speakers=false must not render a checked checkbox",
    );
    assert.match(cardA, /Область поиска: Иван &amp; Пётр/, "scope text lists the speaker names");

    const cardB = html.match(/data-source-id="media_b2"[\s\S]*?<\/article>/)[0];
    assert.match(cardB, /data-sync-source-all checked/, "all_speakers=true must render a checked checkbox");
    assert.match(cardB, /value=""/, "a source without speakers renders an empty speakers field");
    assert.match(cardB, /Область поиска: все спикеры/);
  } finally {
    globalThis.fetch = originalFetch;
    appState.syncFeatureAvailable = originalSyncFeatureAvailable;
    appState.syncSources = originalSyncSources;
  }

  // --- Payload builder: speaker/speakers stay consistent (first name in
  // `speaker`, the rest in `speakers`) so the backend merge in
  // api_stage2_routes.py never wipes one field with the other. ---
  const { syncSourceEditPayload } = sync;
  const source = { id: "media_a1" };
  assert.deepStrictEqual(
    syncSourceEditPayload(source, "Иван Иванов, Пётр Петров", true),
    {
      project,
      id: "media_a1",
      source_id: "media_a1",
      speaker: "Иван Иванов",
      speakers: ["Пётр Петров"],
      all_speakers: true,
    },
  );

  // An empty speakers field is a valid case: the source becomes searchable
  // for every speaker (empty `speaker` + empty `speakers`, not omitted).
  const emptyPayload = syncSourceEditPayload(source, "   ", false);
  assert.strictEqual(emptyPayload.speaker, "");
  assert.deepStrictEqual(emptyPayload.speakers, []);
  assert.strictEqual(emptyPayload.all_speakers, false);

  // --- Save flow: verifies the actual POST body sent to the merge endpoint. ---
  const originalFetch2 = globalThis.fetch;
  const originalSyncFeatureAvailable2 = appState.syncFeatureAvailable;
  const originalSyncSources2 = appState.syncSources;
  let capturedRequest = null;
  try {
    globalThis.fetch = async (url, options) => {
      const target = String(url);
      if (target === "/stage2/sync-sources" && (options || {}).method === "POST") {
        capturedRequest = { url: target, body: JSON.parse(options.body) };
        return jsonResponse({ ok: true, result: { project, source: {} } });
      }
      if (target.includes("/sync-sources")) return jsonResponse({ ok: true, result: { items: [] } });
      if (target.includes("/sync-matches")) return jsonResponse({ ok: true, result: { items: [] } });
      if (target.includes("/sync-jobs/status")) return { ok: false, status: 404, text: async () => "" };
      throw new Error(`testSyncSourcesEditor save: unexpected fetch ${target}`);
    };
    await sync.saveSyncSourceEdit({ id: "media_a1" }, "Мария Сидорова", false);
    assert.ok(capturedRequest, "save must POST to /stage2/sync-sources");
    assert.strictEqual(capturedRequest.url, "/stage2/sync-sources");
    assert.deepStrictEqual(capturedRequest.body, {
      project,
      id: "media_a1",
      source_id: "media_a1",
      speaker: "Мария Сидорова",
      speakers: [],
      all_speakers: false,
    });
  } finally {
    globalThis.fetch = originalFetch2;
    appState.syncFeatureAvailable = originalSyncFeatureAvailable2;
    appState.syncSources = originalSyncSources2;
  }
}

// Stage 4 inbox: importRevision() check order + inbox re-read, and the
// debounced refresh on window return while the "Редакции" tab is active.
async function testRevisionsInbox({ state, revisions }) {
  const { ids, appState } = state;
  const originalFetch = globalThis.fetch;
  const originalNormalization = appState.normalization;
  const originalQuerySelector = document.querySelector;
  const originalNow = Date.now;
  const select = ids.inbox_scenario_select;
  // The mock <select> does not parse options: emulate "first option selected".
  Object.defineProperty(select, "innerHTML", {
    configurable: true,
    set(html) { select.value = (/<option value="([^"]*)"/.exec(html) || [])[1] || ""; },
  });
  const inboxCalls = [];
  let inboxFiles = [];
  const importPosts = [];
  globalThis.fetch = async (url, options = {}) => {
    const target = String(url);
    if (target.includes("/stage4/inbox")) {
      inboxCalls.push(target);
      return { ok: true, json: async () => ({ ok: true, result: { scenario_files: inboxFiles, review_files: [] } }) };
    }
    if (target.includes("/stage4/revisions/import")) {
      importPosts.push(JSON.parse(options.body));
      throw new Error("stop-after-import-post");
    }
    throw new Error(`testRevisionsInbox: unexpected fetch ${target}`);
  };
  const rejectsWith = async (fragment) => {
    await assert.rejects(revisions.importRevision(), (err) => {
      assert.ok(err.message.includes(fragment), err.message);
      return true;
    });
  };
  try {
    // 1. No normalization: error comes first, no inbox request, even with an empty select.
    select.value = "";
    appState.normalization = null;
    await rejectsWith("Сначала нормализуйте и подтвердите исходный сценарий");
    appState.normalization = { blocks: "nope" };
    await rejectsWith("Сначала нормализуйте и подтвердите исходный сценарий");
    assert.strictEqual(inboxCalls.length, 0);

    // 2. Normalized, select empty, inbox still empty after re-read.
    appState.normalization = { blocks: [] };
    await rejectsWith("нет новых файлов сценария");
    assert.strictEqual(inboxCalls.length, 1, "empty select must trigger one inbox re-read");

    // 3. File appeared after page load: re-read fills select, import proceeds.
    inboxFiles = [{ relative_path: "Сценарии/new.docx", name: "new.docx", fingerprint: "sha256:abc" }];
    await rejectsWith("stop-after-import-post");
    assert.strictEqual(inboxCalls.length, 2);
    assert.strictEqual(importPosts.length, 1);
    assert.strictEqual(importPosts[0].input_path, "Сценарии/new.docx");

    // 4. Select already filled: no extra inbox request.
    select.value = "Сценарии/new.docx";
    await rejectsWith("stop-after-import-post");
    assert.strictEqual(inboxCalls.length, 2);

    // 5. Return-to-window refresh: only on "versions" tab, debounced.
    let activeTab = "assembly";
    document.querySelector = () => ({ dataset: { tab: activeTab } });
    let now = 1_000_000;
    Date.now = () => now;
    inboxCalls.length = 0;
    await revisions.refreshInboxOnReturn();
    assert.strictEqual(inboxCalls.length, 0, "inactive versions tab must not refresh");
    activeTab = "versions";
    await revisions.refreshInboxOnReturn();
    assert.strictEqual(inboxCalls.length, 1);
    now += 500;
    await revisions.refreshInboxOnReturn();
    assert.strictEqual(inboxCalls.length, 1, "repeat within debounce window is skipped");
    now += 5000;
    await revisions.refreshInboxOnReturn();
    assert.strictEqual(inboxCalls.length, 2);
  } finally {
    globalThis.fetch = originalFetch;
    appState.normalization = originalNormalization;
    document.querySelector = originalQuerySelector;
    Date.now = originalNow;
    delete select.innerHTML;
    select.value = "";
  }
}

async function testBuildTimelineSurfacesWarnings({ state, tts }) {
  const { ids, appState, toastContainer } = state;
  const originalFetch = globalThis.fetch;
  const originalLog = ids.log_box.textContent;
  const originalChildren = toastContainer.children;
  const originalReady = appState.timelineReady;
  const base = { zk: 3, snh: 2, total_duration_sec: 75 };
  const run = async (result) => {
    ids.log_box.textContent = "";
    toastContainer.children = [];
    globalThis.fetch = async () => ({ ok: true, json: async () => ({ ok: true, result }) });
    await tts.doBuildTimeline();
    return { log: ids.log_box.textContent, toasts: toastContainer.children.map((t) => t.className) };
  };
  try {
    // Warnings + unresolved sync matches: warnings logged, warn toast, draft marker.
    const dirty = await run({
      ...base,
      matched: 1,
      review: 1,
      unmatched: 0,
      warnings: ["ZK без WAV (будут пропущены в XML): zk_007", "Устаревшие sync matches пропущены: snh_003"],
    });
    assert.ok(dirty.log.includes("[WARN] ZK без WAV"), dirty.log);
    assert.ok(dirty.log.includes("[WARN] Устаревшие sync matches"), dirty.log);
    assert.ok(/сопоставлено=1.*проверка=1.*без совпадения=0/.test(dirty.log), dirty.log);
    assert.ok(dirty.log.includes("черновик с заглушками"), dirty.log);
    assert.ok(!dirty.log.includes("Timeline built:"), "must not read as clean success");
    assert.ok(dirty.toasts.some((c) => c.includes("warn")), JSON.stringify(dirty.toasts));
    assert.ok(!dirty.toasts.some((c) => c.includes("success")), JSON.stringify(dirty.toasts));
    assert.strictEqual(ids.btn_export_xml.disabled, false);
    assert.strictEqual(appState.timelineReady, true);

    // Clean build: no warnings, no draft marker, no warn toast.
    const clean = await run({ ...base, matched: 2, review: 0, unmatched: 0, warnings: [] });
    assert.ok(clean.log.includes("Timeline built:"), clean.log);
    assert.ok(/сопоставлено=2/.test(clean.log), clean.log);
    assert.ok(!clean.log.includes("черновик"), clean.log);
    assert.ok(!clean.log.includes("[WARN]"), clean.log);
    assert.ok(!clean.toasts.some((c) => c.includes("warn")), JSON.stringify(clean.toasts));

    // Only unmatched sync (no warnings array entries): still a draft.
    const unmatched = await run({ ...base, matched: 0, review: 0, unmatched: 2 });
    assert.ok(unmatched.log.includes("черновик с заглушками"), unmatched.log);
    assert.ok(unmatched.toasts.some((c) => c.includes("warn")), JSON.stringify(unmatched.toasts));
    assert.strictEqual(ids.btn_export_xml.disabled, false);

    // Old response shape without stats still works.
    const legacy = await run(base);
    assert.ok(legacy.log.includes("Timeline built:"), legacy.log);
    assert.ok(!legacy.log.includes("сопоставлено"), legacy.log);
  } finally {
    globalThis.fetch = originalFetch;
    ids.log_box.textContent = originalLog;
    toastContainer.children = originalChildren;
    appState.timelineReady = originalReady;
  }
}

// Matcher verification reasons (docs/SYNC_MATCH_VERIFICATION.md): every new
// review_reason / plausibility flag must read as a Russian explanation on the card.
async function testSyncMatchVerificationReasons({ state, sync }) {
  const { ids, appState } = state;
  const originalFetch = globalThis.fetch;
  const originalSyncFeatureAvailable = appState.syncFeatureAvailable;
  const originalSyncSources = appState.syncSources;
  const render = async (metadata) => {
    globalThis.fetch = async (url) => {
      const target = String(url);
      const body = target.includes("/sync-matches")
        ? { items: [{
            segment_id: "snh9", status: "needs_review", speaker: "ИВАН", source_id: "media_a1",
            source_in_sec: 1, source_out_sec: 4, metadata,
          }] }
        : { items: [] };
      if (target.includes("/sync-jobs/status")) return { ok: false, status: 404, text: async () => "" };
      return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, result: body }) };
    };
    await sync.loadSyncWorkspace(true);
    return ids.sync_review_list.innerHTML;
  };
  const expected = {
    local_index_fallback: "Найдено только в локальной расшифровке — проверьте",
    implausible_word_rate: "Слишком много слов за длительность фрагмента",
    collapsed_word_times: "Таймкоды слов схлопнуты — границы недостоверны",
    tail_truncated_vs_local: "По локальной расшифровке обрезан конец",
    head_truncated_vs_local: "По локальной расшифровке обрезано начало",
    range_disagrees_with_local: "Диапазон расходится с локальной расшифровкой",
    local_text_mismatch: "На этом месте локальная расшифровка содержит другой текст",
  };
  try {
    for (const [reason, text] of Object.entries(expected)) {
      const html = await render({ review_reason: reason });
      assert.ok(html.includes(text), `${reason}: ${html}`);
    }
    // plausibility_flags rendered as a list; review_reason is not duplicated.
    const html = await render({
      review_reason: "tail_truncated_vs_local",
      plausibility_flags: ["tail_truncated_vs_local", "range_disagrees_with_local"],
    });
    assert.strictEqual(html.split(expected.tail_truncated_vs_local).length - 1, 1, html);
    assert.ok(html.includes(expected.range_disagrees_with_local), html);
    // Unknown values and markup never reach the DOM raw.
    const unknown = await render({ review_reason: "<b>x</b>", plausibility_flags: ["<i>y</i>"] });
    assert.ok(!unknown.includes("<b>x</b>") && !unknown.includes("<i>y</i>"), unknown);
  } finally {
    globalThis.fetch = originalFetch;
    appState.syncFeatureAvailable = originalSyncFeatureAvailable;
    appState.syncSources = originalSyncSources;
  }
}

// Sync ASR failure reporting: a completed job with complete === false reads as
// "готово с ошибками" (not plain "готово"), and an asr_failed match is labelled as a
// recognition failure, not as "no recording".
async function testSyncJobIncompleteAndAsrFailedLabel({ state, sync }) {
  const { ids, appState } = state;
  const originalFetch = globalThis.fetch;
  const originalSyncFeatureAvailable = appState.syncFeatureAvailable;
  const originalSyncSources = appState.syncSources;
  const load = async (job, matchMetadata) => {
    globalThis.fetch = async (url) => {
      const target = String(url);
      let body = { items: [] };
      if (target.includes("/sync-matches")) {
        body = { items: [{
          segment_id: "asr1", status: "not_found", speaker: "ИВАН",
          metadata: matchMetadata || {},
        }] };
      } else if (target.includes("/sync-jobs/status")) {
        body = job;
      }
      return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, result: body }) };
    };
    await sync.loadSyncWorkspace(true);
  };
  try {
    await load({ id: "j1", status: "completed", complete: true, message: "Completed" });
    assert.strictEqual(ids.sync_job_status.textContent, "готово");
    await load({
      id: "j1", status: "completed", complete: false, degraded_sources: ["a"],
      message: "Completed with errors: ASR failed for 1 source(s)",
    });
    assert.strictEqual(ids.sync_job_status.textContent, "готово с ошибками");
    assert.ok(ids.sync_job_message.textContent.includes("ASR failed for 1 source(s)"), ids.sync_job_message.textContent);

    await load(
      { id: "j2", status: "completed", complete: true, message: "Completed" },
      { unmatched_reason: "asr_failed", asr_failed_source_ids: ["a"] },
    );
    const html = ids.sync_review_list.innerHTML;
    assert.ok(html.includes("Распознавание записи спикера не удалось"), html);
    assert.ok(!html.includes("Нет записи"), html);
    await load(
      { id: "j2", status: "completed", complete: true, message: "Completed" },
      { unmatched_reason: "no_eligible_source" },
    );
    assert.ok(ids.sync_review_list.innerHTML.includes("Нет записи"), ids.sync_review_list.innerHTML);
  } finally {
    globalThis.fetch = originalFetch;
    appState.syncFeatureAvailable = originalSyncFeatureAvailable;
    appState.syncSources = originalSyncSources;
  }
}

// Stage 3 prepare result: the UI shows the total placeholder count, duration and reasons,
// not just the narrow unmapped-source swap counter.
function testStage3PlaceholderDescription({ stage3 }) {
  assert.strictEqual(stage3.describeStage3Placeholders({}), "плейсхолдеров: 0");
  const text = stage3.describeStage3Placeholders({
    placeholder_segments: 84,
    placeholder_duration_sec: 312.46,
    placeholder_breakdown: {
      sync_needs_review: { segments: 50, duration_sec: 200 },
      sync_not_found: { segments: 26, duration_sec: 100 },
      life: { segments: 8, duration_sec: 12.46 },
    },
    unmapped_source_placeholders: 0,
  });
  assert.ok(text.startsWith("плейсхолдеров: 84 (312.5 с)"), text);
  assert.ok(text.includes("синхрон: проверить: 50 / 200.0 с"), text);
  assert.ok(text.includes("синхрон: не найден: 26"), text);
  assert.ok(text.includes("LIFE: 8 / 12.5 с"), text);
}

// Audit item 4: the editor sees fragment counts and build warnings BEFORE export;
// unresolved fragments need an explicit confirmation, cancel aborts the export.
async function testExportXmlPreExportSummary({ state, stage3 }) {
  const { ids, appState } = state;
  const originalFetch = globalThis.fetch;
  const originalLog = ids.log_box.textContent;
  const originalWarnings = appState.lastBuildWarningCount;
  const originalRunning = appState.stage3JobRunning;
  const calls = [];
  let summary;
  let statusFails = false;
  globalThis.fetch = async (url) => {
    const target = String(url);
    calls.push(target);
    if (target.includes("/stage1/status")) {
      if (statusFails) return { ok: false, status: 500, json: async () => ({ ok: false, error: "status-down" }) };
      return { ok: true, status: 200, json: async () => ({ ok: true, result: { timeline_sync_summary: summary } }) };
    }
    // Stop the flow at job start; this test only covers the pre-export gate.
    return { ok: false, status: 500, json: async () => ({ ok: false, error: "stop-after-start" }) };
  };
  const started = () => calls.some((url) => url.includes("/stage3/jobs/prepare/start"));
  const settle = () => new Promise((resolve) => setImmediate(resolve));
  const run = async (decision) => {
    calls.length = 0;
    ids.log_box.textContent = "";
    ids.confirm_dialog_message.textContent = "";
    const promise = stage3.exportTimelineXml().catch((err) => err);
    for (let i = 0; i < 20 && !ids.confirm_dialog_message.textContent && !started(); i += 1) await settle();
    const message = ids.confirm_dialog_message.textContent;
    if (message && decision) {
      const button = decision === "accept" ? ids.btn_confirm_dialog_accept : ids.btn_confirm_dialog_cancel;
      button.onclick();
    }
    return { result: await promise, message, log: ids.log_box.textContent };
  };
  try {
    appState.lastBuildWarningCount = 4;
    summary = { total: 8, auto_matched: 3, confirmed: 2, needs_review: 2, not_found: 1 };

    const cancelled = await run("cancel");
    assert.ok(
      cancelled.message.includes(
        "Экспортировать черновик: 2 фрагментов требуют проверки и войдут заглушками, 1 не найдено?",
      ),
      cancelled.message,
    );
    assert.ok(/Автосопоставлено: 3/.test(cancelled.message) && /подтверждено: 2/.test(cancelled.message), cancelled.message);
    assert.ok(cancelled.message.includes("предупреждений сборки: 4"), cancelled.message);
    assert.ok(!started(), "cancel must abort before the export job starts");
    assert.strictEqual(cancelled.result, undefined);
    assert.ok(/Export XML.*needs_review=2.*not_found=1/.test(cancelled.log), cancelled.log);
    assert.strictEqual(appState.stage3JobRunning, false);

    const accepted = await run("accept");
    assert.ok(started(), "accepted export must start the job");
    assert.ok(accepted.result instanceof Error && /stop-after-start/.test(accepted.result.message));

    // Nothing unresolved: no dialog, export proceeds, summary still logged.
    summary = { total: 5, auto_matched: 3, confirmed: 2, needs_review: 0, not_found: 0 };
    const clean = await run(null);
    assert.strictEqual(clean.message, "", "clean timeline must not ask for confirmation");
    assert.ok(started());
    assert.ok(/auto_matched=3/.test(clean.log), clean.log);

    // Old backend without the field: export is not blocked.
    summary = undefined;
    const legacy = await run(null);
    assert.strictEqual(legacy.message, "");
    assert.ok(started());

    // Summary request failed: no silent export, the editor decides; cancel aborts.
    statusFails = true;
    const failedCancel = await run("cancel");
    assert.ok(
      failedCancel.message.includes("Не удалось получить сводку сопоставления. Экспортировать без проверки?"),
      failedCancel.message,
    );
    assert.ok(!started(), "cancel must abort the export when the summary is unavailable");
    assert.strictEqual(failedCancel.result, undefined);
    assert.ok(/status-down/.test(failedCancel.log), failedCancel.log);
    const failedAccept = await run("accept");
    assert.ok(started(), "accepted export without a summary must start the job");
    assert.ok(failedAccept.result instanceof Error && /stop-after-start/.test(failedAccept.result.message));
    statusFails = false;
  } finally {
    globalThis.fetch = originalFetch;
    ids.log_box.textContent = originalLog;
    appState.lastBuildWarningCount = originalWarnings;
    appState.stage3JobRunning = originalRunning;
  }
}

// Audit U2/U3: edits typed while a save/preview/confirm request is in flight must
// survive the response; the server revision is still accepted.
function normalizationRaceHarness({ state }) {
  const { appState } = state;
  const originals = {
    fetch: globalThis.fetch,
    normalization: appState.normalization,
    dirty: appState.normalizationDirty,
    eligible: appState.normalizationEligible,
  };
  const jsonResponse = (body) => ({ ok: true, status: 200, text: async () => JSON.stringify(body), json: async () => body });
  const deferred = () => {
    let resolve;
    const promise = new Promise((res) => { resolve = res; });
    return { promise, resolve };
  };
  const settle = async () => { for (let i = 0; i < 10; i += 1) await new Promise((r) => setImmediate(r)); };
  const doc = (text, revision, status = "draft") => ({
    revision, status, warnings: [],
    blocks: [{ id: "b1", order: 1, type: "voiceover", text_for_tts: text, normalized_text: text }],
  });
  const reset = (text, revision, { status = "draft", dirty = false } = {}) => {
    appState.normalization = doc(text, revision, status);
    appState.normalizationDirty = dirty;
    appState.normalizationEligible = false;
  };
  const restore = () => {
    globalThis.fetch = originals.fetch;
    appState.normalization = originals.normalization;
    appState.normalizationDirty = originals.dirty;
    appState.normalizationEligible = originals.eligible;
  };
  return { appState, jsonResponse, deferred, settle, reset, restore, doc };
}

async function testNormalizationEditDuringConfirm({ state, normalization }) {
  const h = normalizationRaceHarness({ state });
  const { appState } = h;
  const userEdit = (text) => {
    appState.normalization.blocks[0].text_for_tts = text;
    normalization.markNormalizationDirty();
  };
  // gate: "preview" or "confirm" request is held until released.
  const run = async (gate) => {
    const held = h.deferred();
    const calls = [];
    globalThis.fetch = async (url, options = {}) => {
      const target = String(url);
      const body = options.body ? JSON.parse(options.body) : null;
      if (target.includes("/normalization/confirm/preview")) {
        calls.push("preview");
        if (gate === "preview") await held.promise;
        return h.jsonResponse({ ok: true, result: { candidates: [] } });
      }
      if (target.includes("/normalization/confirm")) {
        calls.push("confirm");
        assert.strictEqual(body.revision, 5, "confirm must be sent with the revision it started from");
        if (gate === "confirm") await held.promise;
        return h.jsonResponse({ ok: true, result: h.doc("original", 6, "confirmed") });
      }
      if (target.includes("/normalization/memory")) return h.jsonResponse({ ok: true, result: [] });
      if (target.includes("/stage1/status")) {
        return h.jsonResponse({ ok: true, result: { normalization_confirmed: true, normalization_status: "confirmed" } });
      }
      throw new Error(`testNormalizationEditDuringConfirm: unexpected fetch ${target}`);
    };
    h.reset("original", 5);
    const done = normalization.confirmNormalization();
    await h.settle();
    assert.ok(calls.includes(gate), `${gate} request must be in flight`);
    userEdit("new edit during confirm");
    held.resolve();
    await done;
  };
  try {
    for (const gate of ["preview", "confirm"]) {
      await run(gate);
      const doc = appState.normalization;
      assert.strictEqual(doc.blocks[0].text_for_tts, "new edit during confirm", `${gate}: newer local text must survive`);
      assert.strictEqual(appState.normalizationDirty, true, `${gate}: draft must stay dirty`);
      assert.strictEqual(appState.normalizationEligible, false, `${gate}: edited text is not confirmed-eligible`);
      assert.notStrictEqual(doc.status, "confirmed", `${gate}: edited text must not read as confirmed`);
      assert.strictEqual(doc.revision, 6, `${gate}: server revision must be accepted`);
    }

    // Control: no edit during the request -> confirmed and clean.
    const calls = [];
    globalThis.fetch = async (url) => {
      const target = String(url);
      calls.push(target);
      if (target.includes("/normalization/confirm/preview")) return h.jsonResponse({ ok: true, result: { candidates: [] } });
      if (target.includes("/normalization/confirm")) return h.jsonResponse({ ok: true, result: h.doc("original", 6, "confirmed") });
      if (target.includes("/normalization/memory")) return h.jsonResponse({ ok: true, result: [] });
      return h.jsonResponse({ ok: true, result: { normalization_confirmed: true, normalization_status: "confirmed" } });
    };
    h.reset("original", 5);
    await normalization.confirmNormalization();
    assert.strictEqual(appState.normalization.status, "confirmed");
    assert.strictEqual(appState.normalizationDirty, false);
    assert.strictEqual(appState.normalizationEligible, true);
    assert.strictEqual(appState.normalization.revision, 6);
  } finally {
    h.restore();
  }
}

async function testNormalizationSaveAcceptsRevisionAfterEdit({ state, normalization }) {
  const h = normalizationRaceHarness({ state });
  const { appState } = h;
  let serverRevision = 5;
  const puts = [];
  const gates = [];
  globalThis.fetch = async (url, options = {}) => {
    const target = String(url);
    if (!(target.includes("/stage1/normalization") && options.method === "PUT")) {
      throw new Error(`testNormalizationSaveAcceptsRevisionAfterEdit: unexpected fetch ${target}`);
    }
    const body = JSON.parse(options.body);
    puts.push({ revision: body.revision, text: body.blocks[0].text_for_tts });
    if (gates.length) await gates.shift().promise;
    if (body.revision !== serverRevision) {
      return { ok: false, status: 409, text: async () => JSON.stringify({ ok: false, error: `Expected revision ${body.revision}, current revision is ${serverRevision}` }) };
    }
    serverRevision += 1;
    return h.jsonResponse({ ok: true, result: { ...body, revision: serverRevision } });
  };
  try {
    h.reset("v1", 5, { dirty: true });
    const gate = h.deferred();
    gates.push(gate);
    const first = normalization.saveNormalization();
    await h.settle();
    appState.normalization.blocks[0].text_for_tts = "v2 typed during save";
    normalization.markNormalizationDirty();
    gate.resolve();
    await first;
    assert.strictEqual(appState.normalization.revision, 6, "server revision from the first PUT must be accepted");
    assert.strictEqual(appState.normalization.blocks[0].text_for_tts, "v2 typed during save");
    assert.strictEqual(appState.normalizationDirty, true, "newer edit stays unsaved");

    await normalization.saveNormalization();
    assert.deepStrictEqual(puts.map((p) => p.revision), [5, 6]);
    assert.strictEqual(puts[1].text, "v2 typed during save");
    assert.strictEqual(appState.normalization.revision, 7);
    assert.strictEqual(appState.normalization.blocks[0].text_for_tts, "v2 typed during save");
    assert.strictEqual(appState.normalizationDirty, false);
  } finally {
    h.restore();
  }
}

function testApplyStatusSyncsTimelineReady({ state, normalization }) {
  const { ids, appState } = state;
  const originals = {
    ready: appState.timelineReady,
    normalization: appState.normalization,
    eligible: appState.normalizationEligible,
    dirty: appState.normalizationDirty,
    running: appState.stage3JobRunning,
  };
  const confirmedStatus = { normalization_confirmed: true, normalization_status: "confirmed" };
  try {
    appState.normalization = { status: "confirmed", revision: 3, warnings: [], blocks: [{ id: "b1", type: "voiceover" }] };
    appState.normalizationDirty = false;
    appState.normalizationEligible = true;
    appState.stage3JobRunning = false;
    appState.timelineReady = true;

    normalization.applyStatus({ ...confirmedStatus, timeline_ready: false });
    assert.strictEqual(appState.timelineReady, false, "true -> false transition must be applied");
    assert.strictEqual(ids.btn_export_xml.disabled, true);

    normalization.applyStatus({ ...confirmedStatus, timeline_ready: true });
    assert.strictEqual(appState.timelineReady, true, "false -> true transition must be applied");
    assert.strictEqual(ids.btn_export_xml.disabled, false);

    // Parallel Stage 3 job keeps export disabled even when the timeline is ready.
    appState.stage3JobRunning = true;
    normalization.applyStatus({ ...confirmedStatus, timeline_ready: true });
    assert.strictEqual(ids.btn_export_xml.disabled, true);
    appState.stage3JobRunning = false;

    // Missing / non-boolean timeline_ready leaves the state untouched.
    normalization.applyStatus({ ...confirmedStatus });
    normalization.applyStatus({ ...confirmedStatus, timeline_ready: "no" });
    assert.strictEqual(appState.timelineReady, true);
    assert.strictEqual(ids.btn_export_xml.disabled, false);
  } finally {
    appState.timelineReady = originals.ready;
    appState.normalization = originals.normalization;
    appState.normalizationEligible = originals.eligible;
    appState.normalizationDirty = originals.dirty;
    appState.stage3JobRunning = originals.running;
    normalization.applyStatus({});
  }
}

function testNormalizationWarningLocations({ state, normalization }) {
  const previous = state.appState.normalization;
  try {
    state.appState.normalization = { status: "draft", blocks: [
      { uid: "a", order: 6, type: "unknown", warnings: ["LLM returned an unknown type"] },
      { uid: "b", order: 94, type: "unknown", warnings: ["LLM returned an unknown type"] },
      ...Array.from({ length: 9 }, (_, i) => ({
        uid: `extra-${i}`, order: 100 + i, type: "technical", warnings: [`Additional warning ${i}`],
      })),
    ] };
    normalization.renderNormalization();
    const html = state.ids.normalization_warnings.innerHTML;
    assert.ok(html.includes("Не удалось определить тип блока"), html);
    assert.ok(html.includes("6, 94"), "warning must locate affected blocks");
    assert.ok(html.includes("Additional warning 8"), "last warning must remain visible");
    assert.ok(!html.includes("LLM returned an unknown type"), "known warnings must be translated");
  } finally {
    state.appState.normalization = previous;
  }
}

async function testNormalizationWarningReview({ state, normalization }) {
  const { appState, ids } = state;
  const h = normalizationRaceHarness({ state });
  const originalFetch = globalThis.fetch;
  const originalPath = ids.normalization_input_path.value;
  try {
    const headingWarning = "all-caps section heading treated as technical (not spoken): check manually";
    const transitionWarning = "leading '+' splice continues previous block: check manually";
    h.reset("original", 8);
    appState.normalization.blocks = [
      { uid: "a", order: 1, type: "technical", original_text: "ЛИНИЯ НИКИТЫ", warnings: [headingWarning, transitionWarning] },
      { uid: "b", order: 2, type: "unknown", warnings: ["LLM returned an unknown type"] },
    ];
    normalization.renderNormalization();
    const clickReview = (index, warningIndex) => {
      const card = { dataset: { index: String(index) } };
      const button = { dataset: { action: "review-warning", warningIndex: String(warningIndex) }, closest: () => card };
      ids.normalization_blocks.dispatch("click", { target: { closest: () => button } });
    };
    clickReview(0, 0);
    assert.deepStrictEqual(appState.normalization.blocks[0].warnings, [transitionWarning], "review acknowledges only the selected warning");
    assert.strictEqual(appState.normalization.blocks[0].original_text, "ЛИНИЯ НИКИТЫ");
    assert.strictEqual(appState.normalizationDirty, true);
    clickReview(1, 0);
    assert.strictEqual(appState.normalization.blocks[1].warnings.length, 1, "unknown type still needs a decision");
    clickReview(0, 0);
    assert.ok(!ids.normalization_warnings.innerHTML.includes("ЛИНИЯ"));
    assert.ok(ids.norm_issue_position.textContent.includes("1 из 1"), "reviewed block leaves the review queue, unknown block stays");

    const calls = [];
    ids.normalization_input_path.value = "/app/scenario.txt";
    globalThis.fetch = async (url, options = {}) => {
      calls.push({ url: String(url), method: options.method, body: JSON.parse(options.body || "{}") });
      if (options.method === "PUT") return h.jsonResponse({ ok: true, result: { ...appState.normalization, revision: 9 } });
      return h.jsonResponse({ ok: true, result: { id: "job", status: "completed", result: { ...appState.normalization, revision: 10 } } });
    };
    await normalization.generateNormalization();
    assert.strictEqual(calls[0].method, "PUT", "save review decisions before regenerating");
    assert.deepStrictEqual(calls[0].body.blocks[0].warnings, []);
    assert.ok(calls[1].url.endsWith("/normalization-jobs/start"));
    assert.strictEqual(appState.normalization.revision, 10);
    assert.strictEqual(appState.normalizationDirty, false);
  } finally {
    globalThis.fetch = originalFetch;
    ids.normalization_input_path.value = originalPath;
    h.restore();
  }
}

async function testNormalizationEditDuringGenerate({ state, normalization }) {
  const h = normalizationRaceHarness({ state });
  const originalPath = state.ids.normalization_input_path.value;
  try {
    h.reset("original", 8);
    state.ids.normalization_input_path.value = "/app/scenario.txt";
    const response = h.deferred();
    globalThis.fetch = async () => response.promise;
    const done = normalization.generateNormalization();
    await h.settle();
    state.appState.normalization.blocks[0].text_for_tts = "new text during generation";
    normalization.markNormalizationDirty();
    response.resolve(h.jsonResponse({ ok: true, result: { id: "job", status: "completed", result: h.doc("original", 9) } }));
    await done;
    assert.strictEqual(state.appState.normalization.revision, 9);
    assert.strictEqual(state.appState.normalization.blocks[0].text_for_tts, "new text during generation");
    assert.strictEqual(state.appState.normalizationDirty, true);
  } finally {
    state.ids.normalization_input_path.value = originalPath;
    h.restore();
  }
}

function testTypeDecisionKeepsUnrelatedWarnings({ state, normalization }) {
  const h = normalizationRaceHarness({ state });
  try {
    h.reset("original", 1);
    const timeWarning = "Ambiguous timecode '00:30.00' read as 00:30:00: check manually";
    state.appState.normalization.blocks = [{
      uid: "a", order: 1, type: "unknown", normalized_text: "Рассказ героя.",
      warnings: ["Ambiguous label combination 'ЗК СНХ': check the block type manually", timeWarning],
    }];
    const card = { dataset: { index: "0" }, querySelector: () => null };
    normalization.updateBlockFromControl({ dataset: { field: "type" }, value: "sync", closest: () => card });
    assert.strictEqual(state.appState.normalization.blocks[0].type, "sync");
    assert.deepStrictEqual(state.appState.normalization.blocks[0].warnings, [timeWarning], "choosing a type does not confirm a timecode");
  } finally {
    h.restore();
  }
}

const cyrillic = /[а-яё]/i;
const cardsOf = (ids) => ids.normalization_blocks.children.filter((item) => item.dataset && item.dataset.uid !== undefined);
const cardFor = (ids, uid) => cardsOf(ids).filter((item) => item.dataset.uid === uid).pop();
const issueOf = (code, order, message, severity = "error", uid = "a") => ({ code, uid, order, message, severity });

// Plan section 6: preview summary before confirm, blocking issues never reach /confirm.
async function testConfirmPreviewSummaryAndBlocking({ state, normalization }) {
  const { ids, appState } = state;
  const h = normalizationRaceHarness({ state });
  globalThis.requestAnimationFrame = globalThis.requestAnimationFrame || ((fn) => fn());
  try {
    const calls = [];
    let validation = [
      issueOf("service_label_in_tts", 1, "Block 1 has a service label 'ЗК № 3' in its TTS text"),
      issueOf("scene_opener_changed", 2, "scene opener changed: check reply type", "warning", "b"),
    ];
    globalThis.fetch = async (url) => {
      const target = String(url);
      calls.push(target);
      if (target.includes("/normalization/confirm/preview")) {
        return h.jsonResponse({ ok: true, result: { candidates: [], validation, tts_preview: [{ uid: "a", order: 1, text_for_tts: "x" }] } });
      }
      if (target.includes("/normalization/confirm")) return h.jsonResponse({ ok: true, result: h.doc("ok", 6, "confirmed") });
      if (target.includes("/normalization/memory")) return h.jsonResponse({ ok: true, result: [] });
      return h.jsonResponse({ ok: true, result: { normalization_confirmed: true, normalization_status: "confirmed" } });
    };
    h.reset("original", 5);
    appState.normalization.blocks = [
      { uid: "a", order: 1, type: "voiceover", normalized_text: "ЗК № 3 текст", text_for_tts: "ЗК № 3 текст" },
      { uid: "b", order: 2, type: "life", normalized_text: "Лайф" },
    ];
    ids.normalization_blocks.children = [];
    await assert.rejects(normalization.confirmNormalization(), /Подтверждение не отправлено/);
    assert.ok(!calls.some((url) => url.endsWith("/normalization/confirm")), "blocking issue must not send confirm");
    const html = ids.normalization_confirm_summary.innerHTML;
    assert.strictEqual(ids.normalization_confirm_summary.hidden, false);
    assert.strictEqual(ids.normalization_confirm_summary.dataset.state, "blocked");
    assert.ok(html.includes("В озвучку пойдёт блоков: <b>1</b>"), html);
    assert.ok(html.includes("Блокирующих ошибок: <b>1</b>"), html);
    assert.ok(html.includes("служебная метка"), "reason must be in Russian");
    assert.ok(html.includes('data-goto-uid="a"') && html.includes("Предупреждения (1)"), html);
    assert.ok(html.includes("Начало сцены изменилось"), html);
    assert.ok(ids.norm_issue_position.textContent.includes("из"), "blocked block joins the review queue");

    // Navigation to the reason's block highlights it like the issue navigation does.
    ids.normalization_blocks.children = [];
    ids.normalization_confirm_summary.dispatch("click", { target: { closest: () => ({ dataset: { gotoUid: "b" } }) } });
    assert.ok(cardFor(ids, "b").className.includes("is-active-issue"), "goto highlights the block");
    assert.ok(!cardFor(ids, "a").className.includes("is-active-issue"));

    // Only warnings: summary is shown and confirm proceeds.
    validation = [issueOf("scene_opener_changed", 2, "scene opener changed: check reply type", "warning", "b")];
    h.reset("original", 5);
    calls.length = 0;
    await normalization.confirmNormalization();
    assert.ok(calls.some((url) => url.endsWith("/normalization/confirm")), "no blocking issues: confirm is sent");
    assert.strictEqual(ids.normalization_confirm_summary.dataset.state, "ok");
    assert.strictEqual(appState.normalization.status, "confirmed");
  } finally {
    h.restore();
  }
}

// A rejected confirm (422) shows structured reasons; new codes never fall back to raw text.
async function testConfirmRejectedShowsRussianReasons({ state, normalization }) {
  const { ids, appState } = state;
  const h = normalizationRaceHarness({ state });
  try {
    const messages = [
      ["service_label_in_tts", "Block 1 has a service label 'ЗК № 3' in its TTS text"],
      ["timecode_in_role", "Block 1 has a timecode fragment in role: 'Герой (00:1'"],
      ["timecode_remnant_in_text", "Block 1 normalized_text starts with a timecode remnant: ') слово'"],
      ["false_hero_from_cue", "Block 1 speaker 'Титры' is a technical cue, not a person"],
      ["lost_splice", "Block 1 lost the explicit '+' splice: no fragment starts at 00:10:05"],
      ["unmatched_speech", "Block 1 lost 5 of 9 spoken words (e.g. 'слово')"],
      ["unknown_type", "Block 1 has unknown type"],
      ["sync_no_speaker", "Sync block 1 has no speaker"],
    ];
    const issues = messages.map(([code, message]) => issueOf(code, 1, message));
    for (const issue of issues) {
      const text = normalization.describeIssue(issue);
      assert.notStrictEqual(text, issue.message, `${issue.code}: raw fallback`);
      assert.ok(cyrillic.test(text) && !/Block \d/.test(text), `${issue.code}: ${text}`);
    }
    assert.ok(normalization.describeIssue(issues[1]).includes("Роль / должность"));
    assert.ok(normalization.describeIssue(issues[4]).includes("00:10:05"));
    assert.ok(normalization.describeIssue(issues[5]).includes("5 из 9"));

    globalThis.fetch = async (url) => {
      const target = String(url);
      if (target.includes("/normalization/confirm/preview")) return h.jsonResponse({ ok: true, result: { candidates: [] } });
      if (target.includes("/normalization/confirm")) {
        return { ok: false, status: 422, text: async () => JSON.stringify({
          ok: false, error: "normalization_validation_failed", message: "Block 1 has a service label", issues, codes: [] }) };
      }
      throw new Error(`unexpected fetch ${target}`);
    };
    h.reset("original", 5);
    appState.normalization.blocks[0].uid = "a";
    await assert.rejects(normalization.confirmNormalization(), /Сервер отклонил подтверждение: ошибок 8/);
    const html = ids.normalization_confirm_summary.innerHTML;
    assert.ok(html.includes("сервер отклонил подтверждение") && html.includes('data-goto-uid="a"'), html);
    assert.ok(html.includes("служебная метка") && html.includes("Потерян фрагмент склейки"), html);
    assert.ok(!html.includes("has a service label"), "server English message must not be shown");
    assert.strictEqual(appState.normalization.status, "draft", "rejected confirm leaves the draft unconfirmed");

    // Warnings without a dedicated hint are still translated (no `return raw` fallback).
    appState.normalization.blocks = [{ uid: "w", order: 1, type: "life", warnings: [
      "scene opener changed: check reply type",
      "long dash paragraph joined to the LIFE scene as a reply: check manually",
      "reply joined to the previous SYNC scene; speaker is not stated: check manually",
      "leading '+' splice has no previous LIFE/SYNC block to continue: check manually",
    ] }];
    normalization.renderNormalization();
    const warningHtml = ids.normalization_warnings.innerHTML;
    assert.ok(warningHtml.includes("Начало сцены изменилось") && warningHtml.includes("Длинный абзац с тире"), warningHtml);
    assert.ok(warningHtml.includes("говорящий не указан") && warningHtml.includes("склейку «+»"), warningHtml);
    assert.ok(!/check manually|check reply type/.test(warningHtml), warningHtml);
  } finally {
    h.restore();
  }
}

function testSyncFragmentEditing({ state, normalization }) {
  const { ids, appState } = state;
  const h = normalizationRaceHarness({ state });
  try {
    h.reset("original", 1);
    appState.normalization.blocks = [
      { uid: "op", order: 1, type: "sync", speaker: "Орлов", normalized_text: "Один два три", timecode_start: "00:00:10", timecode_end: "00:00:20",
        sync_fragments: [{ text: "Один два три", timecode_start: "00:00:10", timecode_end: "00:00:20" }], warnings: [] },
      { uid: "re", order: 2, type: "life", scene_uid: "op", normalized_text: "Реплика" },
    ];
    ids.normalization_blocks.children = [];
    normalization.renderNormalization();
    assert.ok(cardFor(ids, "op").innerHTML.includes('data-action="add-fragment"'), "sync block offers adding a fragment");
    const replyHtml = cardFor(ids, "re").innerHTML;
    assert.ok(replyHtml.includes("Сцена: блок #1 (") && replyHtml.includes("Орлов"), replyHtml);
    assert.ok(replyHtml.includes('data-action="goto-scene"') && replyHtml.includes('data-goto-uid="op"'), replyHtml);
    assert.ok(!cardFor(ids, "op").innerHTML.includes("Сцена:"), "scene opener has no scene label");

    const block = () => appState.normalization.blocks[0];
    const click = (action, { fragment, cursor } = {}) => {
      const card = { dataset: { index: "0" } };
      const row = fragment == null ? null : {
        dataset: { fragmentIndex: String(fragment) },
        querySelector: () => ({ selectionStart: cursor }),
      };
      const button = { dataset: { action }, closest: (selector) => (selector === ".normalization-block" ? card : row) };
      ids.normalization_blocks.dispatch("click", { target: { closest: () => button } });
    };
    appState.normalizationDirty = false;
    click("add-fragment");
    assert.strictEqual(block().sync_fragments.length, 2);
    assert.deepStrictEqual(block().sync_fragments[1], { text: "", timecode_start: "", timecode_end: "" });
    assert.strictEqual(appState.normalizationDirty, true, "adding a fragment marks the draft changed");

    block().sync_fragments[1].text = "Четыре";
    appState.normalizationDirty = false;
    click("split-fragment", { fragment: 0, cursor: 4 });
    assert.deepStrictEqual(block().sync_fragments.map((f) => f.text), ["Один", "два три", "Четыре"]);
    assert.strictEqual(block().sync_fragments[0].timecode_start, "00:00:10");
    assert.strictEqual(block().sync_fragments[1].timecode_start, "", "tail awaits its own timecode");
    assert.strictEqual(block().normalized_text, "Один два три Четыре");
    assert.strictEqual(appState.normalizationDirty, true);

    appState.normalizationDirty = false;
    click("split-fragment", { fragment: 0, cursor: 0 });
    click("split-fragment", { fragment: 0, cursor: 4 });
    assert.strictEqual(block().sync_fragments.length, 3, "cursor at an edge does not split");
    assert.strictEqual(appState.normalizationDirty, false);

    click("delete-fragment", { fragment: 1 });
    assert.deepStrictEqual(block().sync_fragments.map((f) => f.text), ["Один", "Четыре"]);
    click("delete-fragment", { fragment: 1 });
    assert.strictEqual(block().sync_fragments.length, 1);
    click("delete-fragment", { fragment: 0 });
    assert.strictEqual(block().sync_fragments.length, 1, "the last fragment cannot be deleted");
    assert.strictEqual(block().normalized_text, "Один");
    assert.ok(cardFor(ids, "op").innerHTML.includes('data-action="add-fragment"'));

    // Scene jump from the reply card.
    const jump = { dataset: { action: "goto-scene", gotoUid: "op" }, closest: () => ({ dataset: { index: "1" } }) };
    ids.normalization_blocks.children = [];
    ids.normalization_blocks.dispatch("click", { target: { closest: () => jump } });
    assert.ok(cardFor(ids, "op").className.includes("is-active-issue"));
  } finally {
    h.restore();
  }
}

function testGroupReviewOnlySelectedBlocks({ state, normalization }) {
  const { ids, appState } = state;
  const h = normalizationRaceHarness({ state });
  try {
    h.reset("original", 1);
    const warn = () => ["leading '+' splice continues previous block: check manually"];
    appState.normalization.blocks = [
      { uid: "a", order: 1, type: "life", warnings: warn() },
      { uid: "b", order: 2, type: "life", warnings: warn() },
      { uid: "c", order: 3, type: "unknown", warnings: ["LLM returned an unknown type"] },
    ];
    normalization.renderNormalization();
    assert.strictEqual(ids.btn_norm_group_review.disabled, true, "no selection: group action unavailable");
    appState.normalizationDirty = false;
    ids.btn_norm_group_review.dispatch("click");
    assert.strictEqual(appState.normalization.blocks[0].warnings.length, 1, "nothing selected: nothing changes");
    assert.strictEqual(appState.normalizationDirty, false);

    const tick = (uid, checked) => ids.normalization_blocks.dispatch("change", {
      target: { dataset: { selectBlock: "" }, checked, closest: () => ({ dataset: { uid } }) },
    });
    tick("a", true);
    tick("c", true);
    assert.strictEqual(ids.norm_selected_count.textContent, "2");
    assert.strictEqual(ids.btn_norm_group_review.disabled, false);
    tick("c", false);
    tick("c", true);
    ids.btn_norm_group_review.dispatch("click");
    const [a, b, c] = appState.normalization.blocks;
    assert.deepStrictEqual(a.warnings, [], "selected block is reviewed");
    assert.strictEqual(b.warnings.length, 1, "unselected block is untouched");
    assert.strictEqual(c.warnings.length, 1, "unknown type still needs a decision");
    assert.strictEqual(appState.normalizationDirty, true);
    assert.strictEqual(ids.norm_selected_count.textContent, "0", "selection is cleared after the action");
    assert.strictEqual(ids.btn_norm_group_review.disabled, true);
  } finally {
    h.restore();
  }
}

async function testNormalizationHistoryDiffRestore({ state, normalization }) {
  const h = normalizationRaceHarness({ state });
  const { ids, appState } = state;
  const name = "20260101T120000000000Z_rev3.json";
  const calls = [];
  const confirmNow = () => ids.btn_confirm_dialog_accept.onclick();
  const install = ({ restoreGate = null } = {}) => {
    calls.length = 0;
    globalThis.fetch = async (url, options = {}) => {
      const target = String(url);
      const method = options.method || "GET";
      const body = options.body ? JSON.parse(options.body) : null;
      calls.push({ method, target, body });
      if (target.includes("/normalization/history/") && target.includes("/diff")) {
        return h.jsonResponse({
          ok: true,
          result: {
            summary: { unchanged: 1, edited: 1 },
            snapshot: { revision: 3, status: "confirmed", blocks: 2 },
            current: { revision: 5, status: "draft", blocks: 2 },
            changes: [
              { status: "unchanged", old_index: 0, new_index: 0, old_type: "voiceover", new_type: "voiceover", old_speaker: "", new_speaker: "", old_text: "same", new_text: "same" },
              { status: "edited", old_index: 1, new_index: 1, old_type: "sync", new_type: "sync", old_speaker: "", new_speaker: "", old_text: "<b>старый</b>", new_text: "новый" },
            ],
          },
        });
      }
      if (target.includes("/normalization/history/") && target.includes("/restore")) {
        if (restoreGate) await restoreGate.promise;
        return h.jsonResponse({ ok: true, result: h.doc("restored", body.expected_revision + 1) });
      }
      if (target.includes("/normalization/history")) {
        return h.jsonResponse({
          ok: true,
          history: [{ name, revision: 3, status: "confirmed", created_at: "2026-01-01T10:00:00+00:00", confirmed_at: "", blocks: 2 }],
        });
      }
      if (method === "PUT" && target.includes("/stage1/normalization")) {
        return h.jsonResponse({ ok: true, result: h.doc("saved edit", body.revision + 1) });
      }
      if (target.includes("/stage1/normalization?")) {
        return h.jsonResponse({ ok: true, result: h.doc("restored", 6) });
      }
      if (target.includes("/stage1/status")) {
        return h.jsonResponse({ ok: true, result: { normalization_confirmed: false, normalization_status: "draft" } });
      }
      throw new Error(`testNormalizationHistoryDiffRestore: unexpected fetch ${method} ${target}`);
    };
  };
  try {
    // List -> diff, with HTML in block text escaped.
    h.reset("original", 5);
    install();
    ids.normalization_history_panel.hidden = true;
    await normalization.toggleNormalizationHistory();
    assert.strictEqual(ids.normalization_history_panel.hidden, false);
    assert.ok(calls[0].target.includes("/stage1/normalization/history?project="));
    const listHtml = ids.normalization_history_list.innerHTML;
    assert.ok(listHtml.includes(name) && listHtml.includes("Ревизия 3") && listHtml.includes("подтверждён"));
    assert.ok(listHtml.includes("01.01.2026 12:00") && listHtml.includes("блоков: 2"));
    assert.ok(!listHtml.includes("path"), "filesystem paths are never shown");
    const item = { dataset: { name } };
    const button = { dataset: { historyAction: "diff" }, closest: () => item };
    ids.normalization_history_list.dispatch("click", { target: { closest: () => button } });
    await h.settle();
    const diffHtml = ids.normalization_history_diff.innerHTML;
    assert.strictEqual(ids.normalization_history_diff.hidden, false);
    assert.ok(diffHtml.includes("Блок 2") && diffHtml.includes("изменён текст"));
    assert.ok(diffHtml.includes("&lt;b&gt;старый&lt;/b&gt;") && !diffHtml.includes("<b>старый"));
    assert.ok(!diffHtml.includes("Блок 1"), "unchanged blocks are not listed");

    // Declined confirmation: nothing is sent.
    install();
    let done = normalization.restoreNormalizationSnapshot(name);
    ids.btn_confirm_dialog_cancel.onclick();
    await done;
    assert.strictEqual(calls.length, 0);
    assert.strictEqual(appState.normalization.revision, 5);

    // Clean restore: sent with the current revision, draft reloaded, never eligible.
    install();
    appState.normalizationEligible = true;
    done = normalization.restoreNormalizationSnapshot(name);
    confirmNow();
    await done;
    const restoreCall = calls.find((call) => call.target.includes("/restore"));
    assert.strictEqual(restoreCall.method, "POST");
    assert.deepStrictEqual(restoreCall.body, { project: state.project, expected_revision: 5 });
    assert.ok(!calls.some((call) => call.method === "PUT"), "no save without unsaved edits");
    assert.ok(calls.some((call) => call.method === "GET" && call.target.includes("/stage1/normalization?")));
    assert.strictEqual(appState.normalization.revision, 6);
    assert.strictEqual(appState.normalization.status, "draft");
    assert.strictEqual(appState.normalizationDirty, false);
    assert.strictEqual(appState.normalizationEligible, false);
    assert.strictEqual(ids.normalization_history_diff.hidden, true, "stale diff is cleared");

    // Unsaved edits are saved first, and the restore uses the saved revision.
    h.reset("original", 5);
    appState.normalization.blocks[0].text_for_tts = "unsaved";
    normalization.markNormalizationDirty();
    install();
    done = normalization.restoreNormalizationSnapshot(name);
    confirmNow();
    await done;
    const methods = calls.map((call) => `${call.method} ${call.target.includes("/restore") ? "restore" : "other"}`);
    assert.ok(methods.indexOf("PUT other") !== -1 && methods.indexOf("PUT other") < methods.indexOf("POST restore"));
    assert.strictEqual(calls.find((call) => call.target.includes("/restore")).body.expected_revision, 6);
    assert.strictEqual(appState.normalizationDirty, false);

    // An edit made while the restore request is in flight is kept, not overwritten.
    h.reset("original", 5);
    const gate = h.deferred();
    install({ restoreGate: gate });
    done = normalization.restoreNormalizationSnapshot(name);
    confirmNow();
    await h.settle();
    appState.normalization.blocks[0].text_for_tts = "typed during restore";
    normalization.markNormalizationDirty();
    gate.resolve();
    await done;
    assert.strictEqual(appState.normalization.blocks[0].text_for_tts, "typed during restore");
    assert.strictEqual(appState.normalization.revision, 6);
    assert.strictEqual(appState.normalizationDirty, true);
    assert.strictEqual(appState.normalizationEligible, false);
    assert.ok(!calls.some((call) => call.method === "GET" && call.target.includes("/stage1/normalization?")));
  } finally {
    h.restore();
  }
}

async function main() {
  const modules = await loadModules();
  const tests = [
    ["testNormalizationWarningReview", () => testNormalizationWarningReview(modules)],
    ["testNormalizationEditDuringGenerate", () => testNormalizationEditDuringGenerate(modules)],
    ["testTypeDecisionKeepsUnrelatedWarnings", () => testTypeDecisionKeepsUnrelatedWarnings(modules)],
    ["testNormalizationWarningLocations", () => testNormalizationWarningLocations(modules)],
    ["testComputeEtaSeconds", () => testComputeEtaSeconds(modules)],
    ["testRenderEtaCells", () => testRenderEtaCells(modules)],
    ["testCloudDisablesLocalWhisperModel", () => testCloudDisablesLocalWhisperModel(modules)],
    ["testHasUnsavedChangesAndBeforeunload", () => testHasUnsavedChangesAndBeforeunload(modules)],
    ["testStyleDegradedWarning", () => testStyleDegradedWarning(modules)],
    ["testRestoredSpeakerDirectory", () => testRestoredSpeakerDirectory(modules)],
    ["testPhraseTtsTimeout", () => testPhraseTtsTimeout(modules)],
    ["testOpenRouterUsagePollingSinglePending", () => testOpenRouterUsagePollingSinglePending(modules)],
    ["testOpenRouterUsagePollingSurvivesRenderError", () => testOpenRouterUsagePollingSurvivesRenderError(modules)],
    ["testConfirmDialog", () => testConfirmDialog(modules)],
    ["testSyncMatchSpeakerFilterHint", () => testSyncMatchSpeakerFilterHint(modules)],
    ["testSyncRetimePendingReviewQueue", () => testSyncRetimePendingReviewQueue(modules)],
    ["testSyncSourcesEditor", () => testSyncSourcesEditor(modules)],
    ["testRevisionsInbox", () => testRevisionsInbox(modules)],
    ["testRevisionImportTimeout", () => testRevisionImportTimeout(modules)],
    ["testBuildTimelineSurfacesWarnings", () => testBuildTimelineSurfacesWarnings(modules)],
    ["testSyncMatchVerificationReasons", () => testSyncMatchVerificationReasons(modules)],
    ["testSyncJobIncompleteAndAsrFailedLabel", () => testSyncJobIncompleteAndAsrFailedLabel(modules)],
    ["testStage3PlaceholderDescription", () => testStage3PlaceholderDescription(modules)],
    ["testExportXmlPreExportSummary", () => testExportXmlPreExportSummary(modules)],
    ["testNormalizationEditDuringConfirm", () => testNormalizationEditDuringConfirm(modules)],
    ["testNormalizationSaveAcceptsRevisionAfterEdit", () => testNormalizationSaveAcceptsRevisionAfterEdit(modules)],
    ["testApplyStatusSyncsTimelineReady", () => testApplyStatusSyncsTimelineReady(modules)],
    ["testConfirmPreviewSummaryAndBlocking", () => testConfirmPreviewSummaryAndBlocking(modules)],
    ["testConfirmRejectedShowsRussianReasons", () => testConfirmRejectedShowsRussianReasons(modules)],
    ["testSyncFragmentEditing", () => testSyncFragmentEditing(modules)],
    ["testGroupReviewOnlySelectedBlocks", () => testGroupReviewOnlySelectedBlocks(modules)],
    ["testNormalizationHistoryDiffRestore", () => testNormalizationHistoryDiffRestore(modules)],
  ];

  let passed = 0;
  let failed = 0;
  for (const [name, test] of tests) {
    try {
      await test();
      console.log(`✓ ${name} passed`);
      passed += 1;
    } catch (err) {
      console.error(`✗ ${name} failed: ${err.message}`);
      failed += 1;
    }
  }

  if (failed > 0) {
    console.error(`\n${failed} test(s) failed, ${passed} passed`);
    process.exitCode = 1;
  } else {
    console.log(`\nAll ${passed} tests passed!`);
    process.exitCode = 0;
  }
}

main().catch((err) => {
  console.error("Unexpected failure while running stage1 behavior tests:", err);
  process.exitCode = 1;
});
