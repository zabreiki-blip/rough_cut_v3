from pathlib import Path


HOST = (Path(__file__).parents[1] / "cep" / "host.jsx").read_text(encoding="utf-8")
PANEL = (Path(__file__).parents[1] / "cep" / "js" / "main.js").read_text(
    encoding="utf-8"
)
HTML = (Path(__file__).parents[1] / "cep" / "index.html").read_text(
    encoding="utf-8"
)


def test_project_picker_loads_existing_backend_projects() -> None:
    assert '<select id="projectName">' in HTML
    assert 'id="refreshProjects"' in HTML
    assert 'projects: "rcStage2_listProjects"' in PANEL
    assert 'callHost("projects", { projectsRoot: current.projectsRoot }' in PANEL
    assert 'request("GET", "/projects", null' in PANEL
    assert 'window.localStorage.setItem("roughCutStage2.project", select.value)' in PANEL
    assert "listProjects: function (encodedPayload)" in HOST
    assert "root.getFiles()" in HOST
    assert "function rcStage2_listProjects(encodedPayload)" in HOST


def test_whisper_job_progress_is_visible_and_polled_without_restarting_backend() -> None:
    assert 'id="whisperProgress" role="progressbar"' in HTML
    assert 'id="whisperStatus"' in HTML
    assert 'id="whisperSources"' in HTML
    assert 'id="whisperSegments"' in HTML
    assert 'id="whisperEta"' in HTML
    assert "function renderWhisperJob(job)" in PANEL
    assert "function etaFromProgress(elapsed, done, total)" in PANEL
    assert 'job.transcription_processed_sec' in PANEL
    assert '"Осталось: ≈ " + formatDuration(etaSeconds)' in PANEL
    assert '"/stage2/sync-jobs/status?project="' in PANEL
    assert "scheduleWhisperPoll(1000)" in PANEL
    assert 'className += " is-indeterminate"' in PANEL


def test_sequence_tick_strings_are_converted_to_seconds() -> None:
    assert 'typeof timeValue === "string" || typeof timeValue === "number"' in HOST
    assert 'if (typeof timeValue === "string")' in HOST
    assert "Number(timeValue) / TICKS_PER_SECOND" in HOST


def test_premiere_insert_and_overwrite_signatures_are_explicit() -> None:
    assert HOST.count("timeFromSeconds(params.timelineSeconds)") >= 2
    assert "videoTrack.insertClip(" in HOST
    assert "params.videoTrackIndex," in HOST
    assert "params.audioTrackIndex" in HOST


def test_batch_conform_requires_inspected_target_guards() -> None:
    assert "Batch conform requires an inspected target sequence and project path." in HOST
    assert "target.sequenceID" in HOST
    assert "expectedTargetSequenceId" in HOST
    assert "expectedProjectPath" in HOST
    assert 'callHost("inspect"' in PANEL
    assert "expectedTargetSequenceId: target.sequenceId" in PANEL
    assert "expectedProjectPath: target.projectPath" in PANEL


def test_source_switch_clears_identity_and_export_poll_retries() -> None:
    assert 'byId("sourceId").value = "";' in PANEL
    assert 'byId("speakerName").value = "";' in PANEL
    assert "hydrateSourceForm(sequence)" in PANEL
    assert "exportPollTimer = window.setTimeout(check, 2000);" in PANEL


def test_source_registration_supports_shared_and_all_speaker_sequences() -> None:
    html = (Path(__file__).parents[1] / "cep" / "index.html").read_text(encoding="utf-8")
    assert 'id="allSpeakers"' in html
    assert 'id="speakerName"' in html
    assert 'speakers: speakerNames.slice(1)' in PANEL
    assert 'all_speakers: byId("allSpeakers").checked' in PANEL
    assert "registeredSpeakers.concat(existing.speakers)" in PANEL


def test_batch_conform_preserves_editorial_context_as_sequence_markers() -> None:
    assert "upsertConformMarker(target, item)" in HOST
    assert "markers.createMarker" in HOST
    assert 'markerName = "RC_SYNC "' in HOST
    assert "text: segment.text" in PANEL
    assert "confidence: Number(segment.match_confidence" in PANEL


def test_ripple_replace_requires_exact_placeholder_then_inserts() -> None:
    assert "placeholderForRipple(target, params)" in HOST
    assert "found.clip.remove(1, 1)" in HOST
    assert "function sequenceDurationSeconds(sequence)" in HOST
    assert "track.clips[track.clips.numItems - 1].end" in HOST
    assert "projectName === expectedNames[j] || clipName === expectedNames[j]" in HOST
    assert "projectItem.setInPoint(secondsToTicks(inSeconds), 4)" in HOST
    assert "projectItem.getOutPoint(2)" in HOST
    assert "function verifyInsertedRange(" in HOST
    assert "function insertThenRippleReplace(" in HOST
    assert "Original placeholder was preserved" in HOST
    assert "function replaceWithExplicitTrackShift(" in HOST
    assert "shiftDownstreamItems(target, downstream, deltaSeconds)" in HOST
    assert "verifyDownstreamItems(target, downstream, deltaSeconds)" in HOST
    assert "found.clip.remove(0, 1)" in HOST
    assert "mode: \"overwrite\"" in HOST
    assert "target.setPlayerPosition(secondsToTicks(params.timelineSeconds))" in HOST
    assert "playerPositionForced: playerPositionForced" in HOST
    assert "alreadyConformed: !resumedReplacement" in HOST
    assert "resumedPartialReplacement: !!resumedReplacement" in HOST
    assert 'item.mode = "ripple_replace"' in HOST
    assert 'requestedMode === "ripple_replace"' in HOST
    assert "placeholderName: pathFilename(segment.placeholder_path)" in PANEL
    assert "placeholderNames: [" in PANEL
    assert "names.unshift(single)" in HOST
    assert "Resolve aliases in priority order instead of merging their matches" in HOST
    assert "if (matches.length === 1)" in HOST
    assert "Math.abs(matches[i].startSeconds - expectedStart) <= 0.08" in HOST
    assert "if (positionMatches.length === 1)" in HOST
    assert '<option value="ripple_replace">' in (
        Path(__file__).parents[1] / "cep" / "index.html"
    ).read_text(encoding="utf-8")


def test_cross_track_layout_diagnostic_and_conform_kill_switch() -> None:
    html = (Path(__file__).parents[1] / "cep" / "index.html").read_text(encoding="utf-8")
    assert "rcStage2_inspectActiveSequenceLayout" in PANEL
    assert "function sequenceLayout(" in HOST
    assert 'id="inspectLayout"' in html
    assert 'id="conformAll" class="button button-primary full-width">' in html
    assert "Batch conform is temporarily disabled" in HOST
    assert "function conformOnePass(" in HOST
    assert "moveBatchEntriesOnce(target, entries)" in HOST
    assert "enableExplicitTrackShiftBatch: true" in PANEL
