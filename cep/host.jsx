/*
 * Rough Cut v3 — Premiere Pro host automation.
 *
 * Public entry points return JSON strings and accept URI-encoded JSON strings.
 * UI and transport live in js/main.js; Premiere-specific work stays here.
 */
(function () {
    var TICKS_PER_SECOND = 254016000000;

    function fail(message, details) {
        return stringify({
            ok: false,
            error: String(message || "Unknown host error"),
            details: details || null
        });
    }

    function success(payload) {
        payload = payload || {};
        payload.ok = true;
        return stringify(payload);
    }

    function stringify(value) {
        function quote(text) {
            return '"' + String(text)
                .replace(/\\/g, "\\\\")
                .replace(/"/g, '\\"')
                .replace(/\r/g, "\\r")
                .replace(/\n/g, "\\n")
                .replace(/\t/g, "\\t")
                .replace(/\f/g, "\\f")
                .replace(/\x08/g, "\\b") + '"';
        }

        function encode(item) {
            var parts;
            var key;
            var i;
            if (item === null || item === undefined) {
                return "null";
            }
            if (typeof item === "string") {
                return quote(item);
            }
            if (typeof item === "number") {
                return isFinite(item) ? String(item) : "null";
            }
            if (typeof item === "boolean") {
                return item ? "true" : "false";
            }
            if (item instanceof Array) {
                parts = [];
                for (i = 0; i < item.length; i++) {
                    parts.push(encode(item[i]));
                }
                return "[" + parts.join(",") + "]";
            }
            if (typeof item === "object") {
                parts = [];
                for (key in item) {
                    if (item.hasOwnProperty(key) && typeof item[key] !== "function") {
                        parts.push(quote(key) + ":" + encode(item[key]));
                    }
                }
                return "{" + parts.join(",") + "}";
            }
            return "null";
        }

        return encode(value);
    }

    function parsePayload(encoded) {
        var decoded;
        if (!encoded) {
            return {};
        }
        decoded = decodeURIComponent(String(encoded));
        if (typeof JSON === "undefined" || !JSON.parse) {
            throw new Error("JSON.parse is unavailable in this Premiere ExtendScript runtime.");
        }
        return JSON.parse(decoded);
    }

    function asString(value) {
        return value === null || value === undefined ? "" : String(value);
    }

    function validProjectName(value) {
        return /^[A-Za-zА-Яа-яЁё0-9_-]{1,64}$/.test(asString(value));
    }

    function ticksOf(timeValue) {
        if (timeValue === null || timeValue === undefined) {
            return "";
        }
        if (typeof timeValue === "string" || typeof timeValue === "number") {
            return String(timeValue);
        }
        try {
            if (timeValue.ticks !== undefined) {
                return String(timeValue.ticks);
            }
        } catch (ignoreTicks) {}
        return "";
    }

    function secondsOf(timeValue) {
        var value;
        if (timeValue === null || timeValue === undefined) {
            return 0;
        }
        try {
            value = Number(timeValue.seconds);
            if (isFinite(value)) {
                return value;
            }
        } catch (ignoreSeconds) {}
        try {
            value = Number(timeValue.ticks) / TICKS_PER_SECOND;
            if (isFinite(value)) {
                return value;
            }
        } catch (ignoreTicks) {}
        // Sequence.end and Sequence.zeroPoint are tick strings, while numeric
        // values passed by this panel are seconds.
        if (typeof timeValue === "string") {
            value = Number(timeValue) / TICKS_PER_SECOND;
            return isFinite(value) ? value : 0;
        }
        value = Number(timeValue);
        return isFinite(value) ? value : 0;
    }

    function secondsToTicks(seconds) {
        return String(Math.round(Number(seconds) * TICKS_PER_SECOND));
    }

    function timeFromSeconds(seconds) {
        var value = new Time();
        value.ticks = secondsToTicks(seconds);
        return value;
    }

    function sequenceDurationSeconds(sequence) {
        var duration = secondsOf(sequence ? sequence.end : null);
        var collections = [];
        var collection;
        var track;
        var clipEnd;
        var i;
        var j;
        if (!sequence) {
            return 0;
        }
        try { collections.push(sequence.videoTracks); } catch (ignoreVideoTracks) {}
        try { collections.push(sequence.audioTracks); } catch (ignoreAudioTracks) {}
        for (i = 0; i < collections.length; i++) {
            collection = collections[i];
            if (!collection) {
                continue;
            }
            for (j = 0; j < collection.numTracks; j++) {
                track = collection[j];
                try {
                    if (track.clips.numItems > 0) {
                        clipEnd = secondsOf(track.clips[track.clips.numItems - 1].end);
                        if (isFinite(clipEnd) && clipEnd > duration) {
                            duration = clipEnd;
                        }
                    }
                } catch (ignoreTrackEnd) {}
            }
        }
        return isFinite(duration) && duration > 0 ? duration : 0;
    }

    function projectPath() {
        try {
            return asString(app.project.path);
        } catch (ignorePath) {
            return "";
        }
    }

    function normalizeFsPath(path) {
        return asString(path).replace(/\\/g, "/");
    }

    function existingFile(path) {
        var file = new File(path);
        return file.exists ? file.fsName : "";
    }

    function discoverAudioPreset() {
        var appPath = normalizeFsPath(app.path);
        var candidates = [
            appPath + "/Contents/Settings/EncoderPresets/Wave48mono24.epr",
            appPath + "/Settings/EncoderPresets/Wave48mono24.epr",
            appPath + "/EncoderPresets/Wave48mono24.epr"
        ];
        var found;
        var i;
        for (i = 0; i < candidates.length; i++) {
            found = existingFile(candidates[i]);
            if (found) {
                return found;
            }
        }
        return "";
    }

    function ensureFolder(folder) {
        if (folder.exists) {
            return true;
        }
        if (folder.parent && folder.parent.fsName !== folder.fsName && !folder.parent.exists) {
            if (!ensureFolder(folder.parent)) {
                return false;
            }
        }
        return folder.create();
    }

    function sequenceMetadata(sequence) {
        var item = null;
        var settings = null;
        var timebaseTicks = "";
        var displayFormat = null;
        try {
            item = sequence.projectItem;
        } catch (ignoreItem) {}
        try {
            settings = sequence.getSettings();
            if (settings) {
                timebaseTicks = ticksOf(settings.videoFrameRate);
                displayFormat = settings.videoDisplayFormat;
            }
        } catch (ignoreSettings) {}

        return {
            sequenceId: asString(sequence.sequenceID),
            name: asString(sequence.name),
            projectItemNodeId: item ? asString(item.nodeId) : "",
            projectItemTreePath: item ? asString(item.treePath) : "",
            projectPath: projectPath(),
            durationSeconds: sequenceDurationSeconds(sequence),
            durationTicks: ticksOf(sequence.end),
            zeroPointSeconds: secondsOf(sequence.zeroPoint),
            zeroPointTicks: ticksOf(sequence.zeroPoint),
            timebaseTicks: timebaseTicks,
            videoDisplayFormat: displayFormat,
            videoTrackCount: sequence.videoTracks ? Number(sequence.videoTracks.numTracks) : 0,
            audioTrackCount: sequence.audioTracks ? Number(sequence.audioTracks.numTracks) : 0
        };
    }

    function allSequences() {
        var result = [];
        var collection;
        var i;
        if (!app.project) {
            return result;
        }
        collection = app.project.sequences;
        for (i = 0; i < collection.numSequences; i++) {
            result.push(collection[i]);
        }
        return result;
    }

    function resolveSequence(criteria) {
        var sequences = allSequences();
        var byId = [];
        var byNode = [];
        var byName = [];
        var sequence;
        var item;
        var i;
        var wantedId = asString(criteria.sequenceId || criteria.sourceSequenceId);
        var wantedNode = asString(criteria.projectItemNodeId || criteria.sourceProjectItemNodeId);
        var wantedName = asString(criteria.name || criteria.sourceSequenceName);

        for (i = 0; i < sequences.length; i++) {
            sequence = sequences[i];
            if (wantedId && asString(sequence.sequenceID) === wantedId) {
                byId.push(sequence);
            }
            if (wantedNode) {
                try {
                    item = sequence.projectItem;
                    if (item && asString(item.nodeId) === wantedNode) {
                        byNode.push(sequence);
                    }
                } catch (ignoreNode) {}
            }
            if (wantedName && asString(sequence.name) === wantedName) {
                byName.push(sequence);
            }
        }
        if (byId.length === 1) {
            return byId[0];
        }
        if (wantedId) {
            throw new Error("Stored sequence ID no longer resolves; re-register this source before conform.");
        }
        if (byNode.length === 1) {
            return byNode[0];
        }
        if (wantedNode) {
            throw new Error("Stored project-item node ID no longer resolves; re-register this source before conform.");
        }
        if (byName.length === 1) {
            return byName[0];
        }
        if (byName.length > 1) {
            throw new Error("Several sequences have the same name; re-register to store a sequence ID.");
        }
        throw new Error("Sequence not found by stored ID, project-item node ID, or exact name.");
    }

    function saveRange(projectItem) {
        var state = { video: {}, audio: {} };

        function read(mediaType, target) {
            try {
                target.inPoint = projectItem.getInPoint(mediaType);
                target.hasIn = ticksOf(target.inPoint) !== "" && Number(ticksOf(target.inPoint)) >= 0;
            } catch (ignoreIn) {
                target.hasIn = false;
            }
            try {
                target.outPoint = projectItem.getOutPoint(mediaType);
                target.hasOut = ticksOf(target.outPoint) !== "" && Number(ticksOf(target.outPoint)) >= 0;
            } catch (ignoreOut) {
                target.hasOut = false;
            }
        }

        read(1, state.video);
        read(2, state.audio);
        return state;
    }

    function restoreRange(projectItem, state) {
        try { projectItem.clearInPoint(); } catch (ignoreClearIn) {}
        try { projectItem.clearOutPoint(); } catch (ignoreClearOut) {}
        if (state.video.hasIn && state.audio.hasIn &&
                ticksOf(state.video.inPoint) === ticksOf(state.audio.inPoint)) {
            try { projectItem.setInPoint(ticksOf(state.video.inPoint), 4); } catch (ignoreSetAllIn) {}
        } else {
            if (state.video.hasIn) {
                try { projectItem.setInPoint(ticksOf(state.video.inPoint), 1); } catch (ignoreSetVideoIn) {}
            }
            if (state.audio.hasIn) {
                try { projectItem.setInPoint(ticksOf(state.audio.inPoint), 2); } catch (ignoreSetAudioIn) {}
            }
        }
        if (state.video.hasOut && state.audio.hasOut &&
                ticksOf(state.video.outPoint) === ticksOf(state.audio.outPoint)) {
            try { projectItem.setOutPoint(ticksOf(state.video.outPoint), 4); } catch (ignoreSetAllOut) {}
        } else {
            if (state.video.hasOut) {
                try { projectItem.setOutPoint(ticksOf(state.video.outPoint), 1); } catch (ignoreSetVideoOut) {}
            }
            if (state.audio.hasOut) {
                try { projectItem.setOutPoint(ticksOf(state.audio.outPoint), 2); } catch (ignoreSetAudioOut) {}
            }
        }
    }

    function setRange(projectItem, inSeconds, outSeconds) {
        var tolerance = 0.05;
        var inResult;
        var outResult;
        var videoIn;
        var videoOut;
        var audioIn;
        var audioOut;
        try { projectItem.clearInPoint(); } catch (ignoreClearIn) {}
        try { projectItem.clearOutPoint(); } catch (ignoreClearOut) {}
        inResult = projectItem.setInPoint(secondsToTicks(inSeconds), 4);
        outResult = projectItem.setOutPoint(secondsToTicks(outSeconds), 4);
        videoIn = secondsOf(projectItem.getInPoint(1));
        videoOut = secondsOf(projectItem.getOutPoint(1));
        audioIn = secondsOf(projectItem.getInPoint(2));
        audioOut = secondsOf(projectItem.getOutPoint(2));
        if (Math.abs(videoIn - inSeconds) > tolerance ||
                Math.abs(videoOut - outSeconds) > tolerance ||
                Math.abs(audioIn - inSeconds) > tolerance ||
                Math.abs(audioOut - outSeconds) > tolerance) {
            throw new Error(
                "Premiere did not apply the requested source range. " +
                "Requested " + Number(inSeconds).toFixed(3) + "–" +
                Number(outSeconds).toFixed(3) + "s; read back V " +
                videoIn.toFixed(3) + "–" + videoOut.toFixed(3) +
                "s, A " + audioIn.toFixed(3) + "–" +
                audioOut.toFixed(3) + "s."
            );
        }
        return {
            video: true,
            audio: true,
            inResult: inResult,
            outResult: outResult,
            videoInSeconds: videoIn,
            videoOutSeconds: videoOut,
            audioInSeconds: audioIn,
            audioOutSeconds: audioOut
        };
    }

    function trackClipCount(track) {
        try {
            return Number(track.clips.numItems);
        } catch (ignoreCount) {
            return 0;
        }
    }

    function trackSourceAt(track, projectItem, timelineSeconds) {
        var expectedNodeId = "";
        var clip;
        var clipItem;
        var clipStart;
        var i;
        try {
            expectedNodeId = asString(projectItem.nodeId);
            for (i = 0; i < track.clips.numItems; i++) {
                clip = track.clips[i];
                clipStart = secondsOf(clip.start);
                if (Math.abs(clipStart - timelineSeconds) > 0.05) {
                    continue;
                }
                clipItem = clip.projectItem;
                if (clipItem === projectItem ||
                        (expectedNodeId && clipItem && asString(clipItem.nodeId) === expectedNodeId)) {
                    return clip;
                }
            }
        } catch (ignoreScan) {}
        return null;
    }

    function trackItemMetadata(clip) {
        if (!clip) {
            return null;
        }
        return {
            name: asString(clip.name),
            nodeId: asString(clip.nodeId),
            startSeconds: secondsOf(clip.start),
            endSeconds: secondsOf(clip.end),
            durationSeconds: Math.max(0, secondsOf(clip.end) - secondsOf(clip.start)),
            sourceInSeconds: secondsOf(clip.inPoint),
            sourceOutSeconds: secondsOf(clip.outPoint)
        };
    }

    function verifyInsertedRange(target, sourceItem, params) {
        var expectedDuration = params.sourceOutSeconds - params.sourceInSeconds;
        var videoClip = trackSourceAt(
            target.videoTracks[params.videoTrackIndex],
            sourceItem,
            params.timelineSeconds
        );
        var audioClip = trackSourceAt(
            target.audioTracks[params.audioTrackIndex],
            sourceItem,
            params.timelineSeconds
        );
        var video = trackItemMetadata(videoClip);
        var audio = trackItemMetadata(audioClip);
        var tolerance = 0.08;
        if (!video || !audio) {
            throw new Error("Premiere did not expose both inserted video and audio track items.");
        }
        if (Math.abs(video.durationSeconds - expectedDuration) > tolerance ||
                Math.abs(audio.durationSeconds - expectedDuration) > tolerance ||
                Math.abs(video.sourceInSeconds - params.sourceInSeconds) > tolerance ||
                Math.abs(video.sourceOutSeconds - params.sourceOutSeconds) > tolerance ||
                Math.abs(audio.sourceInSeconds - params.sourceInSeconds) > tolerance ||
                Math.abs(audio.sourceOutSeconds - params.sourceOutSeconds) > tolerance) {
            throw new Error(
                "Inserted range verification failed. Expected source " +
                params.sourceInSeconds.toFixed(3) + "–" +
                params.sourceOutSeconds.toFixed(3) + "s (" +
                expectedDuration.toFixed(3) + "s); got V " +
                video.sourceInSeconds.toFixed(3) + "–" +
                video.sourceOutSeconds.toFixed(3) + "s (" +
                video.durationSeconds.toFixed(3) + "s), A " +
                audio.sourceInSeconds.toFixed(3) + "–" +
                audio.sourceOutSeconds.toFixed(3) + "s (" +
                audio.durationSeconds.toFixed(3) + "s)."
            );
        }
        return {
            videoClip: videoClip,
            audioClip: audioClip,
            report: {
                expectedDurationSeconds: expectedDuration,
                video: video,
                audio: audio
            }
        };
    }

    function placeholderForRipple(target, params) {
        var track = target.videoTracks[params.videoTrackIndex];
        var expectedName = asString(params.placeholderName);
        var expectedNames = params.placeholderNames instanceof Array ?
            params.placeholderNames.slice(0) : [];
        var matches = [];
        var clip;
        var projectItem;
        var projectName;
        var clipName;
        var clipStart;
        var clipEnd;
        var i;
        var j;

        if (expectedName) {
            expectedNames.push(expectedName);
        }
        for (i = expectedNames.length - 1; i >= 0; i--) {
            expectedNames[i] = asString(expectedNames[i]);
            if (!expectedNames[i]) {
                expectedNames.splice(i, 1);
            }
        }
        if (!expectedNames.length) {
            throw new Error("Ripple replace requires an exact placeholder name.");
        }
        for (i = 0; i < track.clips.numItems; i++) {
            clip = track.clips[i];
            clipStart = secondsOf(clip.start);
            if (Math.abs(clipStart - params.timelineSeconds) > 0.08) {
                continue;
            }
            projectItem = clip.projectItem;
            projectName = projectItem ? asString(projectItem.name) : "";
            clipName = asString(clip.name);
            for (j = 0; j < expectedNames.length; j++) {
                if (placeholderLabelMatches(projectName, expectedNames[j]) ||
                        placeholderLabelMatches(clipName, expectedNames[j])) {
                    break;
                }
            }
            if (j < expectedNames.length) {
                clipEnd = secondsOf(clip.end);
                matches.push({
                    clip: clip,
                    clipName: clipName,
                    projectItemName: projectName,
                    startSeconds: clipStart,
                    endSeconds: clipEnd,
                    durationSeconds: Math.max(0, clipEnd - clipStart)
                });
            }
        }
        if (matches.length !== 1) {
            throw new Error(
                "Expected exactly one placeholder [" + expectedNames.join(" | ") + "]" +
                " at " + Number(params.timelineSeconds).toFixed(3) +
                "s on V" + (params.videoTrackIndex + 1) +
                "; found " + matches.length + "."
            );
        }
        return matches[0];
    }

    function removePlaceholderWithRipple(target, params) {
        var found = placeholderForRipple(target, params);
        var videoTrack = target.videoTracks[params.videoTrackIndex];
        var beforeCount = trackClipCount(videoTrack);
        var sequenceDurationBefore = sequenceDurationSeconds(target);
        var removeResult = found.clip.remove(1, 1);
        var afterCount = trackClipCount(videoTrack);
        if (afterCount >= beforeCount) {
            throw new Error("Premiere did not remove the expected placeholder.");
        }
        return {
            mode: "ripple_replace",
            placeholderName: found.projectItemName,
            clipName: found.clipName,
            startSeconds: found.startSeconds,
            endSeconds: found.endSeconds,
            durationSeconds: found.durationSeconds,
            removeResult: removeResult,
            videoItemsBefore: beforeCount,
            videoItemsAfter: afterCount,
            sequenceDurationBefore: sequenceDurationBefore
        };
    }

    function removePlaceholderWithoutRipple(target, params) {
        var found = placeholderForRipple(target, params);
        var videoTrack = target.videoTracks[params.videoTrackIndex];
        var beforeCount = trackClipCount(videoTrack);
        var removeResult = found.clip.remove(0, 1);
        var afterCount = trackClipCount(videoTrack);
        if (afterCount >= beforeCount) {
            throw new Error("Premiere did not remove the expected placeholder without ripple.");
        }
        return {
            mode: "explicit_track_shift",
            placeholderName: found.projectItemName,
            clipName: found.clipName,
            startSeconds: found.startSeconds,
            endSeconds: found.endSeconds,
            durationSeconds: found.durationSeconds,
            removeResult: removeResult,
            videoItemsBefore: beforeCount,
            videoItemsAfter: afterCount
        };
    }

    function findTrackItemByNodeId(track, nodeId) {
        var i;
        for (i = 0; i < track.clips.numItems; i++) {
            if (asString(track.clips[i].nodeId) === asString(nodeId)) {
                return track.clips[i];
            }
        }
        return null;
    }

    function captureDownstreamItems(target, boundarySeconds) {
        var entries = [];

        function capture(collection, mediaType) {
            var track;
            var clip;
            var start;
            var end;
            var i;
            var j;
            for (i = 0; i < collection.numTracks; i++) {
                track = collection[i];
                for (j = 0; j < track.clips.numItems; j++) {
                    clip = track.clips[j];
                    start = secondsOf(clip.start);
                    end = secondsOf(clip.end);
                    if (start < boundarySeconds - 0.05 &&
                            end > boundarySeconds + 0.05) {
                        throw new Error(
                            mediaType + (i + 1) + " contains a clip crossing the replacement boundary: " +
                            asString(clip.name) + " [" + start.toFixed(3) + "–" +
                            end.toFixed(3) + "s]."
                        );
                    }
                    if (start >= boundarySeconds - 0.05) {
                        entries.push({
                            mediaType: mediaType,
                            trackIndex: i,
                            nodeId: asString(clip.nodeId),
                            name: asString(clip.name),
                            originalStartSeconds: start,
                            originalEndSeconds: end
                        });
                    }
                }
            }
        }

        capture(target.videoTracks, "V");
        capture(target.audioTracks, "A");
        return entries;
    }

    function trackForEntry(target, entry) {
        return entry.mediaType === "V" ?
            target.videoTracks[entry.trackIndex] :
            target.audioTracks[entry.trackIndex];
    }

    function shiftDownstreamItems(target, entries, deltaSeconds) {
        var moved = 0;
        var alreadyMoved = 0;
        var entry;
        var track;
        var clip;
        var currentStart;
        var expectedStart;
        var moveResult;
        var i;
        entries.sort(function (left, right) {
            if (deltaSeconds > 0) {
                return right.originalStartSeconds - left.originalStartSeconds;
            }
            return left.originalStartSeconds - right.originalStartSeconds;
        });
        for (i = 0; i < entries.length; i++) {
            entry = entries[i];
            track = trackForEntry(target, entry);
            clip = findTrackItemByNodeId(track, entry.nodeId);
            if (!clip) {
                throw new Error(
                    "Downstream clip disappeared before move: " +
                    entry.mediaType + (entry.trackIndex + 1) + " " + entry.name + "."
                );
            }
            currentStart = secondsOf(clip.start);
            expectedStart = entry.originalStartSeconds + deltaSeconds;
            if (Math.abs(currentStart - expectedStart) <= 0.05) {
                alreadyMoved++;
                continue;
            }
            if (Math.abs(currentStart - entry.originalStartSeconds) > 0.05) {
                throw new Error(
                    "Unexpected downstream clip position before move: " +
                    entry.name + " at " + currentStart.toFixed(3) +
                    "s; expected " + entry.originalStartSeconds.toFixed(3) + "s."
                );
            }
            moveResult = clip.move(timeFromSeconds(deltaSeconds));
            clip = findTrackItemByNodeId(track, entry.nodeId);
            if (!clip || Math.abs(secondsOf(clip.start) - expectedStart) > 0.05) {
                throw new Error(
                    "Premiere did not move downstream clip to the expected position: " +
                    entry.name + " → " + expectedStart.toFixed(3) + "s."
                );
            }
            moved++;
        }
        return {
            deltaSeconds: deltaSeconds,
            requested: entries.length,
            moved: moved,
            alreadyMovedByLink: alreadyMoved
        };
    }

    function verifyDownstreamItems(target, entries, deltaSeconds) {
        var entry;
        var track;
        var clip;
        var expectedStart;
        var i;
        for (i = 0; i < entries.length; i++) {
            entry = entries[i];
            track = trackForEntry(target, entry);
            clip = findTrackItemByNodeId(track, entry.nodeId);
            expectedStart = entry.originalStartSeconds + deltaSeconds;
            if (!clip || Math.abs(secondsOf(clip.start) - expectedStart) > 0.05) {
                throw new Error(
                    "Cross-track verification failed for " + entry.mediaType +
                    (entry.trackIndex + 1) + " " + entry.name + "."
                );
            }
        }
        return true;
    }

    function trackRangeIsClear(track, startSeconds, endSeconds) {
        var clip;
        var start;
        var end;
        var i;
        for (i = 0; i < track.clips.numItems; i++) {
            clip = track.clips[i];
            start = secondsOf(clip.start);
            end = secondsOf(clip.end);
            if (start < endSeconds - 0.05 && end > startSeconds + 0.05) {
                return false;
            }
        }
        return true;
    }

    function replaceWithExplicitTrackShift(target, sourceItem, params, rangeSupport) {
        var sequenceDurationBefore = sequenceDurationSeconds(target);
        var placeholder = placeholderForRipple(target, params);
        var actualDuration = rangeSupport.videoOutSeconds -
            rangeSupport.videoInSeconds;
        var audioDuration = rangeSupport.audioOutSeconds -
            rangeSupport.audioInSeconds;
        var deltaSeconds;
        var downstream;
        var replacement;
        var shift;
        var overwriteParams;
        var placement;
        var verification;
        var finalDuration;
        var expectedFinalDuration;

        if (Math.abs(actualDuration - audioDuration) > 0.05 || actualDuration <= 0) {
            throw new Error("Source video/audio ranges have different or invalid durations.");
        }
        deltaSeconds = actualDuration - placeholder.durationSeconds;
        downstream = captureDownstreamItems(target, placeholder.endSeconds);
        replacement = removePlaceholderWithoutRipple(target, params);

        overwriteParams = {
            sourceInSeconds: params.sourceInSeconds,
            sourceOutSeconds: params.sourceOutSeconds,
            timelineSeconds: placeholder.startSeconds,
            videoTrackIndex: params.videoTrackIndex,
            audioTrackIndex: params.audioTrackIndex,
            mode: "overwrite"
        };
        if (deltaSeconds > 0) {
            shift = shiftDownstreamItems(target, downstream, deltaSeconds);
        }
        if (!trackRangeIsClear(
                target.videoTracks[params.videoTrackIndex],
                placeholder.startSeconds,
                placeholder.startSeconds + actualDuration
            ) || !trackRangeIsClear(
                target.audioTracks[params.audioTrackIndex],
                placeholder.startSeconds,
                placeholder.startSeconds + actualDuration
            )) {
            throw new Error("The prepared target V/A interval is not empty; overwrite cancelled.");
        }
        placement = placeRange(target, sourceItem, overwriteParams);
        verification = verifyInsertedRange(target, sourceItem, overwriteParams);
        if (deltaSeconds <= 0) {
            shift = shiftDownstreamItems(target, downstream, deltaSeconds);
        }
        verifyDownstreamItems(target, downstream, deltaSeconds);
        finalDuration = sequenceDurationSeconds(target);
        expectedFinalDuration = sequenceDurationBefore + deltaSeconds;
        if (Math.abs(finalDuration - expectedFinalDuration) > 0.1) {
            throw new Error(
                "Final sequence duration mismatch after explicit cross-track shift. Expected " +
                expectedFinalDuration.toFixed(3) + "s, got " +
                finalDuration.toFixed(3) + "s."
            );
        }
        return {
            placement: placement,
            verification: verification.report,
            replacement: replacement,
            shift: shift,
            downstreamVerified: true,
            sequenceDurationBefore: sequenceDurationBefore,
            expectedSequenceDurationAfter: expectedFinalDuration,
            sequenceDurationAfter: finalDuration,
            durationMatchesExpected: true
        };
    }

    function placeholderCandidates(params) {
        var stableIdentity = asString(params.placeholderIdentity);
        var names = params.placeholderNames instanceof Array ?
            params.placeholderNames.slice(0) : [];
        var single = asString(params.placeholderName);
        var result = [];
        var name;
        var seen = {};
        var i;
        if (params.placeholderIdentityRequired && !stableIdentity) {
            throw new Error(
                "Segment " + asString(params.segmentId) +
                " has no canonical placeholder identity. Rebuild the " +
                "normalized timeline and XML. Conform cancelled before " +
                "timeline changes."
            );
        }
        if (stableIdentity) {
            return [stableIdentity];
        }
        if (single) {
            // Legacy panels did not provide canonical block identity. Keep
            // their filename/display-name lookup isolated from the safe path.
            names.unshift(single);
        }
        for (i = 0; i < names.length; i++) {
            name = asString(names[i]);
            if (name && !seen[name]) {
                seen[name] = true;
                result.push(name);
            }
        }
        return result;
    }

    function placeholderLabelMatches(actualName, expectedIdentity) {
        var actual = asString(actualName);
        var expected = asString(expectedIdentity);
        if (expected.indexOf("RC_BLOCK ") === 0) {
            return actual === expected ||
                actual.indexOf(expected + " | ") === 0;
        }
        return actual === expected;
    }

    function placeholderAnywhere(target, params) {
        var track = target.videoTracks[params.videoTrackIndex];
        var names = placeholderCandidates(params);
        var matches = [];
        var positionMatches = [];
        var expectedStart = Number(params.timelineSeconds);
        var clip;
        var projectName;
        var clipName;
        var i;
        var j;
        if (!names.length) {
            throw new Error("No exact placeholder names were supplied.");
        }
        // Canonical RC_BLOCK identity is the only candidate for current
        // panels. Multiple aliases below are legacy-panel compatibility.
        for (j = 0; j < names.length; j++) {
            matches = [];
            for (i = 0; i < track.clips.numItems; i++) {
                clip = track.clips[i];
                projectName = clip.projectItem ? asString(clip.projectItem.name) : "";
                clipName = asString(clip.name);
                if (placeholderLabelMatches(projectName, names[j]) ||
                        placeholderLabelMatches(clipName, names[j])) {
                    matches.push({
                        clip: clip,
                        nodeId: asString(clip.nodeId),
                        clipName: clipName,
                        projectItemName: projectName,
                        startSeconds: secondsOf(clip.start),
                        endSeconds: secondsOf(clip.end),
                        durationSeconds: Math.max(
                            0,
                            secondsOf(clip.end) - secondsOf(clip.start)
                        )
                    });
                }
            }
            if (matches.length === 1) {
                return matches[0];
            }
            if (matches.length > 1) {
                if (asString(params.placeholderIdentity)) {
                    throw new Error(
                        "Target sequence is ambiguous: expected one placeholder " +
                        "identity [" + asString(params.placeholderIdentity) +
                        "] for " + asString(params.segmentId) + " on V" +
                        (params.videoTrackIndex + 1) + "; found " +
                        matches.length + ". Remove the duplicate or re-import " +
                        "the latest XML. Conform cancelled before timeline changes."
                    );
                }
                positionMatches = [];
                if (isFinite(expectedStart)) {
                    for (i = 0; i < matches.length; i++) {
                        if (Math.abs(matches[i].startSeconds - expectedStart) <= 0.08) {
                            positionMatches.push(matches[i]);
                        }
                    }
                }
                if (positionMatches.length === 1) {
                    return positionMatches[0];
                }
                throw new Error(
                    "Expected one globally unique placeholder [" + names[j] +
                    "] on V" + (params.videoTrackIndex + 1) +
                    "; found " + matches.length +
                    ", including " + positionMatches.length +
                    " at expected " + expectedStart.toFixed(3) + "s."
                );
            }
        }
        throw new Error(
            (asString(params.placeholderIdentity) ?
                "Target sequence is stale or legacy: placeholder identity [" +
                asString(params.placeholderIdentity) + "] for " +
                asString(params.segmentId) + " was not found on V" +
                (params.videoTrackIndex + 1) + ". Re-import the latest XML " +
                "into a new sequence. Conform cancelled before timeline changes." :
                "Expected one globally unique placeholder [" + names.join(" | ") +
                "] on V" + (params.videoTrackIndex + 1) + "; found 0.")
        );
    }

    function preparedGapForInsertion(target, item, actualDuration, params) {
        var expectedTimelineDuration = Number(params.expectedTimelineDuration);
        var currentDuration = sequenceDurationSeconds(target);
        var start = Number(item.timelineSeconds);
        var end = start + Number(actualDuration);
        var videoTrack;
        var endsAtStart = [];
        var startsAtEnd = [];
        var clip;
        var clipStart;
        var clipEnd;
        var count;
        var i;
        if (!item.placeholderIdentityRequired ||
                asString(item.placeholderIdentity).indexOf("RC_BLOCK ") !== 0) {
            throw new Error(
                "prepared-gap recovery requires a canonical placeholder identity"
            );
        }
        if (!isFinite(expectedTimelineDuration) ||
                expectedTimelineDuration <= 0 ||
                Math.abs(currentDuration - expectedTimelineDuration) > 0.1) {
            throw new Error(
                "sequence duration does not match the expected prepared timeline"
            );
        }
        if (item.videoTrackIndex !== 0 || item.audioTrackIndex !== 0) {
            throw new Error("prepared-gap recovery is limited to V1/A1");
        }
        if (!isFinite(start) || start < 0 ||
                !isFinite(actualDuration) || actualDuration <= 0) {
            throw new Error("prepared-gap recovery received an invalid range");
        }
        if (!trackRangeIsClear(target.videoTracks[0], start, end) ||
                !trackRangeIsClear(target.audioTracks[0], start, end)) {
            throw new Error("the expected V1/A1 gap is not clear");
        }
        videoTrack = target.videoTracks[0];
        count = Number(videoTrack.clips.numItems);
        if (count > 10000) {
            throw new Error("prepared-gap boundary scan exceeded 10000 clips");
        }
        for (i = 0; i < count; i++) {
            clip = videoTrack.clips[i];
            clipStart = secondsOf(clip.start);
            clipEnd = secondsOf(clip.end);
            if (Math.abs(clipEnd - start) <= 0.05) {
                endsAtStart.push(clip);
            }
            if (Math.abs(clipStart - end) <= 0.05) {
                startsAtEnd.push(clip);
            }
        }
        if (endsAtStart.length !== 1 || startsAtEnd.length !== 1 ||
                asString(endsAtStart[0].nodeId) ===
                    asString(startsAtEnd[0].nodeId)) {
            throw new Error(
                "V1 does not have unique clips bracketing the prepared gap"
            );
        }
        return {
            preparedGap: true,
            placeholder: {
                clip: null,
                nodeId: "",
                clipName: asString(item.placeholderIdentity),
                projectItemName: asString(item.placeholderIdentity),
                startSeconds: start,
                endSeconds: end,
                durationSeconds: Number(actualDuration)
            },
            currentDuration: currentDuration,
            expectedTimelineDuration: expectedTimelineDuration
        };
    }

    function captureAllTrackItems(target, excludedNodeIds) {
        var entries = [];
        var excluded = {};
        var key;

        function capture(collection, mediaType) {
            var track;
            var clip;
            var nodeId;
            var i;
            var j;
            for (i = 0; i < collection.numTracks; i++) {
                track = collection[i];
                for (j = 0; j < track.clips.numItems; j++) {
                    clip = track.clips[j];
                    nodeId = asString(clip.nodeId);
                    if (excluded[nodeId]) {
                        continue;
                    }
                    entries.push({
                        mediaType: mediaType,
                        trackIndex: i,
                        nodeId: nodeId,
                        name: asString(clip.name),
                        originalStartSeconds: secondsOf(clip.start),
                        originalEndSeconds: secondsOf(clip.end),
                        finalShiftSeconds: 0,
                        finalStartSeconds: secondsOf(clip.start)
                    });
                }
            }
        }

        for (key in excludedNodeIds) {
            if (excludedNodeIds.hasOwnProperty(key)) {
                excluded[key] = true;
            }
        }
        capture(target.videoTracks, "V");
        capture(target.audioTracks, "A");
        return entries;
    }

    function assignFinalBatchPositions(entries, plans) {
        var entry;
        var shift;
        var i;
        var j;
        for (i = 0; i < entries.length; i++) {
            entry = entries[i];
            shift = 0;
            for (j = 0; j < plans.length; j++) {
                if (entry.originalStartSeconds >= plans[j].placeholder.endSeconds - 0.05) {
                    shift += plans[j].deltaSeconds;
                }
            }
            entry.finalShiftSeconds = shift;
            entry.finalStartSeconds = entry.originalStartSeconds + shift;
        }
    }

    function moveBatchEntriesOnce(target, entries) {
        var movers = [];
        var entry;
        var track;
        var clip;
        var currentStart;
        var moveResult;
        var moved = 0;
        var alreadyMoved = 0;
        var i;
        for (i = 0; i < entries.length; i++) {
            if (Math.abs(entries[i].finalShiftSeconds) > 0.001) {
                movers.push(entries[i]);
            }
        }
        movers.sort(function (left, right) {
            var leftRight = left.finalShiftSeconds > 0;
            var rightRight = right.finalShiftSeconds > 0;
            if (leftRight && !rightRight) {
                return -1;
            }
            if (!leftRight && rightRight) {
                return 1;
            }
            if (leftRight) {
                return right.originalStartSeconds - left.originalStartSeconds;
            }
            return left.originalStartSeconds - right.originalStartSeconds;
        });
        for (i = 0; i < movers.length; i++) {
            entry = movers[i];
            track = trackForEntry(target, entry);
            clip = findTrackItemByNodeId(track, entry.nodeId);
            if (!clip) {
                throw new Error("Batch mover lost clip: " + entry.name + ".");
            }
            currentStart = secondsOf(clip.start);
            if (Math.abs(currentStart - entry.finalStartSeconds) <= 0.05) {
                alreadyMoved++;
                continue;
            }
            if (Math.abs(currentStart - entry.originalStartSeconds) > 0.05) {
                throw new Error(
                    "Unexpected pre-move position for " + entry.name +
                    ": " + currentStart.toFixed(3) + "s."
                );
            }
            moveResult = clip.move(
                timeFromSeconds(entry.finalStartSeconds - currentStart)
            );
            clip = findTrackItemByNodeId(track, entry.nodeId);
            if (!clip ||
                    Math.abs(secondsOf(clip.start) - entry.finalStartSeconds) > 0.05) {
                throw new Error(
                    "Batch mover could not place " + entry.name +
                    " at " + entry.finalStartSeconds.toFixed(3) + "s."
                );
            }
            moved++;
        }
        return {
            requested: movers.length,
            moved: moved,
            alreadyMovedByLink: alreadyMoved
        };
    }

    function verifyBatchEntryPositions(target, entries) {
        var entry;
        var clip;
        var i;
        for (i = 0; i < entries.length; i++) {
            entry = entries[i];
            clip = findTrackItemByNodeId(trackForEntry(target, entry), entry.nodeId);
            if (!clip ||
                    Math.abs(secondsOf(clip.start) - entry.finalStartSeconds) > 0.05) {
                throw new Error(
                    "Batch cross-track verification failed for " +
                    entry.mediaType + (entry.trackIndex + 1) +
                    " " + entry.name + "."
                );
            }
        }
        return true;
    }

    function removeBatchPlaceholders(target, plans) {
        var plan;
        var track;
        var clip;
        var before;
        var after;
        var removed = 0;
        var i;
        plans.sort(function (left, right) {
            return right.placeholder.startSeconds - left.placeholder.startSeconds;
        });
        for (i = 0; i < plans.length; i++) {
            plan = plans[i];
            if (plan.preparedGap) {
                continue;
            }
            track = target.videoTracks[plan.item.videoTrackIndex];
            clip = findTrackItemByNodeId(track, plan.placeholder.nodeId);
            if (!clip) {
                throw new Error(
                    "Batch placeholder disappeared before removal: " +
                    plan.placeholder.clipName + "."
                );
            }
            before = trackClipCount(track);
            clip.remove(0, 1);
            after = trackClipCount(track);
            if (after >= before) {
                throw new Error(
                    "Batch could not remove placeholder: " +
                    plan.placeholder.clipName + "."
                );
            }
            removed++;
        }
        plans.sort(function (left, right) {
            return left.placeholder.startSeconds - right.placeholder.startSeconds;
        });
        return removed;
    }

    function conformOnePass(target, items, params) {
        var sequenceDurationBefore = sequenceDurationSeconds(target);
        var plans = [];
        var skipped = [];
        var skippedPlans = [];
        var excludedNodeIds = {};
        var entries;
        var cumulativeDelta = 0;
        var totalDelta = 0;
        var item;
        var source;
        var sourceItem;
        var existingVideo;
        var existingAudio;
        var existingVerification;
        var placeholder;
        var placeholderError;
        var preparedGap;
        var originalRange;
        var rangeSupport;
        var actualDuration;
        var plan;
        var moveReport;
        var overwriteParams;
        var placement;
        var verification;
        var markerState;
        var markerResult;
        var skippedPlan;
        var skippedPosition;
        var removedPlaceholderCount;
        var inserted = [];
        var warnings = [];
        var finalDuration;
        var expectedFinalDuration;
        var i;

        items.sort(function (left, right) {
            return Number(left.timelineSeconds) - Number(right.timelineSeconds);
        });
        for (i = 0; i < items.length; i++) {
            item = items[i];
            item.sourceInSeconds = Number(item.sourceInSeconds);
            item.sourceOutSeconds = Number(item.sourceOutSeconds);
            item.timelineSeconds = Number(item.timelineSeconds);
            item.videoTrackIndex = Math.floor(Number(
                item.videoTrackIndex === undefined ?
                    params.videoTrackIndex : item.videoTrackIndex
            ));
            item.audioTrackIndex = Math.floor(Number(
                item.audioTrackIndex === undefined ?
                    params.audioTrackIndex : item.audioTrackIndex
            ));
            source = resolveSequence(item);
            sourceItem = source.projectItem;
            if (!sourceItem) {
                throw new Error("Batch source sequence has no project item.");
            }
            item.sourceSequenceName = asString(source.name);
            markerState = conformMarkerState(target, item);
            if (markerState.status === "valid") {
                existingVerification = verifyConformMarkerInsertion(
                    target,
                    item,
                    sourceItem,
                    markerState
                );
                skippedPlans.push({
                    item: item,
                    source: source,
                    sourceItem: sourceItem,
                    markerState: markerState,
                    verification: existingVerification,
                    videoNodeId: asString(existingVerification.videoClip.nodeId),
                    audioNodeId: asString(existingVerification.audioClip.nodeId)
                });
                if (!skippedPlans[skippedPlans.length - 1].videoNodeId ||
                        !skippedPlans[skippedPlans.length - 1].audioNodeId) {
                    throw new Error(
                        "Marker [" + markerState.name +
                        "] source clips have no stable node IDs; conform cancelled."
                    );
                }
                continue;
            }
            existingVideo = trackSourceAt(
                target.videoTracks[item.videoTrackIndex],
                sourceItem,
                item.timelineSeconds
            );
            existingAudio = trackSourceAt(
                target.audioTracks[item.audioTrackIndex],
                sourceItem,
                item.timelineSeconds
            );
            if (existingVideo || existingAudio) {
                if (!existingVideo || !existingAudio) {
                    throw new Error(
                        "Batch found a partial source insertion without a valid " +
                        "RC_SYNC marker; both verified video and audio are required."
                    );
                }
                try {
                    existingVerification = verifyInsertedRange(
                        target,
                        sourceItem,
                        item
                    );
                } catch (orphanVerificationError) {
                    throw new Error(
                        "Batch found an existing source insertion without a valid " +
                        "RC_SYNC marker, but its source range is not exact: " +
                        orphanVerificationError.message
                    );
                }
                markerResult = upsertConformMarker(target, item);
                if (!markerResult.ok) {
                    throw new Error(
                        "Verified V/A content exists, but its RC_SYNC marker could not " +
                        "be recovered: " + asString(markerResult.error)
                    );
                }
                markerState = conformMarkerState(target, item);
                skippedPlans.push({
                    item: item,
                    source: source,
                    sourceItem: sourceItem,
                    markerState: markerState,
                    verification: existingVerification,
                    videoNodeId: asString(existingVerification.videoClip.nodeId),
                    audioNodeId: asString(existingVerification.audioClip.nodeId),
                    recoveredMissingMarker: true
                });
                if (!skippedPlans[skippedPlans.length - 1].videoNodeId ||
                        !skippedPlans[skippedPlans.length - 1].audioNodeId) {
                    throw new Error(
                        "Recovered source clips have no stable node IDs; conform cancelled."
                    );
                }
                continue;
            }
            placeholder = null;
            placeholderError = null;
            preparedGap = null;
            try {
                placeholder = placeholderAnywhere(target, item);
            } catch (placeholderLookupError) {
                if (item.placeholderIdentityRequired &&
                        asString(item.placeholderIdentity) &&
                        placeholderLookupError.message.indexOf(
                            "was not found on V"
                        ) >= 0) {
                    placeholderError = placeholderLookupError;
                } else {
                    throw placeholderLookupError;
                }
            }
            if (placeholder) {
                captureDownstreamItems(target, placeholder.endSeconds);
            }
            originalRange = saveRange(sourceItem);
            try {
                rangeSupport = setRange(
                    sourceItem,
                    item.sourceInSeconds,
                    item.sourceOutSeconds
                );
            } finally {
                restoreRange(sourceItem, originalRange);
            }
            actualDuration = rangeSupport.videoOutSeconds -
                rangeSupport.videoInSeconds;
            if (Math.abs(
                    actualDuration -
                    (rangeSupport.audioOutSeconds - rangeSupport.audioInSeconds)
                ) > 0.05) {
                throw new Error("Batch source V/A durations differ.");
            }
            if (!placeholder) {
                try {
                    preparedGap = preparedGapForInsertion(
                        target,
                        item,
                        actualDuration,
                        params
                    );
                    placeholder = preparedGap.placeholder;
                } catch (preparedGapError) {
                    throw new Error(
                        placeholderError.message +
                        " Prepared-gap recovery rejected: " +
                        preparedGapError.message + "."
                    );
                }
            }
            plan = {
                item: item,
                source: source,
                sourceItem: sourceItem,
                placeholder: placeholder,
                actualDurationSeconds: actualDuration,
                deltaSeconds: preparedGap ?
                    0 : actualDuration - placeholder.durationSeconds,
                rangeSupport: rangeSupport,
                preparedGap: !!preparedGap,
                finalStartSeconds: 0
            };
            if (placeholder.nodeId) {
                excludedNodeIds[placeholder.nodeId] = true;
            }
            plans.push(plan);
        }
        plans.sort(function (left, right) {
            return left.placeholder.startSeconds - right.placeholder.startSeconds;
        });
        for (i = 0; i < plans.length; i++) {
            plan = plans[i];
            plan.finalStartSeconds =
                plan.placeholder.startSeconds + cumulativeDelta;
            cumulativeDelta += plan.deltaSeconds;
            totalDelta += plan.deltaSeconds;
        }
        entries = captureAllTrackItems(target, excludedNodeIds);
        assignFinalBatchPositions(entries, plans);
        removedPlaceholderCount = removeBatchPlaceholders(target, plans);
        moveReport = moveBatchEntriesOnce(target, entries);
        verifyBatchEntryPositions(target, entries);

        for (i = 0; i < plans.length; i++) {
            plan = plans[i];
            if (!trackRangeIsClear(
                    target.videoTracks[plan.item.videoTrackIndex],
                    plan.finalStartSeconds,
                    plan.finalStartSeconds + plan.actualDurationSeconds
                ) || !trackRangeIsClear(
                    target.audioTracks[plan.item.audioTrackIndex],
                    plan.finalStartSeconds,
                    plan.finalStartSeconds + plan.actualDurationSeconds
                )) {
                throw new Error(
                    "Batch target interval is not empty for " +
                    asString(plan.item.segmentId) + "."
                );
            }
            originalRange = saveRange(plan.sourceItem);
            try {
                rangeSupport = setRange(
                    plan.sourceItem,
                    plan.item.sourceInSeconds,
                    plan.item.sourceOutSeconds
                );
                overwriteParams = {
                    sourceInSeconds: plan.item.sourceInSeconds,
                    sourceOutSeconds: plan.item.sourceOutSeconds,
                    timelineSeconds: plan.finalStartSeconds,
                    videoTrackIndex: plan.item.videoTrackIndex,
                    audioTrackIndex: plan.item.audioTrackIndex,
                    mode: "overwrite"
                };
                placement = placeRange(target, plan.sourceItem, overwriteParams);
                verification = verifyInsertedRange(
                    target,
                    plan.sourceItem,
                    overwriteParams
                );
            } finally {
                restoreRange(plan.sourceItem, originalRange);
            }
            plan.item.timelineSeconds = plan.finalStartSeconds;
            markerResult = upsertConformMarker(target, plan.item);
            if (!markerResult.ok) {
                throw new Error(
                    "Inserted " + asString(plan.item.segmentId) +
                    " but could not create its completion marker: " +
                    asString(markerResult.error)
                );
            }
            inserted.push({
                segmentId: asString(plan.item.segmentId),
                sourceSequenceId: asString(plan.source.sequenceID),
                sourceSequenceName: asString(plan.source.name),
                timelineSeconds: plan.finalStartSeconds,
                deltaSeconds: plan.deltaSeconds,
                placement: placement,
                verification: verification.report,
                marker: markerResult
            });
        }
        verifyBatchEntryPositions(target, entries);
        for (i = 0; i < skippedPlans.length; i++) {
            skippedPlan = skippedPlans[i];
            skippedPosition = verifySkippedInsertionAfterMove(
                target,
                skippedPlan
            );
            skippedPlan.item.timelineSeconds = skippedPosition.timelineSeconds;
            try {
                markerResult = reanchorConformMarker(
                    target,
                    skippedPlan.markerState.marker,
                    skippedPlan.item,
                    skippedPosition.timelineSeconds
                );
                skippedPlan.markerState.marker = markerResult.marker;
                markerResult = markerResult.report;
            } catch (markerRepairError) {
                markerResult = {
                    ok: false,
                    name: markerNameForItem(skippedPlan.item),
                    error: markerRepairError.message
                };
                warnings.push({
                    segmentId: asString(skippedPlan.item.segmentId),
                    warning: markerRepairError.message
                });
            }
            skipped.push({
                segmentId: asString(skippedPlan.item.segmentId),
                sourceSequenceId: asString(skippedPlan.source.sequenceID),
                sourceSequenceName: asString(skippedPlan.source.name),
                timelineSeconds: skippedPosition.timelineSeconds,
                alreadyConformed: true,
                recoveredMissingMarker: !!skippedPlan.recoveredMissingMarker,
                verification: skippedPosition.verification.report,
                marker: markerResult
            });
        }
        finalDuration = sequenceDurationSeconds(target);
        expectedFinalDuration = sequenceDurationBefore + totalDelta;
        if (Math.abs(finalDuration - expectedFinalDuration) > 0.1) {
            throw new Error(
                "One-pass batch duration mismatch. Expected " +
                expectedFinalDuration.toFixed(3) + "s, got " +
                finalDuration.toFixed(3) + "s."
            );
        }
        if (isFinite(Number(params.expectedTimelineDuration)) &&
                Math.abs(finalDuration - Number(params.expectedTimelineDuration)) > 0.1) {
            throw new Error(
                "Final sequence duration does not match the authoritative timeline. " +
                "Expected " + Number(params.expectedTimelineDuration).toFixed(3) +
                "s, got " + finalDuration.toFixed(3) + "s."
            );
        }
        return {
            target: sequenceMetadata(target),
            requested: items.length,
            completed: skipped.concat(inserted),
            completedCount: skipped.length + inserted.length,
            alreadyConformed: skipped,
            inserted: inserted,
            removedPlaceholders: removedPlaceholderCount,
            moveReport: moveReport,
            existingClipsVerified: entries.length,
            sequenceDurationBefore: sequenceDurationBefore,
            totalDeltaSeconds: totalDelta,
            expectedSequenceDurationAfter: expectedFinalDuration,
            sequenceDurationAfter: finalDuration,
            durationMatchesExpected: true,
            stopped: false,
            errors: [],
            warnings: warnings
        };
    }

    function placeRange(target, sourceItem, params) {
        var requestedMode = asString(params.mode || "overwrite");
        var mode = (
            requestedMode === "insert" || requestedMode === "ripple_replace"
        ) ? "insert" : "overwrite";
        var method = mode === "insert" ? "insertClip" : "overwriteClip";
        var videoTrack = target.videoTracks[params.videoTrackIndex];
        var audioTrack = target.audioTracks[params.audioTrackIndex];
        var videoBefore = trackClipCount(videoTrack);
        var audioBefore = trackClipCount(audioTrack);
        var usedApi = "";
        var firstError = "";
        var sequenceResult;
        var previousPlayerTicks = "";
        var playerPositionForced = false;
        var videoAfter;
        var audioAfter;

        try {
            if (typeof target[method] !== "function") {
                throw new Error("Sequence." + method + " is unavailable.");
            }
            if (mode === "insert") {
                sequenceResult = target.insertClip(
                    sourceItem,
                    timeFromSeconds(params.timelineSeconds),
                    params.videoTrackIndex,
                    params.audioTrackIndex
                );
            } else {
                try {
                    previousPlayerTicks = ticksOf(target.getPlayerPosition());
                } catch (ignorePlayerRead) {}
                if (typeof target.setPlayerPosition === "function") {
                    target.setPlayerPosition(secondsToTicks(params.timelineSeconds));
                    playerPositionForced = true;
                }
                sequenceResult = target.overwriteClip(
                    sourceItem,
                    timeFromSeconds(params.timelineSeconds),
                    params.videoTrackIndex,
                    params.audioTrackIndex
                );
            }
            if (sequenceResult === false) {
                throw new Error("Sequence." + method + " returned false.");
            }
            usedApi = "Sequence." + method;
        } catch (sequenceError) {
            firstError = sequenceError.message;
            try {
                if (mode === "insert") {
                    videoTrack.insertClip(
                        sourceItem,
                        secondsToTicks(params.timelineSeconds),
                        params.videoTrackIndex,
                        params.audioTrackIndex
                    );
                } else {
                    videoTrack.overwriteClip(sourceItem, secondsToTicks(params.timelineSeconds));
                }
                usedApi = "Track." + method + " fallback";
            } catch (videoError) {
                throw new Error(firstError + " Fallback failed: " + videoError.message);
            }

            videoAfter = trackClipCount(videoTrack);
            audioAfter = trackClipCount(audioTrack);
            if (audioAfter <= audioBefore) {
                try {
                    if (mode === "overwrite") {
                        audioTrack.overwriteClip(sourceItem, secondsToTicks(params.timelineSeconds));
                    }
                } catch (ignoreAudioFallback) {}
            }
        }

        videoAfter = trackClipCount(videoTrack);
        audioAfter = trackClipCount(audioTrack);
        if (previousPlayerTicks && typeof target.setPlayerPosition === "function") {
            try { target.setPlayerPosition(previousPlayerTicks); } catch (ignorePlayerRestore) {}
        }
        return {
            requestedMode: requestedMode,
            mode: mode,
            usedApi: usedApi,
            sequenceApiError: firstError,
            playerPositionForced: playerPositionForced,
            videoItemsBefore: videoBefore,
            videoItemsAfter: videoAfter,
            audioItemsBefore: audioBefore,
            audioItemsAfter: audioAfter,
            videoObserved: videoAfter > videoBefore ||
                !!trackSourceAt(videoTrack, sourceItem, params.timelineSeconds),
            audioObserved: audioAfter > audioBefore ||
                !!trackSourceAt(audioTrack, sourceItem, params.timelineSeconds),
            sequenceDurationAfter: sequenceDurationSeconds(target)
        };
    }

    function placeholderParamsAt(params, timelineSeconds) {
        return {
            timelineSeconds: timelineSeconds,
            videoTrackIndex: params.videoTrackIndex,
            placeholderIdentity: params.placeholderIdentity,
            placeholderIdentityRequired: params.placeholderIdentityRequired,
            placeholderName: params.placeholderName,
            placeholderNames: params.placeholderNames
        };
    }

    function insertThenRippleReplace(target, sourceItem, params) {
        var sequenceDurationBefore = sequenceDurationSeconds(target);
        var originalPlaceholder = placeholderForRipple(target, params);
        var existingVideo = trackSourceAt(
            target.videoTracks[params.videoTrackIndex],
            sourceItem,
            params.timelineSeconds
        );
        var existingAudio = trackSourceAt(
            target.audioTracks[params.audioTrackIndex],
            sourceItem,
            params.timelineSeconds
        );
        var placement;
        var verified;
        var durationAfterInsert;
        var expectedAfterInsert;
        var shiftedPlaceholderAt;
        var replacement;
        var finalDuration;
        var expectedFinalDuration;

        if (existingVideo || existingAudio) {
            throw new Error(
                "The source sequence is already present at the requested target position. " +
                "Undo any partial test insertion before retrying."
            );
        }

        placement = placeRange(target, sourceItem, params);
        verified = verifyInsertedRange(target, sourceItem, params);
        durationAfterInsert = sequenceDurationSeconds(target);
        expectedAfterInsert = sequenceDurationBefore +
            verified.report.expectedDurationSeconds;

        if (Math.abs(durationAfterInsert - expectedAfterInsert) > 0.1) {
            throw new Error(
                "Insert did not ripple the full target sequence. Expected duration " +
                expectedAfterInsert.toFixed(3) + "s after insert, got " +
                durationAfterInsert.toFixed(3) +
                "s. Original placeholder was preserved; undo the test insertion."
            );
        }

        shiftedPlaceholderAt = originalPlaceholder.startSeconds +
            verified.report.video.durationSeconds;
        replacement = removePlaceholderWithRipple(
            target,
            placeholderParamsAt(params, shiftedPlaceholderAt)
        );
        finalDuration = sequenceDurationSeconds(target);
        expectedFinalDuration = sequenceDurationBefore +
            verified.report.video.durationSeconds -
            originalPlaceholder.durationSeconds;

        return {
            placement: placement,
            verification: verified.report,
            replacement: replacement,
            sequenceDurationBefore: sequenceDurationBefore,
            sequenceDurationAfterInsert: durationAfterInsert,
            expectedSequenceDurationAfter: expectedFinalDuration,
            sequenceDurationAfter: finalDuration,
            durationMatchesExpected: Math.abs(finalDuration - expectedFinalDuration) <= 0.1
        };
    }

    function normalizeMarkerText(value) {
        return asString(value)
            .replace(/\r\n?/g, "\n")
            .replace(/[ \t]+/g, " ")
            .replace(/ *\n */g, "\n")
            .replace(/\n+/g, "\n")
            .replace(/^\s+|\s+$/g, "");
    }

    function markerNameForItem(item) {
        var markerName = "RC_SYNC " +
            asString(item.blockUid || item.segmentId);
        return markerName;
    }

    function markerSourceLine(item) {
        return "Source: " + asString(item.sourceSequenceName) +
            " [" + Number(item.sourceInSeconds).toFixed(3) +
            "–" + Number(item.sourceOutSeconds).toFixed(3) + "]";
    }

    function markerCommentForItem(item) {
        var comments = asString(item.speaker);
        if (comments && item.text) {
            comments += "\n";
        }
        comments += asString(item.text);
        comments += "\n" + markerSourceLine(item);
        if (isFinite(Number(item.confidence))) {
            comments += "\nConfidence: " +
                Math.round(Number(item.confidence) * 100) + "%";
        }
        return comments;
    }

    function markerPositionKey(marker) {
        var start = "";
        var end = "";
        try { start = ticksOf(marker.start); } catch (ignoreMarkerStart) {}
        try { end = ticksOf(marker.end); } catch (ignoreMarkerEnd) {}
        if (!start) {
            try { start = Number(secondsOf(marker.start)).toFixed(6); }
            catch (ignoreMarkerStartSeconds) {}
        }
        if (!end) {
            try { end = Number(secondsOf(marker.end)).toFixed(6); }
            catch (ignoreMarkerEndSeconds) {}
        }
        return asString(marker.name) + "\n" + start + "\n" + end + "\n" +
            asString(marker.comments);
    }

    function exactMarkersNamed(target, markerName) {
        var markers;
        var matches = [];
        var current;
        var next;
        var currentKey;
        var nextKey;
        var visited = 0;
        var maxMarkers = 10000;
        try {
            markers = target.markers;
        } catch (markerAccessError) {
            markers = null;
        }
        if (!markers ||
                typeof markers.getFirstMarker !== "function" ||
                typeof markers.getNextMarker !== "function") {
            throw new Error(
                "Sequence markers API is unavailable; conform cannot safely resume."
            );
        }
        current = markers.getFirstMarker();
        while (current) {
            visited += 1;
            if (visited > maxMarkers) {
                throw new Error(
                    "Sequence marker scan exceeded " + maxMarkers +
                    " entries; conform cancelled."
                );
            }
            if (asString(current.name) === markerName) {
                matches.push(current);
            }
            currentKey = markerPositionKey(current);
            next = markers.getNextMarker(current);
            if (next) {
                nextKey = markerPositionKey(next);
                if (next === current || nextKey === currentKey) {
                    throw new Error(
                        "Sequence marker scan did not advance at [" +
                        asString(current.name) + "]; conform cancelled."
                    );
                }
            }
            current = next;
        }
        return matches;
    }

    function conformMarkerState(target, item) {
        var markerName = markerNameForItem(item);
        var matches = exactMarkersNamed(target, markerName);
        var marker;
        var comments;
        var lines;
        var sourceLineIndex = -1;
        var actualText;
        var actualTextWithoutLabel;
        var expectedText;
        var expectedSource;
        var markerStart;
        var i;
        if (!matches.length) {
            return { status: "absent", name: markerName };
        }
        if (matches.length !== 1) {
            throw new Error(
                "Expected exactly one marker [" + markerName +
                "]; found " + matches.length + ". Conform cancelled."
            );
        }
        marker = matches[0];
        comments = asString(marker.comments).replace(/\r\n?/g, "\n");
        lines = comments.split("\n");
        expectedSource = normalizeMarkerText(markerSourceLine(item));
        for (i = 0; i < lines.length; i++) {
            if (normalizeMarkerText(lines[i]).indexOf("Source:") === 0) {
                if (sourceLineIndex !== -1) {
                    throw new Error(
                        "Marker [" + markerName +
                        "] has ambiguous Source metadata; conform cancelled."
                    );
                }
                sourceLineIndex = i;
            }
        }
        if (sourceLineIndex === -1 ||
                normalizeMarkerText(lines[sourceLineIndex]) !== expectedSource) {
            throw new Error(
                "Marker [" + markerName +
                "] Source sequence/range does not match; conform cancelled."
            );
        }
        actualText = normalizeMarkerText(
            lines.slice(0, sourceLineIndex).join("\n")
        );
        actualTextWithoutLabel = normalizeMarkerText(
            lines.slice(1, sourceLineIndex).join("\n")
        );
        expectedText = normalizeMarkerText(item.text);
        if (actualText !== expectedText &&
                actualTextWithoutLabel !== expectedText) {
            throw new Error(
                "Marker [" + markerName +
                "] item text does not match; conform cancelled."
            );
        }
        try {
            if (marker.start === null || marker.start === undefined) {
                throw new Error("missing marker start");
            }
            markerStart = secondsOf(marker.start);
        } catch (markerStartError) {
            throw new Error(
                "Marker [" + markerName +
                "] has no readable start position; conform cancelled."
            );
        }
        if (!isFinite(markerStart) || markerStart < 0) {
            throw new Error(
                "Marker [" + markerName +
                "] has an invalid start position; conform cancelled."
            );
        }
        return {
            status: "valid",
            name: markerName,
            source: expectedSource,
            timelineSeconds: markerStart,
            marker: marker
        };
    }

    function markerVerificationParams(item, timelineSeconds) {
        return {
            sourceInSeconds: Number(item.sourceInSeconds),
            sourceOutSeconds: Number(item.sourceOutSeconds),
            timelineSeconds: Number(timelineSeconds),
            videoTrackIndex: Number(item.videoTrackIndex),
            audioTrackIndex: Number(item.audioTrackIndex)
        };
    }

    function verifyConformMarkerInsertion(target, item, sourceItem, markerState) {
        var directError;
        var recovered;
        try {
            return verifyInsertedRange(
                target,
                sourceItem,
                markerVerificationParams(item, markerState.timelineSeconds)
            );
        } catch (verificationError) {
            directError = verificationError;
        }
        try {
            recovered = uniqueSourcePairAnywhere(target, item, sourceItem);
            if (recovered) {
                return recovered;
            }
        } catch (recoveryError) {
            throw new Error(
                "Marker [" + markerState.name +
                "] is stale or partial: " + recoveryError.message
            );
        }
        throw new Error(
            "Marker [" + markerState.name +
            "] is stale or partial: " + directError.message
        );
    }

    function clipMatchesSourceRange(clip, sourceItem, item) {
        var clipItem;
        var expectedNodeId = asString(sourceItem.nodeId);
        var metadata;
        var expectedDuration = Number(item.sourceOutSeconds) -
            Number(item.sourceInSeconds);
        try {
            clipItem = clip.projectItem;
        } catch (ignoreClipItem) {
            return false;
        }
        if (clipItem !== sourceItem &&
                (!expectedNodeId || !clipItem ||
                    asString(clipItem.nodeId) !== expectedNodeId)) {
            return false;
        }
        metadata = trackItemMetadata(clip);
        return !!metadata &&
            Math.abs(metadata.durationSeconds - expectedDuration) <= 0.08 &&
            Math.abs(metadata.sourceInSeconds -
                Number(item.sourceInSeconds)) <= 0.08 &&
            Math.abs(metadata.sourceOutSeconds -
                Number(item.sourceOutSeconds)) <= 0.08;
    }

    function matchingSourceClips(track, sourceItem, item) {
        var matches = [];
        var count;
        var i;
        try {
            count = Number(track.clips.numItems);
        } catch (countError) {
            count = 0;
        }
        if (count > 10000) {
            throw new Error(
                "Source-pair recovery scan exceeded 10000 clips."
            );
        }
        for (i = 0; i < count; i++) {
            if (clipMatchesSourceRange(track.clips[i], sourceItem, item)) {
                matches.push(track.clips[i]);
            }
        }
        return matches;
    }

    function uniqueSourcePairAnywhere(target, item, sourceItem) {
        var videos = matchingSourceClips(
            target.videoTracks[item.videoTrackIndex],
            sourceItem,
            item
        );
        var audios = matchingSourceClips(
            target.audioTracks[item.audioTrackIndex],
            sourceItem,
            item
        );
        var pairs = [];
        var videoStart;
        var audioStart;
        var verification;
        var i;
        var j;
        for (i = 0; i < videos.length; i++) {
            videoStart = secondsOf(videos[i].start);
            for (j = 0; j < audios.length; j++) {
                audioStart = secondsOf(audios[j].start);
                if (Math.abs(videoStart - audioStart) <= 0.05) {
                    pairs.push({
                        video: videos[i],
                        audio: audios[j],
                        timelineSeconds: videoStart
                    });
                    if (pairs.length > 1) {
                        throw new Error(
                            "More than one matching V/A source pair exists."
                        );
                    }
                }
            }
        }
        if (!pairs.length) {
            return null;
        }
        verification = verifyInsertedRange(
            target,
            sourceItem,
            markerVerificationParams(item, pairs[0].timelineSeconds)
        );
        if (asString(verification.videoClip.nodeId) !==
                    asString(pairs[0].video.nodeId) ||
                asString(verification.audioClip.nodeId) !==
                    asString(pairs[0].audio.nodeId)) {
            throw new Error(
                "Recovered V/A source pair identity is ambiguous."
            );
        }
        return verification;
    }

    function verifySkippedInsertionAfterMove(target, skippedPlan) {
        var item = skippedPlan.item;
        var videoTrack = target.videoTracks[item.videoTrackIndex];
        var audioTrack = target.audioTracks[item.audioTrackIndex];
        var videoClip = findTrackItemByNodeId(
            videoTrack,
            skippedPlan.videoNodeId
        );
        var audioClip = findTrackItemByNodeId(
            audioTrack,
            skippedPlan.audioNodeId
        );
        var videoStart;
        var audioStart;
        var verification;
        if (!videoClip || !audioClip) {
            throw new Error(
                "Previously conformed source pair [" +
                markerNameForItem(item) +
                "] disappeared during batch movement; conform cancelled."
            );
        }
        videoStart = secondsOf(videoClip.start);
        audioStart = secondsOf(audioClip.start);
        if (Math.abs(videoStart - audioStart) > 0.05) {
            throw new Error(
                "Previously conformed source pair [" +
                markerNameForItem(item) +
                "] lost V/A alignment; conform cancelled."
            );
        }
        try {
            verification = verifyInsertedRange(
                target,
                skippedPlan.sourceItem,
                markerVerificationParams(item, videoStart)
            );
        } catch (verificationError) {
            throw new Error(
                "Previously conformed source pair [" +
                markerNameForItem(item) +
                "] failed after batch movement: " +
                verificationError.message
            );
        }
        if ((skippedPlan.videoNodeId &&
                    asString(verification.videoClip.nodeId) !==
                    skippedPlan.videoNodeId) ||
                (skippedPlan.audioNodeId &&
                    asString(verification.audioClip.nodeId) !==
                    skippedPlan.audioNodeId)) {
            throw new Error(
                "Previously conformed source pair [" +
                markerNameForItem(item) +
                "] changed identity during batch movement; conform cancelled."
            );
        }
        return {
            timelineSeconds: videoStart,
            verification: verification
        };
    }

    function markerTimingMatches(marker, startSeconds, endSeconds) {
        var actualStart;
        var actualEnd;
        try {
            actualStart = secondsOf(marker.start);
            actualEnd = secondsOf(marker.end);
        } catch (readError) {
            return false;
        }
        return Math.abs(actualStart - Number(startSeconds)) <= 0.05 &&
            Math.abs(actualEnd - Number(endSeconds)) <= 0.05;
    }

    function reanchorConformMarker(target, marker, item, timelineSeconds) {
        var markerName = markerNameForItem(item);
        var expectedStart = Number(timelineSeconds);
        var expectedEnd = Number(timelineSeconds) +
            Number(item.sourceOutSeconds) -
            Number(item.sourceInSeconds);
        var preservedName = asString(marker.name);
        var preservedComments = asString(marker.comments);
        var markers;
        var temporary = null;
        var deleteResult;
        var exactMatches;
        var currentStart;
        var currentEnd;
        var actualStart;
        var actualEnd;
        try {
            currentStart = secondsOf(marker.start);
            currentEnd = secondsOf(marker.end);
            if (!isFinite(currentStart) || !isFinite(currentEnd) ||
                    currentEnd < currentStart) {
                throw new Error("marker has an invalid current range");
            }
            // Premiere can reject start > current end. When moving a marker
            // to the right beyond its old range, extend the end first.
            if (expectedStart > currentEnd) {
                marker.end = expectedEnd;
                marker.start = expectedStart;
            } else {
                marker.start = expectedStart;
                marker.end = expectedEnd;
            }
        } catch (ignoreDirectMarkerWrite) {}
        if (!markerTimingMatches(marker, expectedStart, expectedEnd)) {
            try {
                markers = target.markers;
                if (!markers ||
                        typeof markers.createMarker !== "function" ||
                        typeof markers.deleteMarker !== "function") {
                    throw new Error(
                        "marker delete/create API is unavailable"
                    );
                }
                temporary = markers.createMarker(expectedStart);
                if (!temporary) {
                    throw new Error("createMarker returned no marker");
                }
                temporary.name = "RC_SYNC_REANCHOR_TMP " +
                    asString(item.segmentId);
                temporary.comments = preservedComments;
                temporary.end = expectedEnd;
                if (!markerTimingMatches(
                        temporary,
                        expectedStart,
                        expectedEnd
                    )) {
                    try { markers.deleteMarker(temporary); }
                    catch (ignoreTemporaryDelete) {}
                    temporary = null;
                    throw new Error(
                        "temporary marker timing did not verify"
                    );
                }
                try {
                    deleteResult = markers.deleteMarker(marker);
                    if (deleteResult === false) {
                        throw new Error("deleteMarker returned false");
                    }
                } catch (oldMarkerDeleteError) {
                    try { markers.deleteMarker(temporary); }
                    catch (ignoreTemporaryCleanup) {}
                    temporary = null;
                    throw oldMarkerDeleteError;
                }
                temporary.name = preservedName;
                temporary.comments = preservedComments;
                marker = temporary;
                exactMatches = exactMarkersNamed(target, preservedName);
                if (exactMatches.length !== 1 ||
                        !markerTimingMatches(
                            exactMatches[0],
                            expectedStart,
                            expectedEnd
                        )) {
                    throw new Error(
                        "replacement marker did not become globally unique"
                    );
                }
                marker = exactMatches[0];
            } catch (recreateError) {
                throw new Error(
                    "Could not recreate marker [" + markerName +
                    "] at the verified source pair: " +
                    recreateError.message
                );
            }
        }
        if (asString(marker.name) !== preservedName ||
                asString(marker.comments) !== preservedComments ||
                !markerTimingMatches(marker, expectedStart, expectedEnd)) {
            throw new Error(
                "Premiere did not re-anchor marker [" + markerName +
                "] to the verified source pair."
            );
        }
        actualStart = secondsOf(marker.start);
        actualEnd = secondsOf(marker.end);
        return {
            marker: marker,
            report: {
                ok: true,
                name: markerName,
                startSeconds: actualStart,
                endSeconds: actualEnd,
                recreated: marker === temporary
            }
        };
    }

    function upsertConformMarker(target, item) {
        var markerName = markerNameForItem(item);
        var markerComments = markerCommentForItem(item);
        var markerStart = Number(item.timelineSeconds);
        var markerEnd = markerStart + Number(item.sourceOutSeconds) -
            Number(item.sourceInSeconds);
        var markers;
        var matches;
        var marker = null;
        var persistedMarker = null;
        try {
            markers = target.markers;
            if (!markers || typeof markers.createMarker !== "function") {
                return { ok: false, error: "Sequence markers API is unavailable." };
            }
            matches = exactMarkersNamed(target, markerName);
            if (matches.length > 1) {
                return {
                    ok: false,
                    error: "Expected at most one marker [" + markerName +
                        "]; found " + matches.length + "."
                };
            }
            if (matches.length === 1) {
                marker = matches[0];
            } else {
                marker = markers.createMarker(markerStart);
            }
            marker.name = markerName;
            marker.comments = markerComments;
            try {
                marker.end = markerEnd;
            } catch (ignoreMarkerEnd) {}
            matches = exactMarkersNamed(target, markerName);
            persistedMarker = matches.length === 1 ? matches[0] : null;
            // Premiere may return a fresh host proxy when the marker
            // collection is read again.  Object identity is therefore not a
            // persistence signal; verify the unique re-read marker's content.
            if (!persistedMarker ||
                    asString(persistedMarker.name) !== markerName ||
                    asString(persistedMarker.comments) !== markerComments ||
                    !markerTimingMatches(persistedMarker, markerStart, markerEnd)) {
                return {
                    ok: false,
                    error: "Premiere did not persist the exact completion marker [" +
                        markerName + "]."
                };
            }
            return { ok: true, name: markerName, marker: persistedMarker };
        } catch (error) {
            return { ok: false, error: error.message };
        }
    }

    function sequenceLayout(sequence, fromSeconds, toSeconds) {
        var result = {
            sequence: sequenceMetadata(sequence),
            fromSeconds: fromSeconds,
            toSeconds: toSeconds,
            video: [],
            audio: []
        };

        function readTracks(collection, prefix, destination) {
            var track;
            var clip;
            var clipStart;
            var clipEnd;
            var clips;
            var i;
            var j;
            for (i = 0; i < collection.numTracks; i++) {
                track = collection[i];
                clips = [];
                for (j = 0; j < track.clips.numItems; j++) {
                    clip = track.clips[j];
                    clipStart = secondsOf(clip.start);
                    clipEnd = secondsOf(clip.end);
                    if (clipEnd < fromSeconds || clipStart > toSeconds) {
                        continue;
                    }
                    clips.push({
                        name: asString(clip.name),
                        projectItemName: clip.projectItem ? asString(clip.projectItem.name) : "",
                        nodeId: asString(clip.nodeId),
                        startSeconds: clipStart,
                        endSeconds: clipEnd,
                        durationSeconds: Math.max(0, clipEnd - clipStart),
                        sourceInSeconds: secondsOf(clip.inPoint),
                        sourceOutSeconds: secondsOf(clip.outPoint)
                    });
                }
                destination.push({
                    track: prefix + (i + 1),
                    clipCount: Number(track.clips.numItems),
                    clips: clips
                });
            }
        }

        readTracks(sequence.videoTracks, "V", result.video);
        readTracks(sequence.audioTracks, "A", result.audio);
        return result;
    }

    $._roughCutStage2 = {
        ping: function () {
            if (!app.project) {
                return fail("No Premiere project is open.");
            }
            return success({
                host: asString(app.appName || "Premiere Pro"),
                version: asString(app.version),
                projectPath: projectPath()
            });
        },

        listProjects: function (encodedPayload) {
            var params;
            var root;
            var entries;
            var projects = [];
            var i;
            var name;
            try {
                params = parsePayload(encodedPayload);
                if (!params.projectsRoot) {
                    throw new Error("projectsRoot is required.");
                }
                root = new Folder(asString(params.projectsRoot));
                if (!root.exists) {
                    throw new Error("Projects folder does not exist: " + root.fsName);
                }
                entries = root.getFiles();
                for (i = 0; i < entries.length; i++) {
                    if (!(entries[i] instanceof Folder)) {
                        continue;
                    }
                    name = asString(entries[i].name);
                    if (validProjectName(name)) {
                        projects.push(name);
                    }
                }
                projects.sort();
                return success({
                    projects: projects,
                    projectsRoot: root.fsName
                });
            } catch (error) {
                return fail(error.message);
            }
        },

        inspectActiveSequence: function () {
            try {
                if (!app.project || !app.project.activeSequence) {
                    return fail("Open a sequence and make it active in Premiere.");
                }
                return success({ sequence: sequenceMetadata(app.project.activeSequence) });
            } catch (error) {
                return fail(error.message);
            }
        },

        inspectActiveSequenceLayout: function (encodedPayload) {
            var params;
            var sequence;
            var fromSeconds;
            var toSeconds;
            try {
                if (!app.project || !app.project.activeSequence) {
                    return fail("Open a sequence and make it active in Premiere.");
                }
                params = parsePayload(encodedPayload);
                sequence = app.project.activeSequence;
                fromSeconds = Number(params.fromSeconds);
                toSeconds = Number(params.toSeconds);
                if (!isFinite(fromSeconds) || fromSeconds < 0 ||
                        !isFinite(toSeconds) || toSeconds <= fromSeconds) {
                    throw new Error("Invalid layout inspection range.");
                }
                return success({
                    layout: sequenceLayout(sequence, fromSeconds, toSeconds)
                });
            } catch (error) {
                return fail(error.message);
            }
        },

        resolveSequence: function (encodedPayload) {
            try {
                return success({ sequence: sequenceMetadata(resolveSequence(parsePayload(encodedPayload))) });
            } catch (error) {
                return fail(error.message);
            }
        },

        insertSequenceRange: function (encodedPayload) {
            var params;
            var source;
            var target;
            var sourceItem;
            var originalRange;
            var rangeSupport;
            var placement;
            var replacement;
            var verification;
            var rippleResult;
            try {
                params = parsePayload(encodedPayload);
                params.sourceInSeconds = Number(params.sourceInSeconds);
                params.sourceOutSeconds = Number(params.sourceOutSeconds);
                params.timelineSeconds = Number(params.timelineSeconds);
                params.videoTrackIndex = Math.floor(Number(params.videoTrackIndex));
                params.audioTrackIndex = Math.floor(Number(params.audioTrackIndex));

                if (!isFinite(params.sourceInSeconds) || params.sourceInSeconds < 0 ||
                        !isFinite(params.sourceOutSeconds) || params.sourceOutSeconds <= params.sourceInSeconds) {
                    throw new Error("sourceOutSeconds must be greater than sourceInSeconds and both must be finite.");
                }
                if (!isFinite(params.timelineSeconds) || params.timelineSeconds < 0) {
                    throw new Error("timelineSeconds must be a finite non-negative number.");
                }
                if (params.videoTrackIndex < 0 || params.audioTrackIndex < 0) {
                    throw new Error("Track indices must be zero-based non-negative integers.");
                }

                source = resolveSequence(params);
                target = app.project ? app.project.activeSequence : null;
                if (!target) {
                    throw new Error("Open the target sequence and make it active.");
                }
                if (asString(source.sequenceID) === asString(target.sequenceID)) {
                    throw new Error("Source and target sequences must be different.");
                }
                if (params.sourceOutSeconds > sequenceDurationSeconds(source) + 0.001) {
                    throw new Error(
                        "Requested source out " + params.sourceOutSeconds.toFixed(3) +
                        "s exceeds source sequence duration " +
                        sequenceDurationSeconds(source).toFixed(3) + "s."
                    );
                }
                if (params.videoTrackIndex >= target.videoTracks.numTracks ||
                        params.audioTrackIndex >= target.audioTracks.numTracks) {
                    throw new Error("Target sequence does not have the requested video/audio track.");
                }

                sourceItem = source.projectItem;
                if (!sourceItem) {
                    throw new Error("Source sequence has no project item.");
                }

                originalRange = saveRange(sourceItem);
                try {
                    rangeSupport = setRange(sourceItem, params.sourceInSeconds, params.sourceOutSeconds);
                    if (params.mode === "ripple_replace") {
                        rippleResult = replaceWithExplicitTrackShift(
                            target,
                            sourceItem,
                            params,
                            rangeSupport
                        );
                        placement = rippleResult.placement;
                        replacement = rippleResult.replacement;
                        verification = rippleResult.verification;
                    } else {
                        placement = placeRange(target, sourceItem, params);
                    }
                } finally {
                    restoreRange(sourceItem, originalRange);
                }

                return success({
                    result: {
                        source: sequenceMetadata(source),
                        target: sequenceMetadata(target),
                        sourceInSeconds: params.sourceInSeconds,
                        sourceOutSeconds: params.sourceOutSeconds,
                        timelineSeconds: params.timelineSeconds,
                        videoTrackIndex: params.videoTrackIndex,
                        audioTrackIndex: params.audioTrackIndex,
                        rangeSupport: rangeSupport,
                        replacement: replacement || null,
                        placement: placement,
                        verification: verification || null,
                        rippleResult: rippleResult || null,
                        warning: (!placement.videoObserved || !placement.audioObserved) ?
                            "Premiere did not expose both inserted track items; inspect the target sequence before continuing." : ""
                    }
                });
            } catch (error) {
                return fail(error.message);
            }
        },

        conformSequenceRanges: function (encodedPayload) {
            var params;
            var items;
            var target;
            try {
                params = parsePayload(encodedPayload);
                if (!params.enableExplicitTrackShiftBatch) {
                    throw new Error(
                        "Batch conform is temporarily disabled until explicit cross-track shifting is pilot-verified."
                    );
                }
                items = params.items instanceof Array ? params.items : [];
                target = app.project ? app.project.activeSequence : null;
                if (!target) {
                    throw new Error("Open the Rough Cut target sequence and make it active.");
                }
                if (!params.expectedTargetSequenceId || !params.expectedProjectPath) {
                    throw new Error("Batch conform requires an inspected target sequence and project path.");
                }
                if (asString(target.sequenceID) !== asString(params.expectedTargetSequenceId)) {
                    throw new Error("Active target sequence changed after inspection; conform cancelled.");
                }
                if (normalizeFsPath(projectPath()) !== normalizeFsPath(params.expectedProjectPath)) {
                    throw new Error("Premiere project changed after inspection; conform cancelled.");
                }
                if (!items.length) {
                    throw new Error("No confirmed sync ranges were supplied.");
                }
                return success({
                    report: conformOnePass(target, items, params)
                });
            } catch (error) {
                return fail(error.message);
            }
        },

        exportActiveSequenceAudio: function (encodedPayload) {
            var params;
            var sequence;
            var outputFile;
            var presetPath;
            var presetFile;
            var jobId;
            try {
                params = parsePayload(encodedPayload);
                sequence = app.project ? app.project.activeSequence : null;
                if (!sequence) {
                    throw new Error("Open the sync sequence and make it active before export.");
                }
                if (params.expectedSequenceId &&
                        asString(sequence.sequenceID) !== asString(params.expectedSequenceId)) {
                    throw new Error("The active sequence changed after registration. Activate the stored sync sequence and retry.");
                }
                if (!params.outputPath) {
                    throw new Error("outputPath is required.");
                }
                outputFile = new File(asString(params.outputPath));
                if (!/\.wav$/i.test(outputFile.name)) {
                    throw new Error("Audio guide outputPath must end with .wav.");
                }
                if (outputFile.exists && !params.overwrite) {
                    throw new Error("Output WAV already exists. Enable overwrite or choose another path.");
                }
                if (outputFile.exists && params.overwrite && !outputFile.remove()) {
                    throw new Error("Existing output WAV could not be removed before export.");
                }
                if (!ensureFolder(outputFile.parent)) {
                    throw new Error("Cannot create output folder: " + outputFile.parent.fsName);
                }

                presetPath = asString(params.presetPath);
                if (!presetPath) {
                    presetPath = discoverAudioPreset();
                }
                if (!presetPath) {
                    throw new Error("Wave48mono24.epr was not found automatically. Choose an audio .epr preset manually.");
                }
                presetFile = new File(presetPath);
                if (!presetFile.exists || !/\.epr$/i.test(presetFile.name)) {
                    throw new Error("Encoder preset does not exist or is not an .epr file: " + presetPath);
                }
                if (!app.encoder || !app.encoder.encodeSequence) {
                    throw new Error("Premiere encoder API is unavailable.");
                }

                app.encoder.launchEncoder();
                jobId = app.encoder.encodeSequence(
                    sequence,
                    outputFile.fsName,
                    presetFile.fsName,
                    0,
                    1
                );
                if (!jobId || String(jobId) === "0") {
                    throw new Error("Adobe Media Encoder rejected the export job.");
                }
                app.encoder.startBatch();

                return success({
                    job: {
                        jobId: asString(jobId),
                        outputPath: outputFile.fsName,
                        presetPath: presetFile.fsName,
                        sequence: sequenceMetadata(sequence)
                    }
                });
            } catch (error) {
                return fail(error.message);
            }
        },

        fileInfo: function (encodedPayload) {
            var params;
            var file;
            var modified;
            try {
                params = parsePayload(encodedPayload);
                if (!params.path) {
                    throw new Error("path is required.");
                }
                file = new File(asString(params.path));
                modified = file.exists ? file.modified : null;
                return success({
                    file: {
                        path: file.fsName,
                        exists: file.exists,
                        size: file.exists ? Number(file.length) : 0,
                        modifiedMs: modified ? Number(modified.getTime()) : 0,
                        modifiedIso: modified ? modified.toUTCString() : ""
                    }
                });
            } catch (error) {
                return fail(error.message);
            }
        }
    };

    if ($._roughCutTestMode) {
        $._roughCutStage2Test = {
            placeholderAnywhere: placeholderAnywhere,
            preparedGapForInsertion: preparedGapForInsertion,
            conformMarkerState: conformMarkerState,
            verifyConformMarkerInsertion: verifyConformMarkerInsertion,
            reanchorConformMarker: reanchorConformMarker,
            upsertConformMarker: upsertConformMarker,
            validProjectName: validProjectName
        };
    }
})();

function rcStage2_ping() {
    return $._roughCutStage2.ping();
}

function rcStage2_listProjects(encodedPayload) {
    return $._roughCutStage2.listProjects(encodedPayload);
}

function rcStage2_inspectActiveSequence() {
    return $._roughCutStage2.inspectActiveSequence();
}

function rcStage2_inspectActiveSequenceLayout(encodedPayload) {
    return $._roughCutStage2.inspectActiveSequenceLayout(encodedPayload);
}

function rcStage2_resolveSequence(encodedPayload) {
    return $._roughCutStage2.resolveSequence(encodedPayload);
}

function rcStage2_insertSequenceRange(encodedPayload) {
    return $._roughCutStage2.insertSequenceRange(encodedPayload);
}

function rcStage2_conformSequenceRanges(encodedPayload) {
    return $._roughCutStage2.conformSequenceRanges(encodedPayload);
}

function rcStage2_exportActiveSequenceAudio(encodedPayload) {
    return $._roughCutStage2.exportActiveSequenceAudio(encodedPayload);
}

function rcStage2_fileInfo(encodedPayload) {
    return $._roughCutStage2.fileInfo(encodedPayload);
}
