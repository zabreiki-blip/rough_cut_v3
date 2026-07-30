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
                if (projectName === expectedNames[j] || clipName === expectedNames[j]) {
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
        var names = params.placeholderNames instanceof Array ?
            params.placeholderNames.slice(0) : [];
        var single = asString(params.placeholderName);
        var result = [];
        var name;
        var seen = {};
        var i;
        if (single) {
            // The generated filename is the stable identity. Human-readable
            // clip labels are legacy fallbacks and may legitimately repeat
            // when one speaker has two quotes with the same source timecode.
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
        // Resolve aliases in priority order instead of merging their matches.
        // A stable generated filename can be unique while a legacy display
        // label such as "SPEAKER [00:12:43]" matches several valid clips.
        for (j = 0; j < names.length; j++) {
            matches = [];
            for (i = 0; i < track.clips.numItems; i++) {
                clip = track.clips[i];
                projectName = clip.projectItem ? asString(clip.projectItem.name) : "";
                clipName = asString(clip.name);
                if (projectName === names[j] || clipName === names[j]) {
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
            "Expected one globally unique placeholder [" + names.join(" | ") +
            "] on V" + (params.videoTrackIndex + 1) + "; found 0."
        );
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
        var i;
        plans.sort(function (left, right) {
            return right.placeholder.startSeconds - left.placeholder.startSeconds;
        });
        for (i = 0; i < plans.length; i++) {
            plan = plans[i];
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
        }
        plans.sort(function (left, right) {
            return left.placeholder.startSeconds - right.placeholder.startSeconds;
        });
    }

    function conformOnePass(target, items, params) {
        var sequenceDurationBefore = sequenceDurationSeconds(target);
        var plans = [];
        var skipped = [];
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
        var originalRange;
        var rangeSupport;
        var actualDuration;
        var plan;
        var moveReport;
        var overwriteParams;
        var placement;
        var verification;
        var markerResult;
        var inserted = [];
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
                    throw new Error("Batch found a partial existing V/A insertion.");
                }
                existingVerification = verifyInsertedRange(target, sourceItem, item);
                skipped.push({
                    segmentId: asString(item.segmentId),
                    sourceSequenceName: asString(source.name),
                    verification: existingVerification.report
                });
                continue;
            }
            placeholder = placeholderAnywhere(target, item);
            captureDownstreamItems(target, placeholder.endSeconds);
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
            plan = {
                item: item,
                source: source,
                sourceItem: sourceItem,
                placeholder: placeholder,
                actualDurationSeconds: actualDuration,
                deltaSeconds: actualDuration - placeholder.durationSeconds,
                rangeSupport: rangeSupport,
                finalStartSeconds: 0
            };
            excludedNodeIds[placeholder.nodeId] = true;
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
        removeBatchPlaceholders(target, plans);
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
        finalDuration = sequenceDurationSeconds(target);
        expectedFinalDuration = sequenceDurationBefore + totalDelta;
        if (Math.abs(finalDuration - expectedFinalDuration) > 0.1) {
            throw new Error(
                "One-pass batch duration mismatch. Expected " +
                expectedFinalDuration.toFixed(3) + "s, got " +
                finalDuration.toFixed(3) + "s."
            );
        }
        return {
            target: sequenceMetadata(target),
            requested: items.length,
            completed: skipped.concat(inserted),
            completedCount: skipped.length + inserted.length,
            alreadyConformed: skipped,
            inserted: inserted,
            removedPlaceholders: plans.length,
            moveReport: moveReport,
            existingClipsVerified: entries.length,
            sequenceDurationBefore: sequenceDurationBefore,
            totalDeltaSeconds: totalDelta,
            expectedSequenceDurationAfter: expectedFinalDuration,
            sequenceDurationAfter: finalDuration,
            durationMatchesExpected: true,
            stopped: false,
            errors: []
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

    function upsertConformMarker(target, item) {
        var markerName = "RC_SYNC " + asString(item.segmentId);
        var markers;
        var marker = null;
        var current;
        var comments;
        try {
            markers = target.markers;
            if (!markers || typeof markers.createMarker !== "function") {
                return { ok: false, error: "Sequence markers API is unavailable." };
            }
            try {
                current = markers.getFirstMarker();
                while (current) {
                    if (asString(current.name) === markerName) {
                        marker = current;
                        break;
                    }
                    current = markers.getNextMarker(current);
                }
            } catch (ignoreMarkerScan) {}
            if (!marker) {
                marker = markers.createMarker(Number(item.timelineSeconds));
            }
            marker.name = markerName;
            comments = asString(item.speaker);
            if (comments && item.text) {
                comments += "\n";
            }
            comments += asString(item.text);
            comments += "\nSource: " + asString(item.sourceSequenceName);
            comments += " [" + Number(item.sourceInSeconds).toFixed(3) +
                "–" + Number(item.sourceOutSeconds).toFixed(3) + "]";
            if (isFinite(Number(item.confidence))) {
                comments += "\nConfidence: " + Math.round(Number(item.confidence) * 100) + "%";
            }
            marker.comments = comments;
            try {
                marker.end = (
                    Number(item.timelineSeconds) +
                    Number(item.sourceOutSeconds) -
                    Number(item.sourceInSeconds)
                );
            } catch (ignoreMarkerEnd) {}
            return { ok: true, name: markerName };
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
                    if (/^[A-Za-z0-9_-]{1,64}$/.test(name)) {
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
            var report;
            var item;
            var source;
            var sourceItem;
            var originalRange;
            var rangeSupport;
            var placement;
            var replacement;
            var rippleResult;
            var existingVideo;
            var existingAudio;
            var existingVerification;
            var resumeParams;
            var resumedReplacement;
            var markerResult;
            var i;
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
                items.sort(function (left, right) {
                    return Number(left.timelineSeconds) - Number(right.timelineSeconds);
                });
                report = {
                    target: sequenceMetadata(target),
                    requested: items.length,
                    completed: [],
                    errors: [],
                    stopped: false
                };

                for (i = 0; i < items.length; i++) {
                    item = items[i];
                    rangeSupport = null;
                    placement = null;
                    replacement = null;
                    rippleResult = null;
                    existingVerification = null;
                    resumedReplacement = null;
                    item.sourceInSeconds = Number(item.sourceInSeconds);
                    item.sourceOutSeconds = Number(item.sourceOutSeconds);
                    item.timelineSeconds = Number(item.timelineSeconds);
                    item.videoTrackIndex = Math.floor(Number(
                        item.videoTrackIndex === undefined ? params.videoTrackIndex : item.videoTrackIndex
                    ));
                    item.audioTrackIndex = Math.floor(Number(
                        item.audioTrackIndex === undefined ? params.audioTrackIndex : item.audioTrackIndex
                    ));
                    item.mode = "ripple_replace";
                    try {
                        if (!isFinite(item.sourceInSeconds) || item.sourceInSeconds < 0 ||
                                !isFinite(item.sourceOutSeconds) || item.sourceOutSeconds <= item.sourceInSeconds ||
                                !isFinite(item.timelineSeconds) || item.timelineSeconds < 0) {
                            throw new Error("Invalid source/timeline range.");
                        }
                        if (item.videoTrackIndex < 0 || item.audioTrackIndex < 0 ||
                                item.videoTrackIndex >= target.videoTracks.numTracks ||
                                item.audioTrackIndex >= target.audioTracks.numTracks) {
                            throw new Error("Requested target track does not exist.");
                        }
                        source = resolveSequence(item);
                        if (asString(source.sequenceID) === asString(target.sequenceID)) {
                            throw new Error("Source sequence is the active target sequence.");
                        }
                        if (item.sourceOutSeconds > sequenceDurationSeconds(source) + 0.001) {
                            throw new Error(
                                "Source out " + item.sourceOutSeconds.toFixed(3) +
                                "s exceeds sequence duration " +
                                sequenceDurationSeconds(source).toFixed(3) + "s."
                            );
                        }
                        sourceItem = source.projectItem;
                        if (!sourceItem) {
                            throw new Error("Source sequence has no project item.");
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
                                    "A partial source insertion already exists at the target position. " +
                                    "Restore or inspect this segment before continuing."
                                );
                            }
                            existingVerification = verifyInsertedRange(target, sourceItem, item);
                            resumeParams = placeholderParamsAt(
                                item,
                                item.timelineSeconds +
                                    existingVerification.report.video.durationSeconds
                            );
                            try {
                                placeholderForRipple(target, resumeParams);
                                resumedReplacement = removePlaceholderWithRipple(target, resumeParams);
                            } catch (resumeError) {
                                if (resumeError.message.indexOf("; found 0.") < 0) {
                                    throw resumeError;
                                }
                            }
                            markerResult = upsertConformMarker(target, item);
                            report.completed.push({
                                segmentId: asString(item.segmentId),
                                sourceSequenceId: asString(source.sequenceID),
                                sourceSequenceName: asString(source.name),
                                timelineSeconds: item.timelineSeconds,
                                alreadyConformed: !resumedReplacement,
                                resumedPartialReplacement: !!resumedReplacement,
                                replacement: resumedReplacement,
                                verification: existingVerification.report,
                                marker: markerResult
                            });
                            continue;
                        }
                        originalRange = saveRange(sourceItem);
                        try {
                            rangeSupport = setRange(sourceItem, item.sourceInSeconds, item.sourceOutSeconds);
                            rippleResult = insertThenRippleReplace(target, sourceItem, item);
                            replacement = rippleResult.replacement;
                            placement = rippleResult.placement;
                        } finally {
                            restoreRange(sourceItem, originalRange);
                        }
                        if (!placement.videoObserved || !placement.audioObserved) {
                            report.errors.push({
                                segmentId: asString(item.segmentId),
                                error: "Premiere did not expose both video and audio after placement.",
                                placement: placement
                            });
                            report.stopped = true;
                            break;
                        }
                        markerResult = upsertConformMarker(target, item);
                        report.completed.push({
                            segmentId: asString(item.segmentId),
                            sourceSequenceId: asString(source.sequenceID),
                            sourceSequenceName: asString(source.name),
                            timelineSeconds: item.timelineSeconds,
                            rangeSupport: rangeSupport,
                            replacement: replacement,
                            placement: placement,
                            verification: rippleResult.verification,
                            rippleResult: rippleResult,
                            marker: markerResult
                        });
                    } catch (itemError) {
                        report.errors.push({
                            segmentId: asString(item.segmentId),
                            error: itemError.message
                        });
                        report.stopped = true;
                        break;
                    }
                }
                return success({ report: report });
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
            placeholderAnywhere: placeholderAnywhere
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
