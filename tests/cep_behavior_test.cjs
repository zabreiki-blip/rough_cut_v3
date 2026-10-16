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

function testSequenceRoleGuards() {
  const context = {
    $: {_roughCutTestMode: true},
    app: {project: {path: "/projects/current.prproj"}},
    console, Date, JSON, Math, decodeURIComponent, encodeURIComponent, isFinite,
    File: function File() {}, Folder: function Folder() {},
    Time: function Time() { this.ticks = "0"; },
  };
  vm.createContext(context);
  vm.runInContext(
    fs.readFileSync(path.join(root, "cep", "host.jsx"), "utf8"),
    context,
    {filename: "host.jsx"},
  );
  const hooks = context.$._roughCutStage2Test;
  function tracks(clips) {
    const clipCollection = clips.slice();
    clipCollection.numItems = clipCollection.length;
    const collection = [{clips: clipCollection}];
    collection.numTracks = 1;
    return collection;
  }
  function markers(values) {
    return {
      get numMarkers() { return values.length; },
      getFirstMarker() { return values[0] || null; },
      getNextMarker(marker) {
        const index = values.indexOf(marker);
        return index >= 0 ? values[index + 1] || null : null;
      },
    };
  }
  function sequence(overrides) {
    return Object.assign({
      sequenceID: "target-id",
      name: "Target",
      end: {seconds: 10},
      zeroPoint: {seconds: 0},
      projectItem: {nodeId: "node-1", treePath: "\\Project\\Target"},
      markers: markers([]),
      videoTracks: tracks([]),
      audioTracks: tracks([]),
      getSettings() { return {}; },
    }, overrides || {});
  }

  const clean = hooks.sequenceRoughCutSignals(sequence());
  assert.strictEqual(clean.inspectionComplete, true);
  assert.strictEqual(clean.isRoughCutTarget, false);

  const prepared = hooks.sequenceRoughCutSignals(sequence({
    markers: {
      getFirstMarker() { throw new Error("slow marker API must not be called"); },
      getNextMarker() { throw new Error("slow marker API must not be called"); },
    },
    videoTracks: tracks([{name: "RC_BLOCK block-7 | Speaker", projectItem: {name: "placeholder.png"}}]),
  }));
  assert.strictEqual(prepared.rcBlockClips, 1);
  assert.strictEqual(prepared.rcSyncMarkers, 0);
  assert.strictEqual(prepared.isRoughCutTarget, true);

  const conformed = hooks.sequenceRoughCutSignals(sequence({
    markers: markers([{name: "RC_SYNC block-7"}]),
  }));
  assert.strictEqual(conformed.rcSyncMarkers, 1);
  assert.strictEqual(conformed.isRoughCutTarget, true);

  let markerScans = 0;
  const cachedEmptyMarkers = {
    numMarkers: 0,
    getFirstMarker() { markerScans += 1; return null; },
    getNextMarker() { return null; },
  };
  const cachedSequence = sequence({
    sequenceID: "cache-clean",
    markers: cachedEmptyMarkers,
  });
  assert.strictEqual(hooks.sequenceRoughCutSignals(cachedSequence).isRoughCutTarget, false);
  assert.strictEqual(hooks.sequenceRoughCutSignals(cachedSequence).isRoughCutTarget, false);
  assert.strictEqual(markerScans, 1, "unchanged sequence should reuse rough-cut signals");

  cachedSequence.videoTracks[0].clips[0] = {
    nodeId: "new-block",
    name: "RC_BLOCK block-new | Speaker",
    projectItem: {name: "placeholder.png"},
  };
  cachedSequence.videoTracks[0].clips.numItems = 1;
  assert.strictEqual(
    hooks.sequenceRoughCutSignals(cachedSequence).isRoughCutTarget,
    true,
    "clip-count changes must invalidate cached clean signals",
  );

  const sameCountSequence = sequence({
    sequenceID: "cache-same-count",
    markers: markers([]),
    videoTracks: tracks([{
      nodeId: "ordinary-node",
      name: "Ordinary clip",
      projectItem: {name: "ordinary.mov"},
    }]),
  });
  assert.strictEqual(hooks.sequenceRoughCutSignals(sameCountSequence).isRoughCutTarget, false);
  sameCountSequence.videoTracks[0].clips[0] = {
    nodeId: "replacement-node",
    name: "RC_BLOCK block-replaced | Speaker",
    projectItem: {name: "placeholder.png"},
  };
  assert.strictEqual(
    hooks.sequenceRoughCutSignals(sameCountSequence).isRoughCutTarget,
    true,
    "same-count boundary replacement must invalidate cached signals",
  );

  const mutableMarkerValues = [];
  const markerChangedSequence = sequence({
    sequenceID: "cache-marker-change",
    markers: markers(mutableMarkerValues),
  });
  assert.strictEqual(hooks.sequenceRoughCutSignals(markerChangedSequence).isRoughCutTarget, false);
  mutableMarkerValues.push({name: "RC_SYNC changed"});
  assert.strictEqual(
    hooks.sequenceRoughCutSignals(markerChangedSequence).isRoughCutTarget,
    true,
    "marker-count changes must not return cached clean signals",
  );

  assert.throws(
    () => hooks.assertSourceIsNotTarget(
      {sequenceID: "same"},
      {sequenceID: "same"},
    ),
    /conform.*саму себя запрещён/,
  );

  const target = sequence();
  function guardList(value) {
    const result = vm.runInContext("[]", context);
    result.push(value);
    return result;
  }
  assert.strictEqual(
    hooks.registeredSourceMatch(target, guardList({
      id: "source-1",
      sequenceId: "target-id",
      projectPath: "/different.prproj",
    })).id,
    "source-1",
  );
  assert.strictEqual(
    hooks.registeredSourceMatch(target, guardList({
      id: "node-collision",
      projectItemNodeId: "node-1",
      projectPath: "/different.prproj",
    })),
    null,
  );
  assert.strictEqual(
    hooks.registeredSourceMatch(target, guardList({
      id: "node-match",
      projectItemNodeId: "node-1",
      projectPath: "/projects/current.prproj",
    })).id,
    "node-match",
  );
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

  const repairItem = Object.assign({}, item, {
    sourceInSeconds: 1009.82,
    sourceOutSeconds: 1018.62,
  });
  const staleVideo = sourceClip("stale-video", {
    start: {seconds: 12},
    end: {seconds: 20.68},
    inPoint: {seconds: 1009.8},
    outPoint: {seconds: 1018.48},
  });
  const staleAudio = sourceClip("stale-audio", {
    start: {seconds: 12},
    end: {seconds: 20.68},
    inPoint: {seconds: 1009.8},
    outPoint: {seconds: 1018.48},
  });
  const stalePair = hooks.staleSourcePairForRepair(
    staleVideo,
    staleAudio,
    sourceItem,
    repairItem,
  );
  assert.strictEqual(stalePair.nodeId, "stale-video");
  assert.strictEqual(stalePair.audioNodeId, "stale-audio");
  assert.ok(Math.abs(stalePair.durationSeconds - 8.68) < 0.000001);
  const markerConfirmedItem = Object.assign({}, repairItem, {
    sourceInSeconds: 32.43,
    sourceOutSeconds: 68.77,
  });
  const markerConfirmedVideo = sourceClip("marker-stale-video", {
    inPoint: {seconds: 32.27},
    outPoint: {seconds: 68.51},
    end: {seconds: 48.24},
  });
  const markerConfirmedAudio = sourceClip("marker-stale-audio", {
    inPoint: {seconds: 32.27},
    outPoint: {seconds: 68.51},
    end: {seconds: 48.24},
  });
  assert.strictEqual(hooks.staleSourcePairForRepair(
    markerConfirmedVideo,
    markerConfirmedAudio,
    sourceItem,
    markerConfirmedItem,
    true,
  ), null);
  assert.strictEqual(hooks.staleSourcePairForRepair(
    markerConfirmedVideo,
    markerConfirmedAudio,
    sourceItem,
    markerConfirmedItem,
    true,
    0.5,
    0.5,
  ).nodeId, "marker-stale-video");
  assert.strictEqual(hooks.staleSourcePairForRepair(
    staleVideo,
    sourceClip("misaligned-audio", {
      start: {seconds: 12.2},
      end: {seconds: 20.88},
      inPoint: {seconds: 1009.8},
      outPoint: {seconds: 1018.48},
    }),
    sourceItem,
    repairItem,
  ), null);
  const globallyShiftedItem = Object.assign({}, repairItem, {
    timelineSeconds: 22.308,
  });
  const globallyShiftedVideo = sourceClip("global-stale-video", {
    start: {seconds: 22.2},
    end: {seconds: 30.88},
    inPoint: {seconds: 1009.8},
    outPoint: {seconds: 1018.48},
  });
  const globallyShiftedAudio = sourceClip("global-stale-audio", {
    start: {seconds: 22.2},
    end: {seconds: 30.88},
    inPoint: {seconds: 1009.8},
    outPoint: {seconds: 1018.48},
  });
  const globalStalePair = hooks.uniqueStaleSourcePairAnywhere(
    insertionTarget([globallyShiftedVideo], [globallyShiftedAudio]),
    globallyShiftedItem,
    sourceItem,
  );
  assert.strictEqual(globalStalePair.nodeId, "global-stale-video");
  assert.strictEqual(globalStalePair.audioNodeId, "global-stale-audio");
  assert.throws(
    () => hooks.uniqueStaleSourcePairAnywhere(
      insertionTarget(
        [globallyShiftedVideo],
        [globallyShiftedAudio, sourceClip("duplicate-stale-audio", {
          start: {seconds: 22.2},
          end: {seconds: 30.88},
          inPoint: {seconds: 1009.8},
          outPoint: {seconds: 1018.48},
        })],
      ),
      globallyShiftedItem,
      sourceItem,
    ),
    /More than one compatible stale V\/A source pair/,
  );

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
  const recoveredAnywhere = hooks.uniqueSourcePairAnywhere(
    insertionTarget([driftVideo], [driftAudio]),
    item,
    sourceItem,
  );
  assert.strictEqual(recoveredAnywhere.videoClip.nodeId, "drift-video");
  assert.strictEqual(recoveredAnywhere.audioClip.nodeId, "drift-audio");

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

  assert.strictEqual(
    hooks.conformMarkerState(
      targetWith([marker({comments: marker().comments.replace("1028.400", "1028.401")})]),
      item,
    ).status,
    "stale_source",
  );
  assert.throws(
    () => hooks.conformMarkerState(
      targetWith([marker({comments: marker().comments.replace(
        "Source: 100626_Васильева",
        "Source: Другая секвенция",
      )})]),
      item,
    ),
    /Source sequence does not match/,
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

function testMarkerNameIndexCaching() {
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
  const hooks = context.$._roughCutStage2Test;

  // --- (a) + (c): a large marker collection scanned by many item lookups
  // against the same target must return results identical to a naive scan,
  // while not re-walking the whole collection for every lookup.
  const N = 40;
  const markerList = [];
  for (let i = 0; i < N; i++) {
    markerList.push({
      name: "RC_SYNC seg-" + i,
      comments: "",
      start: {seconds: i * 10},
      end: {seconds: i * 10 + 5},
    });
  }
  let nextCalls = 0;
  let firstCalls = 0;
  const target = {
    markers: {
      getFirstMarker() {
        firstCalls += 1;
        return markerList[0] || null;
      },
      getNextMarker(current) {
        nextCalls += 1;
        const index = markerList.indexOf(current);
        return index >= 0 ? markerList[index + 1] || null : null;
      },
      createMarker(start) {
        const created = {
          name: "",
          comments: "",
          start: {seconds: start},
          end: {seconds: start},
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

  function naiveMatches(name) {
    return markerList.filter((marker) => marker.name === name);
  }

  // Marker objects cross the vm-context boundary; compare by reference
  // identity and length rather than assert.deepStrictEqual, which also
  // requires the containing Array's prototype to match across realms.
  function assertSameMarkers(actual, expected) {
    assert.strictEqual(actual.length, expected.length);
    for (let i = 0; i < expected.length; i++) {
      assert.strictEqual(actual[i], expected[i]);
    }
  }

  for (let i = 0; i < N; i += 7) {
    const name = "RC_SYNC seg-" + i;
    assertSameMarkers(hooks.exactMarkersNamed(target, name), naiveMatches(name));
  }
  assert.strictEqual(hooks.exactMarkersNamed(target, "RC_SYNC absent").length, 0);

  nextCalls = 0;
  firstCalls = 0;
  const M = 30;
  for (let i = 0; i < M; i++) {
    hooks.exactMarkersNamed(target, "RC_SYNC seg-" + (i % N));
  }
  assert.ok(
    nextCalls <= N,
    "expected a single collection scan (<= " + N +
      " getNextMarker calls) across " + M + " item lookups against the " +
      "same target, got " + nextCalls,
  );
  assert.ok(
    firstCalls <= 1,
    "expected a single getFirstMarker call across " + M +
      " item lookups, got " + firstCalls,
  );

  // (b) upsertConformMarker's own createMarker call must be visible to the
  // very next lookup against the same target.
  const newItem = {
    segmentId: "seg-new",
    speaker: "S",
    text: "text",
    sourceSequenceName: "src",
    sourceInSeconds: 0,
    sourceOutSeconds: 5,
    timelineSeconds: 900,
  };
  const write = hooks.upsertConformMarker(target, newItem);
  assert.strictEqual(write.ok, true);
  const afterCreate = hooks.exactMarkersNamed(target, "RC_SYNC seg-new");
  assertSameMarkers(afterCreate, naiveMatches("RC_SYNC seg-new"));
  assert.strictEqual(afterCreate.length, 1);
  assert.strictEqual(afterCreate[0], write.marker);

  // --- (b): reanchorConformMarker's delete-then-recreate fallback must
  // also be visible to the very next lookup, and must not keep returning
  // the deleted marker instance from a stale cached index.
  function fallbackMarker() {
    return Object.assign({
      name: "RC_SYNC seg-b",
      comments: "orig",
      start: {seconds: 12},
      end: {seconds: 17},
    }, {});
  }
  const fixedStart = 12;
  const fixedEnd = 17;
  const fixedMarker = fallbackMarker();
  Object.defineProperties(fixedMarker, {
    start: {get() { return {seconds: fixedStart}; }, set() {}, configurable: true},
    end: {get() { return {seconds: fixedEnd}; }, set() {}, configurable: true},
  });
  const markerList2 = [fixedMarker];
  const target2 = {
    markers: {
      getFirstMarker() { return markerList2[0] || null; },
      getNextMarker(current) {
        const index = markerList2.indexOf(current);
        return index >= 0 ? markerList2[index + 1] || null : null;
      },
      createMarker(start) {
        const created = {name: "", comments: "", start, end: start};
        markerList2.push(created);
        return created;
      },
      deleteMarker(value) {
        const index = markerList2.indexOf(value);
        if (index < 0) return false;
        markerList2.splice(index, 1);
        return true;
      },
    },
  };
  const itemB = {
    segmentId: "seg-b",
    sourceInSeconds: 0,
    sourceOutSeconds: 5,
    timelineSeconds: 12,
  };

  // Warm the index cache before the mutation so a missing invalidation
  // would reproduce as a stale-marker bug below.
  const beforeReanchor = hooks.exactMarkersNamed(target2, "RC_SYNC seg-b");
  assert.strictEqual(beforeReanchor.length, 1);
  assert.strictEqual(beforeReanchor[0], fixedMarker);

  const reanchored = hooks.reanchorConformMarker(target2, fixedMarker, itemB, 21.5);
  assert.strictEqual(reanchored.report.recreated, true);

  const afterReanchor = hooks.exactMarkersNamed(target2, "RC_SYNC seg-b");
  assert.strictEqual(
    afterReanchor.length,
    1,
    "index must reflect delete+recreate, not a stale cached entry",
  );
  assert.strictEqual(afterReanchor[0], reanchored.marker);
  assert.notStrictEqual(afterReanchor[0], fixedMarker);
  assert.strictEqual(markerList2.indexOf(fixedMarker), -1);
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
  assert.match(
    hooks.sourceRegistrationBlockReason({
      roughCutGuard: {inspectionComplete: true, isRoughCutTarget: true},
    }),
    /RC_BLOCK\/RC_SYNC/,
  );
  assert.strictEqual(
    hooks.sourceRegistrationBlockReason({
      roughCutGuard: {inspectionComplete: true, isRoughCutTarget: false},
    }),
    "",
  );
  const targetIdentity = {
    sequenceId: "target-sequence",
    projectPath: "/projects/current.prproj",
    projectItemNodeId: "shared-node",
    projectItemTreePath: "\\Project\\Target",
  };
  assert.strictEqual(hooks.sourceIdentityMatchesSequence({
    sequence_id: "target-sequence",
    project_path: "/different.prproj",
    metadata: {},
  }, targetIdentity), true);
  assert.strictEqual(hooks.sourceIdentityMatchesSequence({
    project_path: "/different.prproj",
    metadata: {project_item_node_id: "shared-node"},
  }, targetIdentity), false);
  assert.strictEqual(hooks.sourceIdentityMatchesSequence({
    project_path: "/projects/current.prproj",
    metadata: {project_item_node_id: "shared-node"},
  }, targetIdentity), true);

  element("projectName").value = "RVK_ULINOVSK";
  element("apiBaseUrl").value = "http://127.0.0.1:8000";
  element("apiToken").value = "memory-only-token";
  element("projectsRoot").value = "/projects";
  element("transcribePath").value = "/stage2/sync-jobs/start";

  for (let i = 0; i < 40; i += 1) {
    hooks.log("diagnostic " + i, {payload: "x".repeat(8000)});
  }
  assert.ok(
    element("log").textContent.length <= 100000,
    "the long-lived CEP log must remain bounded",
  );
  assert.match(element("log").textContent, /diagnostic 39/);
  assert.match(element("log").textContent, /запись журнала сокращена/);
  element("log").textContent = "";

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

function testVisibilityPausesBackgroundPolling() {
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
  const requestedUrls = [];
  let clearTimeoutCalls = 0;
  const evalScriptCalls = [];
  let fileSizeCounter = 0;

  const job = {
    id: "job-1",
    status: "running",
    message: "Matching scenario quotes",
    total: 44,
    done: 11,
  };

  class FakeXHR {
    open(method, url) {
      this.method = method;
      this.url = url;
      requestedUrls.push(url);
    }
    setRequestHeader() {}
    send() {
      this.readyState = 4;
      this.status = 200;
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
  // The panel keeps polling the backend even while backgrounded/minimized.
  // CSInterface.evalScript is only exercised here to drive pollExport's
  // "guide WAV not stable yet" branch, so the fake never stabilizes and the
  // export poll keeps re-arming its timer until we pause/resume it.
  context.CSInterface = function CSInterface() {
    this.evalScript = function (script, callback) {
      evalScriptCalls.push(script);
      fileSizeCounter += 1;
      callback(JSON.stringify({
        ok: true,
        file: {exists: true, size: fileSizeCounter, modifiedMs: fileSizeCounter, path: "/tmp/guide.wav"},
      }));
    };
  };
  context.localStorage = {
    values: {},
    getItem(key) { return this.values[key] || null; },
    setItem(key, value) { this.values[key] = String(value); },
  };
  context.setTimeout = function setTimeoutFake(callback, delay) {
    timers.push({callback, delay});
    return timers.length;
  };
  context.clearTimeout = function clearTimeoutFake() {
    clearTimeoutCalls += 1;
  };
  context.document = {
    readyState: "loading",
    hidden: false,
    addEventListener() {},
    createElement() { return new Element(); },
    getElementById(id) {
      if (!elements[id]) elements[id] = new Element();
      return elements[id];
    },
  };

  const mainJsSource = fs.readFileSync(path.join(root, "cep", "js", "main.js"), "utf8");
  // `bind()` (run from `init()` at load time in the real panel) must wire the
  // exported handleVisibilityChange hook to the real event so this pause
  // actually engages outside of tests; the behavioural assertions below
  // exercise that same hook directly, mirroring what the listener calls.
  assert.match(
    mainJsSource,
    /addEventListener\(\s*"visibilitychange"\s*,\s*handleVisibilityChange\s*\)/,
    "the panel must subscribe handleVisibilityChange to document visibilitychange",
  );

  vm.createContext(context);
  vm.runInContext(mainJsSource, context, {filename: "main.js"});
  const hooks = context.__ROUGH_CUT_TEST_HOOKS__;
  const element = (id) => context.document.getElementById(id);

  element("projectName").value = "RVK_ULINOVSK";
  element("apiBaseUrl").value = "http://127.0.0.1:8000";
  element("apiToken").value = "memory-only-token";

  // --- whisper status polling ---
  hooks.refreshWhisperStatus();
  const requestsAfterInitialRefresh = requestedUrls.length;
  assert.strictEqual(requestsAfterInitialRefresh, 1);
  assert.strictEqual(timers.length, 1, "a poll timer must be scheduled after a status refresh");

  context.document.hidden = true;
  hooks.handleVisibilityChange();
  assert.ok(
    clearTimeoutCalls >= 1,
    "hiding the panel must clear the pending whisper poll timer",
  );
  const timersAfterHide = timers.length;
  const requestsAfterHide = requestedUrls.length;

  // Simulate real time passing while the panel stays hidden: nothing new
  // should be scheduled or requested because there is no live timer left.
  assert.strictEqual(timers.length, timersAfterHide);
  assert.strictEqual(requestedUrls.length, requestsAfterHide);

  context.document.hidden = false;
  hooks.handleVisibilityChange();
  assert.strictEqual(
    requestedUrls.length,
    requestsAfterHide + 1,
    "returning to the foreground must trigger an immediate status refresh",
  );
  assert.strictEqual(
    timers.length,
    timersAfterHide + 1,
    "returning to the foreground must resume the normal poll cycle",
  );

  // --- export polling ---
  hooks.pollExport("/tmp/guide.wav", "job-export-1");
  const exportTimerIndex = timers.length - 1;
  timers[exportTimerIndex].callback();
  assert.strictEqual(evalScriptCalls.length, 1, "the first export check must run once armed");
  const attemptsBeforeHide = evalScriptCalls.length;
  const timerCountBeforeExportHide = timers.length;

  context.document.hidden = true;
  hooks.handleVisibilityChange();
  assert.strictEqual(
    evalScriptCalls.length,
    attemptsBeforeHide,
    "hiding the panel must not consume export-poll attempts",
  );
  assert.strictEqual(
    timers.length,
    timerCountBeforeExportHide,
    "hiding the panel must not arm a new export-poll timer",
  );

  context.document.hidden = false;
  hooks.handleVisibilityChange();
  assert.strictEqual(
    evalScriptCalls.length,
    attemptsBeforeHide + 1,
    "returning to the foreground must immediately resume the export check " +
      "from where it left off, without wasting the attempt budget on hidden time",
  );
}

function testVisibilityRaceDiscardsStaleInFlightRequests() {
  // Reproduces the race an independent review flagged in
  // pauseBackgroundPolling: it only clears *pending* timers, so a request
  // that is already in flight (callHost round-trip or XHR) when the panel
  // is hidden can still land afterwards and, unguarded, would re-arm a
  // timer behind the pause's back or even double-fire pollExport's
  // terminal branch. Unlike the other polling test, the fakes here hold
  // every request/host call open until the test resolves it manually, so
  // the exact hide/resolve/show ordering described in the race can be
  // driven deterministically.
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
  const pendingXhr = [];
  const pendingEval = [];

  const job = {
    id: "job-1",
    status: "running",
    message: "Matching scenario quotes",
    total: 44,
    done: 11,
  };

  class FakeXHR {
    open(method, url) {
      this.method = method;
      this.url = url;
    }
    setRequestHeader() {}
    send() {
      const self = this;
      pendingXhr.push({
        url: self.url,
        resolve(status, body) {
          self.readyState = 4;
          self.status = status;
          self.responseText = JSON.stringify(body);
          self.onreadystatechange();
        },
      });
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
  context.CSInterface = function CSInterface() {
    this.evalScript = function (script, callback) {
      pendingEval.push({
        script,
        resolve(fileObj) {
          callback(JSON.stringify({ok: true, file: fileObj}));
        },
      });
    };
  };
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
    hidden: false,
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

  element("projectName").value = "RVK_ULINOVSK";
  element("apiBaseUrl").value = "http://127.0.0.1:8000";
  element("apiToken").value = "memory-only-token";

  // --- whisper status poll: response arrives for a request that started
  // before the panel was hidden ---
  hooks.refreshWhisperStatus();
  assert.strictEqual(pendingXhr.length, 1, "the status request must be sent immediately");
  assert.strictEqual(timers.length, 0, "no poll timer may exist before the in-flight request resolves");

  context.document.hidden = true;
  hooks.handleVisibilityChange();

  pendingXhr.shift().resolve(200, {ok: true, result: job});
  assert.strictEqual(
    timers.length,
    0,
    "a stale in-flight response must not arm a poll timer while hidden",
  );
  assert.strictEqual(
    pendingXhr.length,
    0,
    "a stale in-flight response must not trigger a new request",
  );

  context.document.hidden = false;
  hooks.handleVisibilityChange();
  assert.strictEqual(
    pendingXhr.length,
    1,
    "returning to the foreground must trigger exactly one immediate refresh",
  );

  pendingXhr.shift().resolve(200, {ok: true, result: job});
  assert.strictEqual(timers.length, 1, "the resumed refresh must arm exactly one poll timer");
  assert.strictEqual(pendingXhr.length, 0);

  // A redundant visibilitychange -> visible (no hide in between) must be a
  // no-op: the WasActive flag was already consumed above.
  hooks.handleVisibilityChange();
  assert.strictEqual(pendingXhr.length, 0, "a redundant resume must not send another request");
  assert.strictEqual(timers.length, 1, "a redundant resume must not arm another timer");

  // --- export poll: a stale in-flight "guide WAV is stable now" response
  // must not double-fire registerSequenceWithCallback/startTranscriptionJob ---
  hooks.pollExport("/tmp/guide.wav", "job-export-1");
  const exportTimersStart = timers.length;
  assert.strictEqual(exportTimersStart, 2, "pollExport must arm its own initial timer");

  function fireNextExportCheck() {
    timers[timers.length - 1].callback();
  }
  function resolveNextEval(fileObj) {
    pendingEval.shift().resolve(fileObj);
  }
  const stableFile = {exists: true, size: 100, modifiedMs: 100, path: "/tmp/guide.wav"};

  // Three consecutive identical checks build stableChecks up to 2; a fourth
  // identical check is what would flip it to the completion threshold (3).
  fireNextExportCheck();
  resolveNextEval(stableFile);
  fireNextExportCheck();
  resolveNextEval(stableFile);
  fireNextExportCheck();
  resolveNextEval(stableFile);
  assert.strictEqual(hooks.getExportPollTerminalCallCount(), 0);

  // The fourth check is sent (this is the one that would complete the
  // export)...
  fireNextExportCheck();
  assert.strictEqual(pendingEval.length, 1);

  // ...but the panel is hidden before its callHost round-trip returns.
  context.document.hidden = true;
  hooks.handleVisibilityChange();
  const timersBeforeStaleResolve = timers.length;
  resolveNextEval(stableFile);
  assert.strictEqual(
    hooks.getExportPollTerminalCallCount(),
    0,
    "a stale in-flight export check must not complete the export while hidden",
  );
  assert.strictEqual(
    timers.length,
    timersBeforeStaleResolve,
    "a stale in-flight export check must not arm a new export-poll timer",
  );

  // Resuming must run exactly one fresh check, which is the one that
  // actually reaches the stable-file threshold.
  context.document.hidden = false;
  hooks.handleVisibilityChange();
  assert.strictEqual(pendingEval.length, 1, "resume must issue exactly one export check");
  resolveNextEval(stableFile);
  assert.strictEqual(
    hooks.getExportPollTerminalCallCount(),
    1,
    "the export completion must fire exactly once despite the race",
  );
}

testPlaceholderSelection();
testCyrillicProjectNames();
testSequenceRoleGuards();
testMarkerAwareConformResume();
testMarkerNameIndexCaching();
testWhisperPollingAndEta();
testVisibilityPausesBackgroundPolling();
testVisibilityRaceDiscardsStaleInFlightRequests();


// The panel runs unchanged in a VM. Only the external CEP, XHR and timer
// boundaries are controlled, so races can be resolved in a deliberate order.
function finalReviewHarness() {
  const elements = {};
  const requests = [];
  const hosts = [];
  const timers = new Map();
  const confirmations = [];
  const events = {};
  const storage = {
    "roughCutStage2.project": "demo",
    "roughCutStage2.projectsRoot": "/projects",
  };
  let timerId = 0;
  class Element {
    constructor() {
      this.value = "";
      this.textContent = "";
      this.disabled = false;
      this.hidden = false;
      this.listeners = {};
      this.style = {};
      this.children = [];
      this.firstChild = null;
    }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    appendChild(child) { this.children.push(child); this.firstChild = this.children[0]; }
    removeChild(child) { this.children = this.children.filter((item) => item !== child); this.firstChild = this.children[0] || null; }
    setAttribute() {}
    removeAttribute() {}
  }
  class XHR {
    open(method, url) { this.method = method; this.url = url; }
    setRequestHeader() {}
    send(body) {
      const xhr = this;
      requests.push({
        method: this.method,
        url: this.url,
        body: body && JSON.parse(body),
        resolve(result, status = 200) {
          xhr.readyState = 4;
          xhr.status = status;
          xhr.responseText = JSON.stringify(status < 300 ? {ok: true, result} : {ok: false, error: result});
          xhr.onreadystatechange();
        },
        fail() { xhr.onerror(); },
        nonJson(status) {
          xhr.readyState = 4; xhr.status = status; xhr.responseText = "<html>Backend down</html>";
          xhr.onreadystatechange();
        },
      });
    }
  }
  const context = {
    console, Date, JSON, Math, Array, Error, isFinite, encodeURIComponent,
    decodeURIComponent, XMLHttpRequest: XHR, __ROUGH_CUT_TEST_MODE__: true,
    document: {
      readyState: "loading", hidden: false,
      addEventListener(name, callback) { events[name] = callback; },
      createElement() { return new Element(); },
      getElementById(id) { return elements[id] || (elements[id] = new Element()); },
    },
    localStorage: {getItem(key) { return storage[key] || null; }, setItem(key, value) { storage[key] = value; }},
    setTimeout(callback, delay) {
      const id = ++timerId;
      timers.set(id, {callback, delay});
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
    confirm(message) { confirmations.push(message); return context.acceptConfirmation; },
    acceptConfirmation: true,
    CSInterface: function () {
      this.evalScript = function (script, callback) {
        hosts.push({script, resolve(result) { callback(JSON.stringify(Object.assign({ok: true}, result))); }});
      };
    },
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(root, "cep", "js", "main.js"), "utf8"), context);
  const element = (id) => context.document.getElementById(id);
  element("projectName").value = "demo";
  element("apiBaseUrl").value = "http://localhost:8000";
  element("apiToken").value = "memory-only-token";
  element("projectsRoot").value = "/projects";
  element("applyFinalReview").disabled = true;
  element("retryFinalReview").hidden = true;
  element("cancelFinalReview").hidden = true;
  return {
    context, hooks: context.__ROUGH_CUT_TEST_HOOKS__, requests, hosts, timers,
    confirmations, element, initialize() { events.DOMContentLoaded(); },
    fireTimer() {
      assert.strictEqual(timers.size, 1, "only one final-review timer may be armed");
      const [id, timer] = timers.entries().next().value;
      timers.delete(id);
      timer.callback();
      return timer.delay;
    },
  };
}

function finalReviewJob(status = "running", extra = {}) {
  return Object.assign({
    id: "review-1", project: "demo", status, message: "Provider progress",
    source_sequence: {sequenceId: "sequence-1", durationSeconds: 90},
    estimated_cost_usd: "0.17", cost_limit_usd: 1,
    cost_usd: 0, reserved_cost_usd: 0, cost_pending: false,
  }, extra);
}

function restoreReview(h, status = "running") {
  assert.strictEqual(typeof h.hooks.restoreFinalReview, "function", "final-review recovery must be exposed");
  h.hooks.restoreFinalReview();
  assert.match(h.requests[0].url, /\/stage2\/final-review\/latest\?project=demo$/);
  h.requests[0].resolve(finalReviewJob(status));
}

function testFinalReviewRestoredJobsDisplayConfirmedAndPendingCosts() {
  const cases = [
    {status: "completed", cost: 0.04, reserve: 0, pending: false, amount: /\$0\.04/},
    {status: "running", cost: 0.02, reserve: 0.15, pending: true, amount: /\$0\.02/, reserved: /\$0\.15/},
    {status: "running", cost: null, reserve: 0, pending: true, amount: /неизвестна/i, reserved: /\$0(?:\D|$)/},
    {status: "cancelled", cost: 0.03, reserve: 0.06, pending: true, amount: /\$0\.03/, reserved: /\$0\.06/, warning: true},
    {status: "timed_out", cost: 0, reserve: 0, pending: true, amount: /\$0(?:\D|$)/, reserved: /\$0(?:\D|$)/, warning: true},
    {status: "failed", error: "Final review whole-job deadline exceeded", cost: 0.03, reserve: 0.06, pending: true, amount: /\$0\.03/, reserved: /\$0\.06/, warning: true},
    {status: "failed", error: "Provider request timeout", cost: 0, reserve: 0, pending: true, amount: /\$0(?:\D|$)/, reserved: /\$0(?:\D|$)/, warning: true},
    {status: "failed", error: "Cloud ASR transcription exceeded its deadline", cost: 0.03, reserve: 0.06, pending: true, amount: /\$0\.03/, reserved: /\$0\.06/, warning: true},
    {status: "failed", error: "Whisper transcription exceeded 120 seconds", cost: 0.03, reserve: 0.06, pending: true, amount: /\$0\.03/, reserved: /\$0\.06/, warning: true},
    {status: "failed", error: "Invalid audio file", cost: 0, reserve: 0, pending: false, amount: /\$0(?:\D|$)/},
  ];
  for (const item of cases) {
    const h = finalReviewHarness();
    h.element("finalReviewCost").textContent = "Stale preflight estimate $9.99";
    h.hooks.restoreFinalReview();
    h.requests[0].resolve(finalReviewJob(item.status, {
      cost_usd: item.cost, reserved_cost_usd: item.reserve, cost_pending: item.pending, error: item.error,
    }));
    const cost = h.element("finalReviewCost").textContent;
    assert.match(cost, /подтвержд[её]нная стоимость/i,
      item.status + " recovery must display confirmed accounting instead of the old estimate");
    assert.match(cost, item.amount);
    assert.doesNotMatch(cost, /9\.99/);
    if (item.pending) {
      assert.match(cost, /ожидает подтверждения/i, "pending cost must remain visible even when reserve is zero");
      assert.match(cost, /резерв/i);
      assert.match(cost, item.reserved);
    } else {
      assert.doesNotMatch(cost, /ожидает подтверждения/i);
    }
    if (item.warning) {
      assert.match(cost, /уже отправлен/i,
        (item.error || item.status) + " must warn that provider work may still be billed");
      assert.match(cost, /провайдер/i);
      assert.match(cost, /оплачен/i);
      assert.strictEqual(h.timers.size, 0, item.status + " must stop status polling");
    } else {
      assert.doesNotMatch(cost, /уже отправлен/i,
        "a failure unrelated to timeout must keep its existing accounting message");
    }
  }
}

function testFinalReviewPolledJobRefreshesAccounting() {
  const h = finalReviewHarness();
  restoreReview(h);
  h.fireTimer();
  h.requests[1].resolve(finalReviewJob("running", {
    cost_usd: 0.02, reserved_cost_usd: 0.15, cost_pending: true,
  }));
  assert.match(h.element("finalReviewCost").textContent, /\$0\.02/);
  assert.match(h.element("finalReviewCost").textContent, /\$0\.15/);
  assert.match(h.element("finalReviewCost").textContent, /ожидает подтверждения/i);
  h.fireTimer();
  h.requests[2].resolve(finalReviewJob("completed", {
    cost_usd: 0.12, reserved_cost_usd: 0, cost_pending: false,
  }));
  const settled = h.element("finalReviewCost").textContent;
  assert.match(settled, /\$0\.12/);
  assert.doesNotMatch(settled, /ожидает подтверждения|\$0\.15/i,
    "settled polling must replace the previous pending reserve");
}

function testFinalReviewBackoffAndManualRetry() {
  const h = finalReviewHarness();
  restoreReview(h);
  assert.strictEqual(h.fireTimer(), 3000);
  for (const delay of [3000, 6000, 12000, 24000, 30000]) {
    const req = h.requests[h.requests.length - 1];
    assert.match(req.url, /\/jobs\/review-1\?project=demo$/);
    req.fail();
    assert.strictEqual(h.fireTimer(), delay);
  }
  h.requests[h.requests.length - 1].fail();
  assert.strictEqual(h.timers.size, 0, "retry exhaustion must stop polling");
  assert.strictEqual(h.element("retryFinalReview").hidden, false);
  assert.strictEqual(h.hooks.getFinalReviewState().job.id, "review-1");
  const before = h.requests.length;
  h.hooks.retryFinalReview();
  h.hooks.retryFinalReview();
  assert.strictEqual(h.requests.length, before + 1, "manual Retry must issue exactly one status request");
  h.requests[h.requests.length - 1].resolve(finalReviewJob());
  assert.strictEqual(h.hooks.getFinalReviewState().retryIndex, 0);
  assert.strictEqual(h.element("retryFinalReview").hidden, true);
  assert.ok(h.requests.every((r) => r.method === "GET"), "recovery and Retry never start provider work");
}

function testFinalReviewRecoveryChecksHostBeforeApply() {
  const h = finalReviewHarness();
  restoreReview(h, "completed");
  assert.strictEqual(h.timers.size, 0);
  assert.strictEqual(h.element("applyFinalReview").disabled, true);
  assert.match(h.hosts[0].script, /^rcStage2_inspectActiveSequence\(/);
  h.hosts[0].resolve({sequence: {sequenceId: "wrong-sequence"}});
  assert.strictEqual(h.element("applyFinalReview").disabled, true);
  assert.match(h.element("finalReviewStatus").textContent, /sequence-1/);
  assert.match(h.element("finalReviewStatus").textContent, /не совпадает/);
  h.hooks.applyFinalReview();
  assert.strictEqual(h.requests.length, 1, "sequence mismatch must block the Apply API too");
  h.hosts[h.hosts.length - 1].resolve({sequence: {sequenceId: "sequence-1"}});
  assert.strictEqual(h.requests.length, 2, "Apply must inspect again before sending the mutation");
  assert.match(h.requests[1].url, /\/apply$/);
  assert.ok(h.requests.every((r) => !/\/start$/.test(r.url)));

  for (const status of ["failed", "interrupted", "cancelled"]) {
    const terminal = finalReviewHarness();
    restoreReview(terminal, status);
    assert.strictEqual(terminal.timers.size, 0, status + " must be terminal for polling");
    assert.strictEqual(terminal.element("applyFinalReview").disabled, true);
    assert.strictEqual(terminal.hosts.length, 0);
  }
}

function testFinalReviewCancelRejectsOlderStatusAndInspection() {
  const h = finalReviewHarness();
  restoreReview(h);
  h.fireTimer();
  const stale = h.requests[1];
  const before = h.hooks.getFinalReviewState().generation;
  h.hooks.cancelFinalReview();
  assert.strictEqual(h.hooks.getFinalReviewState().generation, before + 1);
  const cancel = h.requests[2];
  assert.strictEqual(cancel.method, "POST");
  assert.match(cancel.url, /\/jobs\/review-1\/cancel\?project=demo$/);
  assert.match(h.element("finalReviewStatus").textContent, /отмен/);
  stale.resolve(finalReviewJob("completed"));
  assert.strictEqual(h.hosts.length, 0);
  assert.strictEqual(h.element("applyFinalReview").disabled, true);
  cancel.resolve(finalReviewJob("cancelling"), 202);
  assert.match(h.element("finalReviewStatus").textContent, /отмен/);
  h.fireTimer();
  h.requests[3].resolve(finalReviewJob("cancelled"));
  assert.strictEqual(h.timers.size, 0);
  assert.strictEqual(h.element("cancelFinalReview").hidden, true);

  const completed = finalReviewHarness();
  restoreReview(completed, "completed");
  const oldInspection = completed.hosts[0];
  completed.hooks.finalReviewProjectChanged();
  oldInspection.resolve({sequence: {sequenceId: "sequence-1"}});
  assert.strictEqual(completed.element("applyFinalReview").disabled, true);
}

function testFinalReviewVisibilityAndProjectGeneration() {
  const h = finalReviewHarness();
  restoreReview(h);
  const oldTimer = h.timers.values().next().value;
  const generation = h.hooks.getFinalReviewState().generation;
  h.context.document.hidden = true;
  h.hooks.handleVisibilityChange();
  assert.strictEqual(h.timers.size, 0);
  assert.strictEqual(h.hooks.getFinalReviewState().generation, generation + 1);
  assert.strictEqual(h.hooks.getFinalReviewState().visible, false);
  oldTimer.callback();
  assert.strictEqual(h.requests.length, 1, "a cleared stale timer must be inert");
  h.context.document.hidden = false;
  h.hooks.handleVisibilityChange();
  h.hooks.handleVisibilityChange();
  assert.strictEqual(h.requests.length, 2, "show must perform exactly one immediate status refresh");
  const oldRequest = h.requests[1];
  const beforeProject = h.hooks.getFinalReviewState().generation;
  h.element("projectName").value = "another-project";
  h.hooks.finalReviewProjectChanged();
  assert.strictEqual(h.hooks.getFinalReviewState().generation, beforeProject + 1);
  assert.strictEqual(h.hooks.getFinalReviewState().job, null);
  assert.match(h.requests[2].url, /latest\?project=another-project$/);
  oldRequest.resolve(finalReviewJob("completed"));
  assert.strictEqual(h.hosts.length, 0);
  assert.strictEqual(h.timers.size, 0);
  assert.strictEqual(h.element("applyFinalReview").disabled, true);
  h.requests[2].resolve("job not found", 404);
  assert.strictEqual(h.timers.size, 0);
  assert.strictEqual(h.element("retryFinalReview").hidden, true);
}

function testFinalReviewHideDiscardsInFlightStatusAndResumesOnce() {
  const h = finalReviewHarness();
  h.hooks.restoreFinalReview();
  const stale = h.requests[0];
  h.context.document.hidden = true;
  h.hooks.handleVisibilityChange();
  h.context.document.hidden = false;
  h.hooks.handleVisibilityChange();
  assert.strictEqual(h.requests.length, 2);
  stale.resolve(finalReviewJob("completed"));
  assert.strictEqual(h.hosts.length, 0);
  assert.strictEqual(h.timers.size, 0);
  h.requests[1].resolve(finalReviewJob());
  assert.strictEqual(h.timers.size, 1);
  h.hooks.handleVisibilityChange();
  assert.strictEqual(h.requests.length, 2, "stale response must not rearm the resume flag");
}

function recoverEmptyFinalReview(h) {
  h.hooks.restoreFinalReview();
  h.requests[0].resolve("job not found", 404);
  h.requests.length = 0;
}

function prepareFinalReviewEstimate(h) {
  recoverEmptyFinalReview(h);
  const before = h.hooks.getFinalReviewState().generation;
  h.hooks.startFinalReviewFromWav();
  assert.strictEqual(h.hooks.getFinalReviewState().generation, before + 1);
  h.hosts[0].resolve({sequence: {sequenceId: "sequence-1", durationSeconds: 90}});
  h.hosts[1].resolve({layout: {entries: []}});
  assert.strictEqual(h.requests.length, 1, "Estimate must be the first backend request");
  assert.match(h.requests[0].url, /\/final-review\/estimate$/);
  assert.strictEqual(h.confirmations.length, 0, "confirmation must wait for Estimate");
}

function testFinalReviewEstimateConfirmationAnd409() {
  const h = finalReviewHarness();
  prepareFinalReviewEstimate(h);
  const exact = "0.1700000000000000028333333333";
  h.requests[0].resolve({estimated_cost_usd: exact, enforced: false});
  assert.strictEqual(h.confirmations.length, 1);
  assert.ok(h.confirmations[0].includes(exact));
  assert.match(h.element("finalReviewCost").textContent, /не.*лимит/i);
  assert.match(h.requests[1].url, /\/final-review\/start$/);
  assert.strictEqual(h.requests[1].body.confirmed_estimate_usd, exact);
  h.requests[1].resolve("cloud ASR estimate changed; confirm the new estimate", 409);
  assert.strictEqual(h.requests.length, 3, "stale estimate must obtain a fresh Estimate");
  assert.match(h.requests[2].url, /\/estimate$/);
  h.context.acceptConfirmation = false;
  h.requests[2].resolve({estimated_cost_usd: "0.2", enforced: true, cost_limit_usd: 1});
  assert.strictEqual(h.confirmations.length, 2);
  assert.strictEqual(h.requests.filter((r) => /\/start$/.test(r.url)).length, 1, "declined new estimate cannot start work");
  assert.strictEqual(h.element("startFinalReviewFromWav").disabled, false);
  for (const estimate of [{estimated_cost_usd: "0", enforced: true}, {enforced: true}]) {
    const unknown = finalReviewHarness();
    prepareFinalReviewEstimate(unknown);
    unknown.context.acceptConfirmation = false;
    unknown.requests[0].resolve(estimate);
    assert.match(unknown.element("finalReviewCost").textContent, /не.*лимит/i);
    assert.strictEqual(unknown.requests.length, 1);
  }
}

function testFinalReviewStartAndEstimateDiscardStaleCallbacks() {
  const h = finalReviewHarness();
  restoreReview(h);
  h.fireTimer();
  const staleStatus = h.requests[1];
  h.hooks.startFinalReviewFromWav();
  staleStatus.resolve(finalReviewJob("completed"));
  assert.strictEqual(h.hosts.length, 1, "stale completion cannot trigger host validation after Start");
  assert.strictEqual(h.timers.size, 0);
  const staleInspect = h.hosts[0];
  h.context.document.hidden = true;
  h.hooks.handleVisibilityChange();
  staleInspect.resolve({sequence: {sequenceId: "sequence-1"}});
  assert.strictEqual(h.hosts.length, 1, "stale source inspection cannot continue Start");

  const estimate = finalReviewHarness();
  prepareFinalReviewEstimate(estimate);
  estimate.hooks.finalReviewProjectChanged();
  estimate.requests[0].resolve({estimated_cost_usd: "0.17", enforced: true});
  assert.strictEqual(estimate.confirmations.length, 0, "old project cannot show a cost confirmation");
  assert.ok(estimate.requests.every((r) => !/\/start$/.test(r.url)));
}

function testFinalReviewApplyNetworkRetryKeepsRequestIdAndSequenceGuard() {
  const h = finalReviewHarness();
  restoreReview(h, "completed");
  h.hosts[0].resolve({sequence: {sequenceId: "sequence-1"}});
  assert.strictEqual(h.element("applyFinalReview").disabled, false);
  h.hooks.applyFinalReview();
  assert.strictEqual(h.element("applyFinalReview").disabled, true);
  h.hosts[1].resolve({sequence: {sequenceId: "sequence-1"}});
  const firstApply = h.requests[1];
  assert.ok(firstApply.body.request_id, "one logical Apply needs a backend idempotency key");
  firstApply.fail();
  assert.strictEqual(h.element("retryFinalReview").hidden, false);
  h.hooks.retryFinalReview();
  assert.match(h.hosts[2].script, /^rcStage2_inspectActiveSequence\(/);
  h.hosts[2].resolve({sequence: {sequenceId: "sequence-1"}});
  const retriedApply = h.requests[2];
  assert.strictEqual(retriedApply.body.request_id, firstApply.body.request_id);
  assert.strictEqual(retriedApply.body.project, "demo");
  retriedApply.resolve({iteration: {id: "iteration-1"}, corrections: []});
  assert.match(h.hosts[3].script, /^rcStage2_cloneActiveSequence\(/);
  h.hosts[3].resolve({draft: {sequenceId: "draft-1", name: "Draft"}});
  assert.match(h.element("finalReviewStatus").textContent, /Draft/);
  assert.strictEqual(h.timers.size, 0);
  assert.strictEqual(h.element("applyFinalReview").disabled, true);
}

function testFinalReviewStartSuccessPollsExistingJobAndDropsStaleStart() {
  const h = finalReviewHarness();
  prepareFinalReviewEstimate(h);
  h.requests[0].resolve({estimated_cost_usd: "0.17", enforced: true, cost_limit_usd: 1});
  h.requests[1].resolve({job_id: "started-review"}, 202);
  assert.strictEqual(h.requests.length, 3);
  assert.match(h.requests[2].url, /\/jobs\/started-review\?project=demo$/);
  h.requests[2].resolve(finalReviewJob("running", {id: "started-review"}));
  assert.strictEqual(h.hooks.getFinalReviewState().job.id, "started-review");
  assert.strictEqual(h.fireTimer(), 3000);
  assert.match(h.requests[3].url, /\/jobs\/started-review\?project=demo$/);

  const stale = finalReviewHarness();
  prepareFinalReviewEstimate(stale);
  stale.requests[0].resolve({estimated_cost_usd: "0.17", enforced: true, cost_limit_usd: 1});
  stale.context.document.hidden = true;
  stale.hooks.handleVisibilityChange();
  stale.requests[1].resolve({job_id: "old-start"}, 202);
  assert.strictEqual(stale.requests.length, 2, "stale Start must not begin status polling");
  assert.strictEqual(stale.hooks.getFinalReviewState().job, null);
  stale.context.document.hidden = false;
  stale.hooks.handleVisibilityChange();
  assert.match(stale.requests[2].url, /\/latest\?project=demo$/);
  stale.requests[2].resolve(finalReviewJob("running", {id: "old-start"}));
  assert.strictEqual(stale.hooks.getFinalReviewState().job.id, "old-start");
  assert.strictEqual(stale.requests.filter((r) => /\/start$/.test(r.url)).length, 1);
}

function testFinalReviewExportDropsStaleChecksAndDoesNotDoubleSubmit() {
  const h = finalReviewHarness();
  recoverEmptyFinalReview(h);
  h.hooks.startFinalReview();
  h.hosts[0].resolve({sequence: {sequenceId: "sequence-1", durationSeconds: 90}});
  assert.match(h.hosts[1].script, /^rcStage2_exportFinalReviewAudio\(/);
  h.hosts[1].resolve({job: {outputPath: "/projects/demo/work/final_review.wav", jobId: "export-1"}});
  assert.strictEqual(h.fireTimer(), 1000);
  const stale = h.hosts[2];
  h.context.document.hidden = true;
  h.hooks.handleVisibilityChange();
  assert.strictEqual(h.timers.size, 0);
  stale.resolve({file: {exists: true, size: 500, modifiedMs: 100}});
  assert.strictEqual(h.timers.size, 0);
  assert.strictEqual(h.requests.length, 0);
  h.context.document.hidden = false;
  h.hooks.handleVisibilityChange();
  h.hooks.handleVisibilityChange();
  for (let i = 0; i < 4; i++) {
    h.fireTimer();
    h.hosts[h.hosts.length - 1].resolve({file: {exists: true, size: 500, modifiedMs: 100}});
  }
  assert.strictEqual(h.timers.size, 0);
  assert.match(h.hosts[h.hosts.length - 1].script, /^rcStage2_captureAssemblyLayout\(/);
  h.hosts[h.hosts.length - 1].resolve({layout: {entries: []}});
  assert.strictEqual(h.requests.length, 1);
  assert.match(h.requests[0].url, /\/estimate$/);
}

function testFinalReviewApplyDropsStaleMutationAndHostCallbacks() {
  const h = finalReviewHarness();
  restoreReview(h, "completed");
  h.hosts[0].resolve({sequence: {sequenceId: "sequence-1"}});
  h.hooks.applyFinalReview();
  h.hosts[1].resolve({sequence: {sequenceId: "sequence-1"}});
  const staleApply = h.requests[1];
  h.context.document.hidden = true;
  h.hooks.handleVisibilityChange();
  staleApply.resolve({iteration: {id: "old-iteration"}, corrections: []});
  assert.strictEqual(h.hosts.length, 2, "stale backend Apply must not clone a sequence");
  assert.strictEqual(h.element("applyFinalReview").disabled, true);

  const clone = finalReviewHarness();
  restoreReview(clone, "completed");
  clone.hosts[0].resolve({sequence: {sequenceId: "sequence-1"}});
  clone.hooks.applyFinalReview();
  clone.hosts[1].resolve({sequence: {sequenceId: "sequence-1"}});
  clone.requests[1].resolve({iteration: {id: "old-iteration"}, corrections: [{block_uid: "block-1"}]});
  clone.hooks.finalReviewProjectChanged();
  clone.hosts[2].resolve({draft: {sequenceId: "old-draft", name: "Old draft"}});
  assert.strictEqual(clone.hosts.length, 3, "stale Clone must not conform a sequence");
  assert.strictEqual(clone.element("applyFinalReview").disabled, true);
}

function testFinalReviewMissingStoredSourceNeverEnablesApply() {
  const h = finalReviewHarness();
  h.hooks.restoreFinalReview();
  h.requests[0].resolve(finalReviewJob("completed", {source_sequence: {}}));
  h.hosts[0].resolve({sequence: {sequenceId: "sequence-1"}});
  assert.strictEqual(h.element("applyFinalReview").disabled, true);
  h.hooks.applyFinalReview();
  h.hosts[1].resolve({sequence: {sequenceId: "sequence-1"}});
  assert.strictEqual(h.requests.length, 1);
}

function testFinalReviewProjectLoadRecoversAndRefreshPreservesApplyRequest() {
  const h = finalReviewHarness();
  h.initialize();
  assert.match(h.hosts[0].script, /^rcStage2_listProjects\(/);
  h.hosts[0].resolve({projects: ["demo"]});
  const latest = h.requests.find((r) => /\/final-review\/latest\?/.test(r.url));
  assert.ok(latest, "loading projects must recover the selected project's job");
  latest.resolve(finalReviewJob("completed"));
  h.hosts[h.hosts.length - 1].resolve({sequence: {sequenceId: "sequence-1"}});
  h.element("applyFinalReview").listeners.click();
  h.hosts[h.hosts.length - 1].resolve({sequence: {sequenceId: "sequence-1"}});
  const apply = h.requests[h.requests.length - 1];
  apply.fail();
  const requestId = apply.body.request_id;
  h.element("refreshProjects").listeners.click();
  h.hosts[h.hosts.length - 1].resolve({projects: ["demo"]});
  assert.strictEqual(h.hooks.getFinalReviewState().applyRequestId, requestId,
    "refreshing the same project must preserve the failed Apply's request ID");
  h.element("retryFinalReview").listeners.click();
  h.hosts[h.hosts.length - 1].resolve({sequence: {sequenceId: "sequence-1"}});
  assert.strictEqual(h.requests[h.requests.length - 1].body.request_id, requestId);
}

function testFinalReviewHiddenProjectLoadRecoversOnShow() {
  const h = finalReviewHarness();
  h.context.document.hidden = true;
  h.hooks.handleVisibilityChange();
  h.element("projectName").value = "hidden-project";
  h.hooks.finalReviewProjectChanged();
  assert.strictEqual(h.requests.length, 0);
  h.context.document.hidden = false;
  h.hooks.handleVisibilityChange();
  assert.strictEqual(h.requests.length, 1, "a project loaded while hidden must recover on show");
  assert.match(h.requests[0].url, /latest\?project=hidden-project$/);
}

function testFinalReviewTransientHttpErrorsBackOff() {
  for (const status of [429, 500, 503]) {
    const h = finalReviewHarness();
    restoreReview(h);
    h.fireTimer();
    h.requests[1].resolve("temporarily unavailable", status);
    assert.strictEqual(h.fireTimer(), 3000);
  }
  const proxy = finalReviewHarness();
  restoreReview(proxy);
  proxy.fireTimer();
  proxy.requests[1].nonJson(502);
  assert.strictEqual(proxy.timers.size, 1, "an HTML proxy failure must still be transient");
  assert.strictEqual(proxy.fireTimer(), 3000);
}

function testFinalReviewAmbiguousStartRetryRecoversWithoutStartingAgain() {
  for (const failure of ["network", 429, 500, 503, "proxy-502", "proxy-200"]) {
    const h = finalReviewHarness();
    prepareFinalReviewEstimate(h);
    h.requests[0].resolve({estimated_cost_usd: "0.17", enforced: true, cost_limit_usd: 1});
    if (failure === "network") h.requests[1].fail();
    else if (failure === "proxy-502") h.requests[1].nonJson(502);
    else if (failure === "proxy-200") h.requests[1].nonJson(200);
    else h.requests[1].resolve("temporary Start response failure", failure);
    assert.strictEqual(h.element("retryFinalReview").hidden, false,
      failure + " Start failure must expose status recovery");
    assert.strictEqual(h.element("startFinalReview").disabled, true,
      failure + " Start failure must not invite another provider Start");
    assert.strictEqual(h.element("startFinalReviewFromWav").disabled, true);
    assert.match(h.element("finalReviewStatus").textContent, /могла начаться/);
    const hostCount = h.hosts.length;
    h.hooks.startFinalReviewFromWav();
    assert.strictEqual(h.hosts.length, hostCount, "ambiguous Start cannot enter preparation again");
    h.hooks.retryFinalReview();
    h.hooks.retryFinalReview();
    assert.strictEqual(h.requests.length, 3);
    assert.match(h.requests[2].url, /\/latest\?project=demo$/);
    h.requests[2].resolve(finalReviewJob("running", {id: "accepted-but-response-lost"}));
    assert.strictEqual(h.hooks.getFinalReviewState().job.id, "accepted-but-response-lost");
    assert.strictEqual(h.requests.filter((r) => /\/start$/.test(r.url)).length, 1);
  }
  const absent = finalReviewHarness();
  prepareFinalReviewEstimate(absent);
  absent.requests[0].resolve({estimated_cost_usd: "0.17", enforced: true, cost_limit_usd: 1});
  absent.requests[1].resolve("temporary failure", 503);
  absent.hooks.retryFinalReview();
  absent.requests[2].resolve("job not found", 404);
  assert.strictEqual(absent.element("startFinalReview").disabled, true,
    "latest 404 cannot prove an ambiguous Start request has stopped processing");
  assert.strictEqual(absent.element("retryFinalReview").hidden, false);
  absent.context.document.hidden = true;
  absent.hooks.handleVisibilityChange();
  absent.context.document.hidden = false;
  absent.hooks.handleVisibilityChange();
  assert.match(absent.requests[3].url, /\/latest\?project=demo$/);

  const validation = finalReviewHarness();
  prepareFinalReviewEstimate(validation);
  validation.requests[0].resolve({estimated_cost_usd: "0.17", enforced: true, cost_limit_usd: 1});
  validation.requests[1].resolve("invalid snapshot", 400);
  assert.strictEqual(validation.element("startFinalReview").disabled, false);
  assert.strictEqual(validation.element("retryFinalReview").hidden, true);
}

function testFinalReviewHideDuringLayoutResumesCaptureOnce() {
  for (const fromWav of [true, false]) {
    const h = finalReviewHarness();
    recoverEmptyFinalReview(h);
    if (fromWav) h.hooks.startFinalReviewFromWav();
    else h.hooks.startFinalReview();
    h.hosts[0].resolve({sequence: {sequenceId: "sequence-1", durationSeconds: 90}});
    if (!fromWav) {
      h.hosts[1].resolve({job: {outputPath: "/projects/demo/work/final_review.wav", jobId: "export-1"}});
      for (let i = 0; i < 4; i++) {
        h.fireTimer();
        h.hosts[h.hosts.length - 1].resolve({file: {exists: true, size: 500, modifiedMs: 100}});
      }
    }
    const staleLayout = h.hosts[h.hosts.length - 1];
    assert.match(staleLayout.script, /^rcStage2_captureAssemblyLayout\(/);
    h.context.document.hidden = true;
    h.hooks.handleVisibilityChange();
    staleLayout.resolve({layout: {entries: ["stale"]}});
    assert.strictEqual(h.requests.length, 0, "hidden layout must not advance to Estimate/Start");
    const hostCount = h.hosts.length;
    h.context.document.hidden = false;
    h.hooks.handleVisibilityChange();
    h.hooks.handleVisibilityChange();
    assert.strictEqual(h.hosts.length, hostCount + 1,
      (fromWav ? "WAV" : "export") + " layout capture must resume exactly once on show");
    const resumedLayout = h.hosts[h.hosts.length - 1];
    assert.match(resumedLayout.script, /^rcStage2_captureAssemblyLayout\(/);
    staleLayout.resolve({layout: {entries: ["late-stale"]}});
    assert.strictEqual(h.requests.length, 0);
    resumedLayout.resolve({layout: {entries: ["fresh"]}});
    assert.strictEqual(h.requests.length, 1);
    assert.match(h.requests[0].url, /\/estimate$/);
    assert.deepStrictEqual(h.requests[0].body.assembly_layout, {entries: ["fresh"]});
    h.requests[0].resolve({estimated_cost_usd: "0.17", enforced: true, cost_limit_usd: 1});
    assert.strictEqual(h.requests.filter((r) => /\/start$/.test(r.url)).length, 1);
    h.requests[1].resolve({job_id: "resumed-review"}, 202);
    assert.match(h.requests[2].url, /\/jobs\/resumed-review\?project=demo$/);
  }
  const switched = finalReviewHarness();
  recoverEmptyFinalReview(switched);
  switched.hooks.startFinalReviewFromWav();
  switched.hosts[0].resolve({sequence: {sequenceId: "sequence-1", durationSeconds: 90}});
  switched.context.document.hidden = true;
  switched.hooks.handleVisibilityChange();
  switched.context.document.hidden = false;
  switched.hooks.handleVisibilityChange();
  switched.hosts[switched.hosts.length - 1].resolve({layout: {
    sequence: {sequenceId: "other-sequence"}, fromSeconds: 0, toSeconds: 90, video: [], audio: [],
  }});
  assert.strictEqual(switched.requests.length, 0,
    "switching active sequences while hidden must block a resumed layout from reaching Start");
  assert.strictEqual(switched.element("startFinalReviewFromWav").disabled, false);
  assert.match(switched.element("finalReviewStatus").textContent, /не совпадает/);

}

function testFinalReviewExplicitApplyAfterCloneFailureGetsFreshRequestId() {
  const h = finalReviewHarness();
  h.initialize();
  h.hosts[0].resolve({projects: ["demo"]});
  h.requests[0].resolve(finalReviewJob("completed"));
  h.hosts[h.hosts.length - 1].resolve({sequence: {sequenceId: "sequence-1"}});
  h.element("applyFinalReview").listeners.click();
  h.hosts[h.hosts.length - 1].resolve({sequence: {sequenceId: "sequence-1"}});
  const first = h.requests[1];
  first.resolve({iteration: {id: "iteration-1"}, corrections: []});
  h.hosts[h.hosts.length - 1].resolve({ok: false, error: "Clone failed before mutation"});
  assert.strictEqual(h.element("applyFinalReview").disabled, false);
  h.element("applyFinalReview").listeners.click();
  h.hosts[h.hosts.length - 1].resolve({sequence: {sequenceId: "sequence-1"}});
  const second = h.requests[2];
  assert.notStrictEqual(second.body.request_id, first.body.request_id,
    "a new explicit Apply after a Clone failure must create another iteration request");
  second.fail();
  h.element("retryFinalReview").listeners.click();
  h.hosts[h.hosts.length - 1].resolve({sequence: {sequenceId: "sequence-1"}});
  assert.strictEqual(h.requests[3].body.request_id, second.body.request_id,
    "manual transport Retry must reuse the new Apply's request ID");
}

function testFinalReviewExplicitApplyAfterSuccessRevalidatesSourceAndGetsFreshRequestId() {
  const h = finalReviewHarness();
  h.initialize();
  h.hosts[0].resolve({projects: ["demo"]});
  h.requests[0].resolve(finalReviewJob("completed"));
  h.hosts[h.hosts.length - 1].resolve({sequence: {sequenceId: "sequence-1"}});
  h.element("applyFinalReview").listeners.click();
  h.hosts[h.hosts.length - 1].resolve({sequence: {sequenceId: "sequence-1"}});
  const first = h.requests[1];
  first.resolve({iteration: {id: "iteration-1"}, corrections: []});
  h.hosts[h.hosts.length - 1].resolve({draft: {sequenceId: "draft-1", name: "Draft 1"}});
  assert.match(h.element("finalReviewStatus").textContent, /Draft 1/);
  assert.strictEqual(h.element("applyFinalReview").disabled, true);

  // Returning to the panel must revalidate even after a successful Apply.
  const hostCount = h.hosts.length;
  h.context.document.hidden = true;
  h.hooks.handleVisibilityChange();
  h.context.document.hidden = false;
  h.hooks.handleVisibilityChange();
  h.requests[2].resolve(finalReviewJob("completed"));
  assert.strictEqual(h.hosts.length, hostCount + 1,
    "a completed review must inspect the source again after a previous successful Apply");
  h.hosts[h.hosts.length - 1].resolve({sequence: {sequenceId: "draft-1"}});
  assert.strictEqual(h.element("applyFinalReview").disabled, true,
    "the created draft must not become the source of a later Apply");
  assert.match(h.element("finalReviewStatus").textContent, /не совпадает/);

  // The user opens the saved source and explicitly refreshes its status.
  h.element("retryFinalReview").listeners.click();
  h.requests[3].resolve(finalReviewJob("completed"));
  h.hosts[h.hosts.length - 1].resolve({sequence: {sequenceId: "sequence-1"}});
  assert.strictEqual(h.element("applyFinalReview").disabled, false);
  const beforeSecondApply = h.requests.length;
  h.element("applyFinalReview").listeners.click();
  assert.strictEqual(h.requests.length, beforeSecondApply,
    "every explicit Apply must inspect the active source before backend mutation");
  h.hosts[h.hosts.length - 1].resolve({sequence: {sequenceId: "sequence-1"}});
  const second = h.requests[4];
  assert.match(second.url, /\/jobs\/review-1\/apply$/);
  assert.notStrictEqual(second.body.request_id, first.body.request_id,
    "a later explicit Apply must request a fresh iteration after a successful Apply");
  second.fail();
  h.element("retryFinalReview").listeners.click();
  h.hosts[h.hosts.length - 1].resolve({sequence: {sequenceId: "sequence-1"}});
  assert.strictEqual(h.requests[5].body.request_id, second.body.request_id,
    "Retry for the unfinished second Apply must preserve its request ID");
}

function submitReviewFromWav(h) {
  h.hooks.startFinalReviewFromWav();
  h.hosts[h.hosts.length - 1].resolve({sequence: {sequenceId: "sequence-1", durationSeconds: 90}});
  h.hosts[h.hosts.length - 1].resolve({layout: {entries: []}});
  const estimate = h.requests[h.requests.length - 1];
  assert.match(estimate.url, /\/estimate$/);
  estimate.resolve({estimated_cost_usd: "0.17", enforced: true, cost_limit_usd: 1});
  const start = h.requests[h.requests.length - 1];
  assert.match(start.url, /\/start$/);
  return start;
}

function testFinalReviewUncertainStartRejectsBaselineLatestJob() {
  const h = finalReviewHarness();
  restoreReview(h, "completed");
  h.hosts[0].resolve({sequence: {sequenceId: "sequence-1"}});
  assert.strictEqual(h.element("applyFinalReview").disabled, false);
  submitReviewFromWav(h).fail();
  const hostCount = h.hosts.length;
  h.hooks.retryFinalReview();
  assert.match(h.requests[3].url, /\/latest\?project=demo$/);
  h.requests[3].resolve(finalReviewJob("completed"));
  assert.strictEqual(h.element("startFinalReview").disabled, true,
    "latest review-1 is the pre-Start job and cannot confirm a new Start");
  assert.strictEqual(h.element("startFinalReviewFromWav").disabled, true);
  assert.strictEqual(h.element("retryFinalReview").hidden, false);
  assert.strictEqual(h.element("applyFinalReview").disabled, true);
  assert.strictEqual(h.hooks.getFinalReviewState().startUncertain, true);
  assert.strictEqual(h.hooks.getFinalReviewState().job, null,
    "uncertain recovery must not adopt the previous completed job");
  assert.strictEqual(h.hosts.length, hostCount, "the previous job must not trigger Apply identity checks");
  assert.strictEqual(h.timers.size, 0, "recovery must not poll review-1");
  h.hooks.retryFinalReview();
  assert.match(h.requests[4].url, /\/latest\?project=demo$/);
  h.requests[4].resolve(finalReviewJob("running", {id: "review-2"}));
  assert.strictEqual(h.hooks.getFinalReviewState().startUncertain, false);
  assert.strictEqual(h.hooks.getFinalReviewState().job.id, "review-2");
  assert.strictEqual(h.element("startFinalReview").disabled, false);
  assert.strictEqual(h.element("retryFinalReview").hidden, true);
  assert.strictEqual(h.fireTimer(), 3000);
  assert.match(h.requests[5].url, /\/jobs\/review-2\?project=demo$/);
  assert.strictEqual(h.requests.filter((r) => /\/start$/.test(r.url)).length, 1);
}

function testFinalReviewStartBaselineResetsAndPreservesKnownJobHistory() {
  const h = finalReviewHarness();
  restoreReview(h, "completed");
  h.hosts[0].resolve({sequence: {sequenceId: "sequence-1"}});
  submitReviewFromWav(h).resolve("invalid snapshot", 400);
  assert.strictEqual(h.hooks.getFinalReviewState().startBaselineJobId, "",
    "deterministic rejection must reset the pending Start baseline");
  submitReviewFromWav(h).resolve("temporary failure", 503);
  h.hooks.retryFinalReview();
  h.requests[h.requests.length - 1].resolve(finalReviewJob("completed"));
  assert.strictEqual(h.element("startFinalReview").disabled, true,
    "a later attempt must still recognize the last known job after deterministic rejection");
  h.element("projectName").value = "different-project";
  h.hooks.finalReviewProjectChanged();
  assert.strictEqual(h.hooks.getFinalReviewState().startBaselineJobId, "",
    "changing projects must reset the previous project's Start baseline");
  h.requests[h.requests.length - 1].resolve(finalReviewJob("running", {id: "different-job", project: "different-project"}));
  assert.strictEqual(h.hooks.getFinalReviewState().job.id, "different-job");
  assert.strictEqual(h.hooks.getFinalReviewState().startUncertain, false);
}

function testFinalReviewStartWaitsForInitialRecoveryBeforeCapturingBaseline() {
  const loading = finalReviewHarness();
  loading.initialize();
  assert.strictEqual(loading.element("startFinalReview").disabled, true,
    "Start must also wait while the initial project list is still loading");
  const loadingHostCount = loading.hosts.length;
  loading.hooks.startFinalReviewFromWav();
  assert.strictEqual(loading.hosts.length, loadingHostCount);
  assert.strictEqual(loading.requests.length, 0);

  const h = finalReviewHarness();
  h.hooks.finalReviewProjectChanged();
  assert.strictEqual(h.element("startFinalReview").disabled, true);
  assert.strictEqual(h.element("startFinalReviewFromWav").disabled, true);
  h.hooks.startFinalReviewFromWav();
  h.hooks.startFinalReview();
  assert.strictEqual(h.hosts.length, 0,
    "pending initial latest must prevent preparation of another paid Start");
  assert.strictEqual(h.requests.length, 1, "blocked Start must not duplicate latest");
  h.requests[0].resolve(finalReviewJob("completed"));
  h.hosts[0].resolve({sequence: {sequenceId: "sequence-1"}});
  assert.strictEqual(h.element("startFinalReview").disabled, false);
  submitReviewFromWav(h).fail();
  h.hooks.retryFinalReview();
  h.requests[h.requests.length - 1].resolve(finalReviewJob("completed"));
  assert.strictEqual(h.hooks.getFinalReviewState().startUncertain, true,
    "the prior job learned during initial recovery cannot confirm an ambiguous new Start");
  assert.strictEqual(h.hooks.getFinalReviewState().job, null);
  assert.strictEqual(h.element("startFinalReviewFromWav").disabled, true);
  assert.strictEqual(h.requests.filter((r) => /\/start$/.test(r.url)).length, 1);
}

function testFinalReviewInitialRecovery404AndStaleRepliesGateStart() {
  const h = finalReviewHarness();
  h.hooks.finalReviewProjectChanged();
  const stale = h.requests[0];
  h.context.document.hidden = true;
  h.hooks.handleVisibilityChange();
  h.context.document.hidden = false;
  h.hooks.handleVisibilityChange();
  stale.resolve("job not found", 404);
  assert.strictEqual(h.element("startFinalReview").disabled, true,
    "a stale initial latest response must not unlock Start");
  h.requests[1].resolve("unauthorized", 401);
  h.hooks.startFinalReviewFromWav();
  assert.strictEqual(h.hosts.length, 0, "failed initial recovery must keep Start blocked");
  h.hooks.retryFinalReview();
  h.requests[h.requests.length - 1].resolve("job not found", 404);
  assert.strictEqual(h.element("startFinalReview").disabled, false,
    "a current latest 404 establishes that the project has no prior job");
  h.hooks.startFinalReviewFromWav();
  assert.strictEqual(h.hosts.length, 1, "Start may prepare after a confirmed empty history");
  h.element("projectName").value = "another-project";
  h.hooks.finalReviewProjectChanged();
  assert.strictEqual(h.element("startFinalReviewFromWav").disabled, true,
    "a different project needs its own initial recovery");
}

for (const test of [
  testFinalReviewRestoredJobsDisplayConfirmedAndPendingCosts,
  testFinalReviewPolledJobRefreshesAccounting,
  testFinalReviewStartWaitsForInitialRecoveryBeforeCapturingBaseline,
  testFinalReviewInitialRecovery404AndStaleRepliesGateStart,
  testFinalReviewBackoffAndManualRetry,
  testFinalReviewRecoveryChecksHostBeforeApply,
  testFinalReviewCancelRejectsOlderStatusAndInspection,
  testFinalReviewVisibilityAndProjectGeneration,
  testFinalReviewHideDiscardsInFlightStatusAndResumesOnce,
  testFinalReviewEstimateConfirmationAnd409,
  testFinalReviewStartAndEstimateDiscardStaleCallbacks,
  testFinalReviewApplyNetworkRetryKeepsRequestIdAndSequenceGuard,
  testFinalReviewStartSuccessPollsExistingJobAndDropsStaleStart,
  testFinalReviewExportDropsStaleChecksAndDoesNotDoubleSubmit,
  testFinalReviewApplyDropsStaleMutationAndHostCallbacks,
  testFinalReviewMissingStoredSourceNeverEnablesApply,
  testFinalReviewProjectLoadRecoversAndRefreshPreservesApplyRequest,
  testFinalReviewHiddenProjectLoadRecoversOnShow,
  testFinalReviewTransientHttpErrorsBackOff,
  testFinalReviewAmbiguousStartRetryRecoversWithoutStartingAgain,
  testFinalReviewHideDuringLayoutResumesCaptureOnce,
  testFinalReviewExplicitApplyAfterCloneFailureGetsFreshRequestId,
  testFinalReviewExplicitApplyAfterSuccessRevalidatesSourceAndGetsFreshRequestId,
  testFinalReviewUncertainStartRejectsBaselineLatestJob,
  testFinalReviewStartBaselineResetsAndPreservesKnownJobHistory,
]) {
  test();
  console.log("PASS", test.name);
}
