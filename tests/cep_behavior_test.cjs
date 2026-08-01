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

  const stableIdentity = "RC_BLOCK block-266-ea56f9c6";
  const stableClip = clip(
    "stable-snh98",
    stableIdentity + " | Шпагин []",
    3032.96,
  );
  const stableClips = {0: stableClip, numItems: 1};
  const stableTarget = {videoTracks: [{clips: stableClips}]};
  const stableParams = {
    videoTrackIndex: 0,
    timelineSeconds: 3227.4925,
    placeholderIdentity: stableIdentity,
    placeholderName: "snh98_placeholder.png",
    placeholderNames: vm.runInContext("[]", context),
  };
  stableParams.placeholderNames.push(
    "snh98_placeholder.png",
    "Шпагин []",
  );
  assert.strictEqual(
    context.$._roughCutStage2Test.placeholderAnywhere(
      stableTarget,
      stableParams,
    ).nodeId,
    "stable-snh98",
  );

  const staleClip = clip("stale-snh98", "Шпагин []", 3032.96);
  const staleTarget = {
    videoTracks: [{clips: {0: staleClip, numItems: 1}}],
  };
  assert.throws(
    () => context.$._roughCutStage2Test.placeholderAnywhere(
      staleTarget,
      stableParams,
    ),
    /stale or legacy.*Re-import the latest XML.*before timeline changes/,
  );

  const prefixCollision = clip(
    "block-266-prefix-collision",
    "RC_BLOCK block-266-ea56f9c60 | Шпагин []",
    3032.96,
  );
  assert.throws(
    () => context.$._roughCutStage2Test.placeholderAnywhere(
      {videoTracks: [{clips: {0: prefixCollision, numItems: 1}}]},
      stableParams,
    ),
    /stale or legacy/,
  );
}

function testCyrillicProjectNames() {
  const context = {
    $: {_roughCutTestMode: true}, app: {}, console, Date, JSON, Math,
    decodeURIComponent, encodeURIComponent, isFinite,
    File: function File() {}, Folder: function Folder() {},
    Time: function Time() { this.ticks = "0"; },
  };
  vm.createContext(context);
  vm.runInContext(
    fs.readFileSync(path.join(root, "cep", "host.jsx"), "utf8"),
    context,
    {filename: "host.jsx"},
  );
  const valid = context.$._roughCutStage2Test.validProjectName;
  assert.strictEqual(valid("Русский_проект-01"), true);
  assert.strictEqual(valid("demo_01"), true);
  assert.strictEqual(valid("имя проекта"), false);
  assert.strictEqual(valid("../demo"), false);
}

