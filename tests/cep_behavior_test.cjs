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
