/* eslint-env node */
"use strict";
const assert = require("assert");
const path = require("path");
const { pathToFileURL } = require("url");

const dir = path.resolve(__dirname, "../src/static/stage1");
const mod = (name) => pathToFileURL(path.join(dir, name)).href;
class Element {
  constructor() { this.value = ""; this.dataset = {}; this.files = []; this.innerHTML = ""; this.textContent = ""; this.hidden = false; this.listeners = {}; this.style = {}; this.classList = { add() {}, remove() {} }; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  appendChild(child) { (this.children ||= []).push(child); return child; }
  remove() {}
  dispatch(type, event) { for (const fn of this.listeners[type] || []) fn(event); }
  closest(selector) { return this.ancestors?.[selector] || (selector.split(",").some((item) => this.selectors?.includes(item.trim())) ? this : null); }
  matches(selector) { return (this.selectors || []).includes(selector); }
  querySelector(selector) { return this.controls?.[selector] || new Element(); }
}
const elements = new Map();
globalThis.document = { getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); }, createElement() { return new Element(); }, querySelectorAll() { return []; }, addEventListener() {} };
globalThis.window = globalThis; globalThis.addEventListener = () => {};

(async () => {
  const state = await import(mod("state.js"));
  const workflow = await import(mod("translated_sync.js"));
  const sync = await import(mod("sync.js"));
  const source = { id: "src 1", duration_sec: 40, metadata: {} };
  const card = { dataset: { sourceId: "src 1" }, querySelector(selector) { return ({
    "[data-ts-file]": { files: [{ name: "lines.docx" }] }, "[data-ts-mode]": { value: "display" }, "[data-ts-start]": { value: "01:00:00" },
  })[selector] || null; } };
  const requests = [];
  const oldFetch = globalThis.fetch;
  let restoreClock = null;
  try {
    globalThis.fetch = async (url, options = {}) => { requests.push({ url: String(url), options }); return { ok: true, json: async () => ({ ok: true, result: { cue_count: 1, matches: [], translation_audio: { active: "tts", variants: {} } } }) }; };
    await workflow.uploadWordTranscript(source, card, async () => {});
    assert.match(requests[0].url, /sync-sources\/src%201\/transcript-docx/);
    assert.equal(requests[0].options.body.get("timecode_mode"), "display");
    assert.equal(requests[0].options.body.get("sequence_start_tc"), "01:00:00");
    await workflow.findByWord([source], async () => {});
    assert.match(requests[1].url, /translated-sync\/match/);
    assert.deepEqual(JSON.parse(requests[1].options.body).source_ids, ["src 1"]);
    assert.equal(JSON.parse(requests[1].options.body).semantic, false);
    assert.match(workflow.renderSourceControls(source), /data-ts-semantic/);
    await workflow.findByWord([source], async () => {}, true);
    assert.equal(JSON.parse(requests[2].options.body).semantic, true);
    assert.match(workflow.renderSourceControls(source), /Смысловой поиск: 0\/8 запросов/);
    assert.match(workflow.audioFileUrl("fragment-2", "recorded"), /translated-sync\/fragment-2\/audio-file/);
    assert.match(workflow.renderTranslatedControls({ segment_id: "fragment-2", metadata: { translation_audio: { active: "recorded", delay_sec: .8, variants: { recorded: { source_in_sec: 4, source_out_sec: 8 } } } } }), /data-ts-active/);
    assert.match(workflow.renderTranslatedControls({ segment_id: "fragment-2" }), /Сгенерировать TTS/);
    assert.match(workflow.renderTranslatedControls({ segment_id: "unassigned", status: "needs_review" }), /data-ts-prerequisite[^>]*>Сначала назначьте/);
    assert.match(workflow.renderTranslatedControls({ segment_id: "fragment-2", source_id: "src 1", source_in_sec: 8, source_out_sec: 18, status: "confirmed", metadata: { speech_in_sec: 10, translation_audio: { active: "tts", variants: { tts: { source_in_sec: 0, source_out_sec: 14 } } } } }), /value="original"/);
    state.appState.syncSources = [{ id: "src 1", guide_offset_sec: 5 }];
    const previewHtml = workflow.renderTranslatedControls({ segment_id: "fragment-2", source_id: "src 1", source_in_sec: 8, source_out_sec: 18, status: "confirmed", metadata: { speech_in_sec: 10, translation_audio: { active: "tts", variants: { tts: { source_in_sec: 0, source_out_sec: 14 } } } } });
    assert.match(previewHtml, /data-ts-guide-offset="5"/);
    assert.match(previewHtml, /data-ts-mix-preview/);
    const originalOnlyHtml = workflow.renderTranslatedControls({ segment_id: "fragment-2", source_id: "src 1", source_in_sec: 8, source_out_sec: 18, status: "confirmed", metadata: { translation_audio: { enabled: false, active: "tts", variants: { tts: { source_in_sec: 0, source_out_sec: 14 }, recorded: { recording_id: "rec-a", source_in_sec: 2, source_out_sec: 6 } } } } });
    assert.match(originalOnlyHtml, /<option value="original" selected>Только исходник/);
    assert.match(originalOnlyHtml, /<option value="tts"/); assert.match(originalOnlyHtml, /<option value="recorded"/);

    const oldFetchForWorkspace = globalThis.fetch;
    globalThis.fetch = async (url) => {
      const target = String(url);
      const result = target.includes("sync-sources?") ? { items: [{ id: "src 1", guide_offset_sec: 5 }] }
        : target.includes("sync-matches?") ? { matches: [{ segment_id: "confirmed-fragment", status: "confirmed", source_id: "src 1", source_in_sec: 8, source_out_sec: 18, speaker: "Аня", metadata: { speech_in_sec: 10, translation_audio: { active: "tts", variants: { tts: { source_in_sec: 0, source_out_sec: 14 } } } } }] }
          : { status: "idle" };
      return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, result }) };
    };
    await sync.loadSyncWorkspace(true);
    assert.match(state.ids.sync_review_list.innerHTML, /data-ts-sync="confirmed-fragment"/, "all-confirmed view keeps translated controls reachable");
    globalThis.fetch = oldFetchForWorkspace;

    // Ordinary source review uses guide-local playback but keeps sequence edits.
    const reviewMatch = { segment_id: "offset-review", status: "confirmed", source_id: "src 1", source_in_sec: 21, source_out_sec: 31,
      metadata: { retime_pending_review: true, document_cue_id: "removed-cue" },
      alternatives: [{ source_id: "src 2", source_in_sec: 44, source_out_sec: 49, document_cue_id: "second-cue" }] };
    globalThis.fetch = async (url, options = {}) => {
      requests.push({ url: String(url), options });
      const target = String(url);
      const result = target.includes("sync-sources?") ? { items: [{ id: "src 1", guide_offset_sec: 20 }, { id: "src 2", guide_offset_sec: 40 }] }
        : target.includes("sync-matches?") ? { matches: [reviewMatch] } : { status: "idle" };
      return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, result }), json: async () => ({ ok: true, result }) };
    };
    await sync.loadSyncWorkspace(true);
    assert.match(state.ids.sync_review_list.innerHTML, /data-range-in="1" data-range-out="11"/, "review player subtracts guide offset");
    const sourceReview = new Element(); sourceReview.dataset = { matchId: "offset-review", sourceId: "src 1" };
    const sourceAudio = new Element(); sourceAudio.load = () => {};
    const selectCandidate = new Element(); selectCandidate.value = "1"; selectCandidate.selectors = ["[data-sync-candidate]"];
    selectCandidate.ancestors = { ".sync-match-card": sourceReview };
    const editIn = new Element(); const editOut = new Element();
    sourceReview.controls = { "[data-sync-candidate]": selectCandidate, "[data-sync-in]": editIn, "[data-sync-out]": editOut, "[data-sync-audio]": sourceAudio };
    state.ids.sync_review_list.dispatch("change", { target: selectCandidate });
    assert.equal(sourceAudio.dataset.rangeIn, 4); assert.equal(sourceAudio.dataset.rangeOut, 9);
    assert.equal(editIn.value, "44.000", "editor keeps canonical sequence time");
    editIn.value = "45"; editOut.value = "48"; editIn.selectors = ["[data-sync-in], [data-sync-out]"];
    editIn.ancestors = { ".sync-match-card": sourceReview };
    state.ids.sync_review_list.dispatch("change", { target: editIn });
    assert.equal(sourceAudio.dataset.rangeIn, 5); assert.equal(sourceAudio.dataset.rangeOut, 8);
    // Returning to a pending or unbound primary must not submit a stale/empty ID.
    selectCandidate.value = "0";
    state.ids.sync_review_list.dispatch("change", { target: selectCandidate });
    const confirmButton = new Element(); confirmButton.selectors = ["[data-action='confirm-sync-match']"];
    confirmButton.dataset.matchId = "offset-review"; confirmButton.ancestors = { ".sync-match-card": sourceReview };
    for (const metadata of [{ retime_pending_review: true, document_cue_id: "removed-cue" }, { document_cue_unbound: true }]) {
      reviewMatch.metadata = metadata;
      await sync.loadSyncWorkspace(true);
      state.ids.sync_review_list.dispatch("click", { target: confirmButton });
      await new Promise((resolve) => setTimeout(resolve, 20));
      const submitted = requests.filter((item) => item.options.method === "PATCH" && item.url.includes("offset-review")).at(-1);
      assert.ok(submitted, "review confirmation submitted");
      assert.equal(Object.hasOwn(JSON.parse(submitted.options.body), "document_cue_id"), false);
    }
    globalThis.fetch = oldFetchForWorkspace;

    const reviewList = new Element();
    const controls = Object.fromEntries([["[data-ts-active]", { value: "tts" }], ["[data-ts-voice]", { value: "Переводной голос" }], ["[data-ts-delay]", { value: "0.8" }], ["[data-ts-gain]", { value: "2" }], ["[data-ts-duck]", { value: "-18" }], ["[data-ts-fade]", { value: "0.2" }], ["[data-ts-in]", { value: "4" }], ["[data-ts-out]", { value: "8" }], ["[data-ts-recording]", { value: "rec-1" }]]);
    const reviewCard = new Element(); reviewCard.dataset.tsSync = "fragment-2"; reviewCard.dataset.tsCanGenerate = "true"; reviewCard.dataset.tsSourceIn = "8"; reviewCard.dataset.tsSourceOut = "18"; reviewCard.dataset.tsSpeechIn = "10"; reviewCard.dataset.tsGuideOffset = "5"; reviewCard.dataset.tsSourceUrl = "/source-audio"; reviewCard.controls = controls;
    const ttsButton = new Element(); ttsButton.selectors = ["[data-ts-generate]"]; ttsButton.ancestors = { "[data-ts-sync]": reviewCard };
    workflow.bindTranslatedSync({ reviewList, onRefresh: async () => {} });
    const normalFetch = globalThis.fetch;
    globalThis.fetch = async (url) => ({ ok: true, json: async () => ({ ok: true, result: { recordings: [{ id: "rec-a", part_name: "Camera dubbing.wav", duration_sec: 9 }] } }) });
    const audioStateCard = new Element(); audioStateCard.selectors = ["[data-ts-sync]"]; audioStateCard.dataset.tsSync = "fragment-2"; audioStateCard.dataset.tsCanGenerate = "true"; audioStateCard.open = true;
    const recordingSelect = new Element(); recordingSelect.dataset.tsRecordingCurrent = "rec-a"; audioStateCard.controls = { "[data-ts-recording]": recordingSelect };
    reviewList.dispatch("toggle", { target: audioStateCard }); await new Promise((resolve) => setImmediate(resolve));
    assert.equal(recordingSelect.children[0].textContent, "Camera dubbing.wav", "common recording list uses its user-facing part name");
    assert.equal(recordingSelect.children[0].selected, true, "existing shared recording remains selected");
    globalThis.fetch = normalFetch;
    reviewList.dispatch("click", { target: ttsButton });
    await new Promise((resolve) => setTimeout(resolve, 20));
    const ttsRequest = requests.find((request) => request.url.includes("/fragment-2/tts"));
    assert.ok(ttsRequest, "Generate TTS posts against the match segment ID");
    assert.equal(JSON.parse(ttsRequest.options.body).voice_name, "Переводной голос");
    const activeSelect = new Element(); activeSelect.selectors = ["[data-ts-active]"]; activeSelect.value = "recorded"; activeSelect.ancestors = { "[data-ts-sync]": reviewCard };
    reviewList.dispatch("change", { target: activeSelect });
    await new Promise((resolve) => setTimeout(resolve, 20));
    const rangePatch = requests.find((request) => request.url.endsWith("/fragment-2/audio"));
    assert.equal(JSON.parse(rangePatch.options.body).active, "recorded");
    assert.equal(Object.hasOwn(JSON.parse(rangePatch.options.body), "source_in_sec"), false, "switching variant must keep its own range");
    const originalOnly = new Element(); originalOnly.selectors = ["[data-ts-active]"]; originalOnly.value = "original"; originalOnly.ancestors = { "[data-ts-sync]": reviewCard };
    reviewList.dispatch("change", { target: originalOnly }); await new Promise((resolve) => setTimeout(resolve, 20));
    const disableRequest = requests.filter((request) => request.url.endsWith("/fragment-2/audio")).at(-1);
    assert.equal(JSON.parse(disableRequest.options.body).enabled, false);
    assert.equal(Object.hasOwn(JSON.parse(disableRequest.options.body), "active"), false);
    const failedButton = new Element(); failedButton.selectors = ["[data-ts-save]"]; failedButton.ancestors = { "[data-ts-sync]": reviewCard };
    const errorNode = { hidden: true, textContent: "" }; reviewCard.controls["[data-ts-error]"] = errorNode;
    globalThis.fetch = async (url, options = {}) => { requests.push({ url: String(url), options }); return { ok: false, status: 400, json: async () => ({ ok: false, error: "invalid range" }) }; };
    reviewList.dispatch("click", { target: failedButton });
    await new Promise((resolve) => setTimeout(resolve, 20));
    assert.equal(errorNode.hidden, false); assert.match(errorNode.textContent, /invalid range/);
    globalThis.fetch = async (url, options = {}) => { requests.push({ url: String(url), options }); return { ok: true, json: async () => ({ ok: true, result: {} }) }; };
    controls["[data-ts-audio]"] = { files: [{ name: "short.wav" }] };
    const uploadButton = new Element(); uploadButton.selectors = ["[data-ts-upload-audio]"]; uploadButton.ancestors = { "[data-ts-sync]": reviewCard };
    reviewList.dispatch("click", { target: uploadButton });
    await new Promise((resolve) => setTimeout(resolve, 20));
    const recordingRequest = requests.find((request) => request.url.includes("/fragment-2/recording"));
    assert.ok(recordingRequest, "uploading a replacement recording reaches the segment endpoint");
    assert.equal(recordingRequest.options.body.has("source_in_sec"), false, "replacement upload must not inherit prior variant bounds");
    assert.equal(recordingRequest.options.body.has("source_out_sec"), false);
    const oldSetTimeout = globalThis.setTimeout; const oldClearTimeout = globalThis.clearTimeout;
    let clockMs = 0; let nextTimerId = 1; const timers = new Map();
    globalThis.setTimeout = (callback, delay = 0) => { const id = nextTimerId++; timers.set(id, { at: clockMs + delay, callback }); return id; };
    globalThis.clearTimeout = (id) => timers.delete(id);
    restoreClock = () => { globalThis.setTimeout = oldSetTimeout; globalThis.clearTimeout = oldClearTimeout; restoreClock = null; };
    const advance = async (ms) => { const target = clockMs + ms; while (true) { const pending = [...timers.entries()].sort((a, b) => a[1].at - b[1].at)[0]; if (!pending || pending[1].at > target) break; timers.delete(pending[0]); clockMs = pending[1].at; pending[1].callback(); await new Promise((resolve) => setImmediate(resolve)); } clockMs = target; };
    class FakeAudioContext {
      constructor() { this.currentTime = clockMs / 1000; this.destination = {}; this.sources = []; this.gains = []; this.closed = false; FakeAudioContext.last = this; }
      async resume() {}
      createMediaElementSource(media) { const source = { media, connect() {} }; this.sources.push(source); return source; }
      createGain() { const calls = []; const node = { connect() {}, gain: { setValueAtTime(...args) { calls.push(["set", ...args]); }, linearRampToValueAtTime(...args) { calls.push(["ramp", ...args]); }, calls } }; this.gains.push(node); return node; }
      async close() { this.closed = true; }
    }
    class FakeMedia {
      constructor(url) { this.src = url; this.currentTime = 0; this.playCalls = 0; FakeMedia.instances.push(this); }
      play() { this.playCalls += 1; this.playing = true; return Promise.resolve(); }
      pause() { this.playing = false; }
    }
    FakeMedia.instances = [];
    window.AudioContext = FakeAudioContext;
    globalThis.Audio = FakeMedia;
    controls["[data-ts-active]"].value = "recorded";
    reviewCard.controls["[data-ts-stop-preview]"] = { hidden: true };
    reviewCard.controls["[data-ts-mix-preview]"] = { disabled: false };
    globalThis.fetch = async (url, options = {}) => { requests.push({ url: String(url), options }); return { ok: true, arrayBuffer: async () => new ArrayBuffer(8), json: async () => ({ ok: true, result: {} }) }; };
    const previewButton = new Element(); previewButton.selectors = ["[data-ts-mix-preview]"]; previewButton.ancestors = { "[data-ts-sync]": reviewCard };
    reviewList.dispatch("click", { target: previewButton });
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(FakeMedia.instances.map((media) => media.currentTime), [3, 4], "joint preview seeks raw guide by sequence range minus guide offset and preserves selected voice head");
    assert.equal(FakeMedia.instances[1].playCalls, 0, "translation remains paused until its actual start offset");
    await advance(2799);
    assert.equal(FakeMedia.instances[1].playCalls, 0, "voice does not advance before speech head plus delay");
    await advance(1);
    assert.equal(FakeMedia.instances[1].playCalls, 1, "voice starts exactly at speech head plus delay");
    assert.equal(FakeMedia.instances[1].currentTime, 4, "selected voice in point is preserved until onset");
    assert.match(FakeMedia.instances[0].src, /source-audio/);
    assert.match(FakeMedia.instances[1].src, /fragment-2\/audio-file/);
    assert.ok(FakeAudioContext.last.sources.length === 2, "joint preview routes both media streams through gain controls");
    assert.ok(FakeAudioContext.last.gains[0].gain.calls.some((call) => call[0] === "ramp" && call[1] < 1), "original is ducked during overlap");
    assert.ok(FakeAudioContext.last.gains[0].gain.calls.some((call) => call[0] === "ramp" && Math.abs(call[2] - 2.8) < .01), "fade controls source duck transition at voice onset");
    assert.equal(FakeAudioContext.last.gains[1].gain.calls.some((call) => call[0] === "ramp"), false, "Russian gain remains constant");
    const firstContext = FakeAudioContext.last;
    const secondCard = new Element(); secondCard.dataset = { ...reviewCard.dataset, tsSync: "fragment-3" }; secondCard.controls = { ...controls, "[data-ts-stop-preview]": { hidden: true }, "[data-ts-mix-preview]": { disabled: false } };
    const secondPreview = new Element(); secondPreview.selectors = ["[data-ts-mix-preview]"]; secondPreview.ancestors = { "[data-ts-sync]": secondCard };
    reviewList.dispatch("click", { target: secondPreview }); await new Promise((resolve) => setImmediate(resolve));
    assert.equal(firstContext.closed, true, "starting a preview on another card stops the active preview first");
    const stopButton = new Element(); stopButton.selectors = ["[data-ts-stop-preview]"]; stopButton.ancestors = { "[data-ts-sync]": secondCard };
    reviewList.dispatch("click", { target: stopButton });
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(FakeAudioContext.last.closed, true, "stop control closes the streaming preview context");
    const thirdCard = new Element(); thirdCard.dataset = { ...secondCard.dataset, tsSync: "fragment-4" }; thirdCard.controls = { ...controls, "[data-ts-stop-preview]": { hidden: true }, "[data-ts-mix-preview]": { disabled: false } };
    const thirdPreview = new Element(); thirdPreview.selectors = ["[data-ts-mix-preview]"]; thirdPreview.ancestors = { "[data-ts-sync]": thirdCard };
    reviewList.dispatch("click", { target: thirdPreview }); await new Promise((resolve) => setImmediate(resolve));
    const changedRange = new Element(); changedRange.selectors = ["[data-ts-in]"]; changedRange.ancestors = { "[data-ts-sync]": thirdCard };
    const thirdContext = FakeAudioContext.last; reviewList.dispatch("input", { target: changedRange });
    assert.equal(thirdContext.closed, true, "editing a range stops the active preview");
    restoreClock();
  } finally { restoreClock?.(); globalThis.fetch = oldFetch; }
  console.log("translated sync browser behavior: PASS");
})().catch((error) => { console.error(error); process.exitCode = 1; });