function testMarkerAwareConformResume() {
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

  const item = {
    segmentId: "snh97",
    speaker: "Татьяна Васильева",
    text: "Это   точный текст\nреплики.",
    sourceSequenceName: "100626_Васильева",
    sourceInSeconds: 1028.4,
    sourceOutSeconds: 1034.125,
    timelineSeconds: 12,
    videoTrackIndex: 0,
    audioTrackIndex: 0,
  };

  function marker(overrides) {
    return Object.assign({
      name: "RC_SYNC snh97",
      comments: [
        "snh",
        "Это точный текст",
        "реплики.",
        "Source: 100626_Васильева [1028.400–1034.125]",
        "Confidence: 93%",
      ].join("\n"),
      start: {seconds: 12},
      end: {seconds: 17.725},
    }, overrides || {});
  }

  function targetWith(markers, getNext) {
    return {
      markers: {
        getFirstMarker() { return markers[0] || null; },
        getNextMarker(current) {
          if (getNext) return getNext(current);
          const index = markers.indexOf(current);
          return index >= 0 ? markers[index + 1] || null : null;
        },
      },
    };
  }

  const hooks = context.$._roughCutStage2Test;
  assert.deepStrictEqual(
    JSON.parse(JSON.stringify(hooks.conformMarkerState(targetWith([]), item))),
    {status: "absent", name: "RC_SYNC snh97"},
  );

  // The marker's legacy speaker label may differ, but the normalized item
  // text and exact source identity still make it a safe completion record.
  const valid = hooks.conformMarkerState(targetWith([marker()]), item);
  assert.strictEqual(valid.status, "valid");
  assert.strictEqual(valid.name, "RC_SYNC snh97");
  assert.strictEqual(valid.timelineSeconds, 12);

  function clips(items) {
    const collection = items.slice();
    collection.numItems = collection.length;
    return collection;
  }

  function sourceClip(nodeId, overrides) {
    return Object.assign({
      nodeId,
      name: "100626_Васильева",
      projectItem: sourceItem,
      start: {seconds: 12},
      end: {seconds: 17.725},
      inPoint: {seconds: 1028.4},
      outPoint: {seconds: 1034.125},
    }, overrides || {});
  }

  const sourceItem = {nodeId: "source-sequence-node"};
  const videoClip = sourceClip("video-node");
  const audioClip = sourceClip("audio-node");
  function insertionTarget(videoItems, audioItems) {
    return {
      videoTracks: [{clips: clips(videoItems)}],
      audioTracks: [{clips: clips(audioItems)}],
    };
  }

  const verified = hooks.verifyConformMarkerInsertion(
    insertionTarget([videoClip], [audioClip]),
    item,
    sourceItem,
    valid,
  );
  assert.strictEqual(verified.videoClip.nodeId, "video-node");
  assert.strictEqual(verified.audioClip.nodeId, "audio-node");

  const driftVideo = sourceClip("drift-video", {
    start: {seconds: 21.5},
    end: {seconds: 27.225},
  });
  const driftAudio = sourceClip("drift-audio", {
    start: {seconds: 21.5},
    end: {seconds: 27.225},
  });
  const driftVerified = hooks.verifyConformMarkerInsertion(
    insertionTarget([driftVideo], [driftAudio]),
    item,
    sourceItem,
    valid,
  );
  assert.strictEqual(driftVerified.videoClip.nodeId, "drift-video");
  assert.strictEqual(driftVerified.audioClip.nodeId, "drift-audio");

  assert.throws(
    () => hooks.verifyConformMarkerInsertion(
      insertionTarget([], []),
      item,
      sourceItem,
      valid,
    ),
    /stale or partial.*both inserted video and audio/,
  );
  assert.throws(
    () => hooks.verifyConformMarkerInsertion(
      insertionTarget([videoClip], []),
      item,
      sourceItem,
      valid,
    ),
    /stale or partial.*both inserted video and audio/,
  );
  assert.throws(
    () => hooks.verifyConformMarkerInsertion(
      insertionTarget(
        [videoClip],
        [sourceClip("wrong-audio-range", {outPoint: {seconds: 1034.0}})],
      ),
      item,
      sourceItem,
      valid,
    ),
    /stale or partial.*Inserted range verification failed/,
  );

  const movableMarker = marker();
  const reanchored = hooks.reanchorConformMarker(
    {},
    movableMarker,
    item,
    21.5,
  );
  assert.strictEqual(movableMarker.start, 21.5);
  assert.ok(Math.abs(movableMarker.end - 27.225) < 0.000001);
  assert.strictEqual(reanchored.report.startSeconds, 21.5);
  assert.ok(Math.abs(reanchored.report.endSeconds - 27.225) < 0.000001);

  let strictStart = 12;
  let strictEnd = 17.725;
  const strictMarker = marker();
  Object.defineProperties(strictMarker, {
    start: {
      get() { return {seconds: strictStart}; },
      set(value) {
        if (Number(value) > strictEnd) {
          throw new Error("start exceeds current end");
        }
        strictStart = Number(value);
      },
      configurable: true,
    },
    end: {
      get() { return {seconds: strictEnd}; },
      set(value) {
        if (Number(value) < strictStart) {
          throw new Error("end precedes current start");
        }
        strictEnd = Number(value);
      },
      configurable: true,
    },
  });
  hooks.reanchorConformMarker({}, strictMarker, item, 21.5);
  assert.strictEqual(strictStart, 21.5);
  assert.ok(Math.abs(strictEnd - 27.225) < 0.000001);

  const fixedStart = 12;
  const fixedEnd = 17.725;
  const fixedMarker = marker();
  Object.defineProperties(fixedMarker, {
    start: {
      get() { return {seconds: fixedStart}; },
      set() {},
      configurable: true,
    },
    end: {
      get() { return {seconds: fixedEnd}; },
      set() {},
      configurable: true,
    },
  });
  const markerList = [fixedMarker];
  const recreateTarget = {
    markers: {
      getFirstMarker() { return markerList[0] || null; },
      getNextMarker(current) {
        const index = markerList.indexOf(current);
        return index >= 0 ? markerList[index + 1] || null : null;
      },
      createMarker(start) {
        const created = {
          name: "",
          comments: "",
          start,
          end: start,
        };
        markerList.push(created);
        return created;
      },
      deleteMarker(value) {
        const index = markerList.indexOf(value);
        if (index < 0) return false;
        markerList.splice(index, 1);
        return true;
      },
    },
  };
  const recreated = hooks.reanchorConformMarker(
    recreateTarget,
    fixedMarker,
    item,
    21.5,
  );
  assert.strictEqual(recreated.report.recreated, true);
  assert.strictEqual(markerList.length, 1);
  assert.strictEqual(recreated.marker, markerList[0]);
  assert.notStrictEqual(recreated.marker, fixedMarker);
  assert.strictEqual(recreated.marker.name, fixedMarker.name);
  assert.strictEqual(recreated.marker.comments, fixedMarker.comments);
  assert.strictEqual(recreated.marker.start, 21.5);
  assert.ok(Math.abs(recreated.marker.end - 27.225) < 0.000001);

  function trackCollection(tracks) {
    tracks.numTracks = tracks.length;
    return tracks;
  }
  const gapItem = Object.assign({}, item, {
    timelineSeconds: 50,
    placeholderIdentity: "RC_BLOCK block-snh97",
    placeholderIdentityRequired: true,
  });
  const beforeGap = {
    nodeId: "before-gap",
    name: "before",
    start: {seconds: 40},
    end: {seconds: 50},
  };
  const afterGap = {
    nodeId: "after-gap",
    name: "after",
    start: {seconds: 55.725},
    end: {seconds: 100},
  };
  const gapTarget = {
    end: {seconds: 100},
    videoTracks: trackCollection([{clips: clips([beforeGap, afterGap])}]),
    audioTracks: trackCollection([{clips: clips([])}]),
  };
  const prepared = hooks.preparedGapForInsertion(
    gapTarget,
    gapItem,
    5.725,
    {expectedTimelineDuration: 100},
  );
  assert.strictEqual(prepared.preparedGap, true);
  assert.strictEqual(prepared.placeholder.startSeconds, 50);
  assert.strictEqual(prepared.placeholder.endSeconds, 55.725);
  assert.throws(
    () => hooks.preparedGapForInsertion(
      gapTarget,
      gapItem,
      5.725,
      {expectedTimelineDuration: 99},
    ),
    /sequence duration does not match/,
  );

  assert.throws(
    () => hooks.conformMarkerState(
      targetWith([marker({comments: marker().comments.replace("1028.400", "1028.401")})]),
      item,
    ),
    /Source sequence\/range does not match/,
  );
  assert.throws(
    () => hooks.conformMarkerState(
      targetWith([marker({comments: marker().comments.replace("точный", "другой")})]),
      item,
    ),
    /item text does not match/,
  );

  const duplicateA = marker();
  const duplicateB = marker({
    start: {seconds: 13},
    end: {seconds: 18.725},
  });
  assert.throws(
    () => hooks.conformMarkerState(targetWith([duplicateA, duplicateB]), item),
    /Expected exactly one marker.*found 2/,
  );

  const stuck = marker({name: "unrelated marker"});
  assert.throws(
    () => hooks.conformMarkerState(targetWith([stuck], () => stuck), item),
    /marker scan did not advance/,
  );

  const createdMarkers = [];
  const markerTarget = {
    markers: {
      createMarker(start) {
        const created = {
          name: "",
          comments: "",
          start: {seconds: start},
          end: {seconds: start},
        };
        createdMarkers.push(created);
        return created;
      },
      getFirstMarker() { return createdMarkers[0] || null; },
      getNextMarker(current) {
        const index = createdMarkers.indexOf(current);
        return index >= 0 ? createdMarkers[index + 1] || null : null;
      },
    },
  };
  const markerWrite = hooks.upsertConformMarker(markerTarget, item);
  assert.strictEqual(markerWrite.ok, true);
  assert.strictEqual(createdMarkers.length, 1);
  assert.strictEqual(createdMarkers[0].name, "RC_SYNC snh97");

  const proxyMarkers = [];
  const proxyTarget = {
    markers: {
      createMarker(start) {
        const stored = {
          name: "",
          comments: "",
          start: {seconds: start},
          end: {seconds: start},
        };
        proxyMarkers.push(stored);
        return stored;
      },
      getFirstMarker() {
        if (!proxyMarkers[0]) return null;
        const stored = proxyMarkers[0];
        return {
          get name() { return stored.name; },
          get comments() { return stored.comments; },
          get start() { return stored.start; },
          get end() { return stored.end; },
        };
      },
      getNextMarker() { return null; },
    },
  };
  const proxyWrite = hooks.upsertConformMarker(proxyTarget, item);
  assert.strictEqual(proxyWrite.ok, true);
  assert.notStrictEqual(proxyWrite.marker, proxyMarkers[0]);
  assert.strictEqual(proxyWrite.marker.name, "RC_SYNC snh97");

  const rejectedMarkers = [];
  const rejectingTarget = {
    markers: {
      createMarker(start) {
        let ignoredName = "";
        const created = {
          comments: "",
          start: {seconds: start},
          end: {seconds: start},
        };
        Object.defineProperty(created, "name", {
          get() { return ignoredName; },
          set() {},
        });
        rejectedMarkers.push(created);
        return created;
      },
      getFirstMarker() { return rejectedMarkers[0] || null; },
      getNextMarker() { return null; },
    },
  };
  const rejectedWrite = hooks.upsertConformMarker(rejectingTarget, item);
  assert.strictEqual(rejectedWrite.ok, false);
  assert.match(rejectedWrite.error, /did not persist the exact completion marker/);
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
  const requestedUrls = [];
  const requests = [];
  let staleJobFallback = false;
  let timelineRefresh = false;
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
    open(method, url) {
      this.method = method;
      this.url = url;
      lastUrl = url;
      requestedUrls.push(url);
    }
    setRequestHeader() {}
    send(body) {
      requests.push({method: this.method, url: this.url, body: body || null});
      this.readyState = 4;
      if (timelineRefresh && /\/stage2\/build$/.test(this.url)) {
        this.status = 200;
        this.responseText = JSON.stringify({ok: true, result: {matched: 9}});
      } else if (timelineRefresh && /\/stage2\/timeline\?project=/.test(this.url)) {
        this.status = 200;
        this.responseText = JSON.stringify({ok: true, result: {segments: []}});
      } else if (staleJobFallback && /\/stage2\/sync-jobs\/job-1$/.test(this.url)) {
        this.status = 404;
        this.responseText = JSON.stringify({ok: false, error: "job not found"});
      } else if (staleJobFallback && /\/stage2\/sync-jobs\/status\?project=/.test(this.url)) {
        this.status = 200;
        this.responseText = JSON.stringify({
          ok: true,
          result: Object.assign({}, job, {id: "job-2", status: "completed", done: 44}),
        });
      } else {
        this.status = 200;
        this.responseText = JSON.stringify({ok: true, result: job});
      }
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

  hooks.renderWhisperJob({
    status: "completed",
    total: 44,
    done: 44,
    needs_review: 3,
    matched: 8,
    unmatched: 33,
  });
  assert.strictEqual(element("whisperReview").textContent, "3");
  assert.strictEqual(element("whisperReviewNoticeCount").textContent, "3");
  assert.strictEqual(element("whisperReviewNotice").hidden, false);

  hooks.renderWhisperJob({
    status: "completed",
    total: 44,
    done: 44,
    needs_review: 0,
    matched: 11,
    unmatched: 33,
  });
  assert.strictEqual(element("whisperReviewNotice").hidden, true);

  hooks.refreshWhisperStatus();
  assert.match(lastUrl, /\/stage2\/sync-jobs\/status\?project=RVK_ULINOVSK$/);
  assert.strictEqual(element("whisperStatus").textContent, "в работе");
  assert.strictEqual(element("whisperSegments").textContent, "11 / 44");
  assert.strictEqual(timers[timers.length - 1].delay, 2500);

  staleJobFallback = true;
  const beforeFallback = requestedUrls.length;
  hooks.refreshWhisperStatus();
  assert.deepStrictEqual(
    requestedUrls.slice(beforeFallback),
    [
      "http://127.0.0.1:8000/stage2/sync-jobs/job-1",
      "http://127.0.0.1:8000/stage2/sync-jobs/status?project=RVK_ULINOVSK",
    ],
  );
  assert.strictEqual(element("whisperStatus").textContent, "готово");
  assert.strictEqual(element("whisperSegments").textContent, "44 / 44");
  assert.strictEqual(element("log").textContent, "");

  const beforeManualRefresh = requestedUrls.length;
  hooks.refreshWhisperStatusManually();
  assert.deepStrictEqual(
    requestedUrls.slice(beforeManualRefresh),
    ["http://127.0.0.1:8000/stage2/sync-jobs/status?project=RVK_ULINOVSK"],
  );
  assert.match(element("log").textContent, /Статус распознавания обновлён/);

  timelineRefresh = true;
  const beforeTimelineRefresh = requests.length;
  let freshTimeline = null;
  hooks.loadFreshTimeline("RVK_ULINOVSK", (error, response) => {
    assert.ifError(error);
    freshTimeline = response.result;
  });
  assert.deepStrictEqual(
    requests.slice(beforeTimelineRefresh).map(({method, url}) => ({method, url})),
    [
      {method: "POST", url: "http://127.0.0.1:8000/stage2/build"},
      {method: "GET", url: "http://127.0.0.1:8000/stage2/timeline?project=RVK_ULINOVSK"},
    ],
  );
  assert.strictEqual(
    JSON.parse(requests[beforeTimelineRefresh].body).project,
    "RVK_ULINOVSK",
  );
  assert.deepStrictEqual(freshTimeline, {segments: []});
}

testPlaceholderSelection();
testCyrillicProjectNames();
testMarkerAwareConformResume();
testWhisperPollingAndEta();
