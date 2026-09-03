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
  return { state, core, normalization };
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

async function main() {
  const modules = await loadModules();
  const tests = [
    ["testComputeEtaSeconds", () => testComputeEtaSeconds(modules)],
    ["testRenderEtaCells", () => testRenderEtaCells(modules)],
    ["testHasUnsavedChangesAndBeforeunload", () => testHasUnsavedChangesAndBeforeunload(modules)],
    ["testConfirmDialog", () => testConfirmDialog(modules)],
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
