"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");

function testPlaceholderSelection() {
  const context = {
    $: {_roughCutTestMode: true},
    app: {},
    console,
    Date,
    JSON,
    Math,
    decodeURIComponent,
    encodeURIComponent,
    isFinite,
    File: function File() {},
    Folder: function Folder() {},
    Time: function Time() { this.ticks = "0"; },
  };
  vm.createContext(context);
  vm.runInContext(
    fs.readFileSync(path.join(root, "cep", "host.jsx"), "utf8"),
    context,
    {filename: "host.jsx"},
  );

  function clip(nodeId, label, start) {
    return {
      nodeId,
      name: label,
      projectItem: {name: label},
      start: {seconds: start},
      end: {seconds: start + 4},
    };
  }

  const label = "ОЛЬГА МЕЛЬНИК [00:12:43]";
  const clips = {
    0: clip("snh15-node", label, 517.7405),
    1: clip("snh26-node", label, 918.2779),
    numItems: 2,
  };
  const target = {videoTracks: [{clips}]};
  const params = {
    videoTrackIndex: 0,
    timelineSeconds: 517.7405,
    placeholderName: "snh15_placeholder.png",
    placeholderNames: vm.runInContext("[]", context),
  };
  params.placeholderNames.push("snh15_placeholder.png", label);
  const selected = context.$._roughCutStage2Test.placeholderAnywhere(target, params);
  assert.strictEqual(selected.nodeId, "snh15-node");

  clips[1] = clip("ambiguous-node", label, 517.75);
  assert.throws(
    () => context.$._roughCutStage2Test.placeholderAnywhere(target, params),
    /including 2 at expected/,
  );
}

function testWhisperPollingAndEta() {
  class Element {
    constructor() {
      this.value = "";
      this.textContent = "";
      this.className = "";
      this.style = {};
      this.attributes = {};
      this.children = [];
      this.firstChild = null;
      this.disabled = false;
      this.checked = false;
    }
    addEventListener() {}
    appendChild(child) {
      this.children.push(child);
      this.firstChild = this.children[0] || null;
      return child;
    }
    removeChild(child) {
      this.children = this.children.filter((item) => item !== child);
      this.firstChild = this.children[0] || null;
    }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    removeAttribute(name) { delete this.attributes[name]; }
  }

  const elements = {};
  const timers = [];
  let lastUrl = "";
  const job = {
    id: "job-1",
    status: "running",
    message: "Matching scenario quotes",
    total: 44,
    total_segments: 44,
    done: 11,
    matched: 8,
    review: 2,
    unmatched: 1,
    matching_started_at: new Date(Date.now() - 100000).toISOString(),
  };

  class FakeXHR {
    open(_method, url) { lastUrl = url; }
    setRequestHeader() {}
    send() {
      this.status = 200;
      this.readyState = 4;
      this.responseText = JSON.stringify({ok: true, result: job});
      this.onreadystatechange();
    }
  }

  const context = {
    console,
    Date,
    JSON,
    Math,
    Array,
    Error,
    isFinite,
    encodeURIComponent,
    decodeURIComponent,
    XMLHttpRequest: FakeXHR,
  };
  context.window = context;
  context.__ROUGH_CUT_TEST_MODE__ = true;
  context.CSInterface = function CSInterface() {};
  context.localStorage = {
    values: {},
    getItem(key) { return this.values[key] || null; },
    setItem(key, value) { this.values[key] = String(value); },
  };
  context.setTimeout = function setTimeoutFake(callback, delay) {
    timers.push({callback, delay});
    return timers.length;
  };
  context.clearTimeout = function clearTimeoutFake() {};
  context.document = {
    readyState: "loading",
    addEventListener() {},
    createElement() { return new Element(); },
    getElementById(id) {
      if (!elements[id]) elements[id] = new Element();
      return elements[id];
    },
  };

  vm.createContext(context);
  vm.runInContext(
    fs.readFileSync(path.join(root, "cep", "js", "main.js"), "utf8"),
    context,
    {filename: "main.js"},
  );
  const hooks = context.__ROUGH_CUT_TEST_HOOKS__;
  const element = (id) => context.document.getElementById(id);
  assert.strictEqual(hooks.etaFromProgress(100, 25, 100), 300);
  assert.strictEqual(hooks.formatDuration(3661), "1:01:01");

  element("projectName").value = "RVK_ULINOVSK";
  element("apiBaseUrl").value = "http://127.0.0.1:8000";
  element("apiToken").value = "memory-only-token";
  element("projectsRoot").value = "/projects";
  element("transcribePath").value = "/stage2/sync-jobs/start";

  hooks.renderWhisperJob({
    status: "running",
    message: "Transcribing audio",
    total: 0,
    transcription_processed_sec: 300,
    transcription_duration_sec: 600,
    transcription_words: 120,
    transcription_source_started_at: new Date(Date.now() - 100000).toISOString(),
  });
  assert.strictEqual(element("whisperProgressText").textContent, "5:00 / 10:00 · 120 слов");
  assert.match(element("whisperEta").textContent, /^Осталось: ≈ 1:4[0-1]$/);

  hooks.refreshWhisperStatus();
  assert.match(lastUrl, /\/stage2\/sync-jobs\/status\?project=RVK_ULINOVSK$/);
  assert.strictEqual(element("whisperStatus").textContent, "в работе");
  assert.strictEqual(element("whisperSegments").textContent, "11 / 44");
  assert.strictEqual(timers[timers.length - 1].delay, 2500);
}

testPlaceholderSelection();
testWhisperPollingAndEta();
