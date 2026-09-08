from pathlib import Path
import re


HOST = (Path(__file__).parents[1] / "cep" / "host.jsx").read_text(encoding="utf-8")
PANEL = (Path(__file__).parents[1] / "cep" / "js" / "main.js").read_text(encoding="utf-8")
HTML = (Path(__file__).parents[1] / "cep" / "index.html").read_text(encoding="utf-8")


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
    assert "job.transcription_processed_sec" in PANEL
    assert '"Осталось: ≈ " + formatDuration(etaSeconds)' in PANEL
    assert '"/stage2/sync-jobs/status?project="' in PANEL
    assert "scheduleWhisperPoll(1000)" in PANEL
    assert 'className += " is-indeterminate"' in PANEL
    assert "force_transcribe: true" not in PANEL


def test_premiere_panel_explains_that_review_matches_need_browser_confirmation() -> None:
    assert 'id="whisperReviewNotice"' in HTML
    assert 'id="whisperReviewNoticeCount"' in HTML
    assert "Неподтверждённые СНХ не применяются в Premiere" in HTML
    assert 'segment.match_status === "needs_review"' in PANEL
    assert "СНХ требуют подтверждения в браузере" in PANEL
    assert "До подтверждения они не будут применены в Premiere" in PANEL


def test_premiere_panel_explains_safe_resume_after_an_interruption() -> None:
    assert "повторный запуск проверит маркеры RC_SYNC" in HTML
    assert 'interrupted: "прервано"' in PANEL
    assert "Сохранённые подтверждения не потеряны" in PANEL
    assert 'currentWhisperJobStatus === "queued"' in PANEL
    assert 'currentWhisperJobStatus === "interrupted"' in PANEL
    assert "Сначала возобновите сопоставление СНХ в браузере" in PANEL
    assert "var changedTimeline = /^Inserted\\s+/i.test(hostError.message);" in PANEL
    assert "повторный запуск проверит V/A и восстановит маркер" in PANEL
    assert 'id="resumeWhisper"' in HTML
    assert 'id="cancelWhisper"' in HTML
    assert '"/cancel"' in PANEL
    assert "Продолжаю распознавание без повторного экспорта WAV" in PANEL


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


def test_sequence_roles_block_self_transcription_and_conform_into_sources() -> None:
    assert "function sequenceRoughCutSignals(sequence)" in HOST
    assert 'label.indexOf("RC_BLOCK ") === 0' in HOST
    assert 'asString(marker.name).indexOf("RC_SYNC ") === 0' in HOST
    assert "roughCutGuard: sequenceRoughCutSignals(sequence)" in HOST
    assert "Монтажную секвенцию с RC_BLOCK/RC_SYNC нельзя экспортировать" in HOST
    assert "sourceRegistrationBlockReason(activeSource)" in PANEL
    assert "sequence_role: source.roughCutGuard.isRoughCutTarget" in PANEL
    assert "function captureSourceOperation(guidePath)" in PANEL
    assert "function sourceIdentityMatchesSequence(source, sequence)" in PANEL
    assert '"/stage2/sync-sources?project="' in PANEL
    assert "registeredSourceGuards: sourceGuards" in PANEL
    assert "registeredSourceMatch(" in HOST
    assert "assertSourceIsNotTarget(target, source);" in HOST
    assert HOST.index("assertSourceIsNotTarget(target, source);") < HOST.index(
        "markerState = conformMarkerState(target, item);"
    )


def test_source_switch_clears_identity_and_export_poll_retries() -> None:
    assert 'byId("sourceId").value = "";' in PANEL
    assert 'byId("speakerName").value = "";' in PANEL
    assert "hydrateSourceForm(sequence)" in PANEL
    assert "exportPollTimer = window.setTimeout(check, 2000);" in PANEL
    assert "xhr.timeout = 30000;" in PANEL
    assert "xhr.ontimeout" in PANEL
    assert "exportInProgress = true;" in PANEL
    assert 'byId("exportAndTranscribe").disabled = true;' in PANEL


def test_source_registration_supports_shared_and_all_speaker_sequences() -> None:
    html = (Path(__file__).parents[1] / "cep" / "index.html").read_text(encoding="utf-8")
    assert 'id="allSpeakers"' in html
    assert 'id="speakerName"' in html
    assert "speakers: speakerNames.slice(1)" in PANEL
    assert "all_speakers: operationContext.allSpeakers" in PANEL
    assert "registeredSpeakers.concat(existing.speakers)" in PANEL


def test_batch_conform_preserves_editorial_context_as_sequence_markers() -> None:
    assert "upsertConformMarker(target, item)" in HOST
    assert "markers.createMarker" in HOST
    assert 'markerName = "RC_SYNC "' in HOST
    assert "text: segment.text" in PANEL
    assert "confidence: Number(segment.match_confidence" in PANEL


def test_batch_conform_resumes_only_from_semantically_exact_unique_markers() -> None:
    assert "function conformMarkerState(target, item)" in HOST
    assert "Expected exactly one marker [" in HOST
    assert 'status: sourceMatches ? "valid" : "stale_source"' in HOST
    assert "item text does not match" in HOST
    assert "Sequence marker scan did not advance" in HOST
    assert "Sequence marker scan exceeded " in HOST
    assert "verifyConformMarkerInsertion(" in HOST
    assert "verifySkippedInsertionAfterMove(" in HOST
    assert "findTrackItemByNodeId(" in HOST
    assert "reanchorConformMarker(" in HOST
    assert 'without a valid " +' in HOST
    assert "source clips have no stable node IDs" in HOST
    marker_check = HOST.index("markerState = conformMarkerState(target, item);")
    placeholder_lookup = HOST.index(
        "placeholder = placeholderAnywhere(target, item);",
        marker_check,
    )
    assert marker_check < placeholder_lookup
    assert "recoveredMissingMarker: true" in HOST
    assert 'markerState.status === "absent"' in HOST
    assert "recoveredAnywhere = uniqueSourcePairAnywhere(" in HOST
    assert "recoveredAwayFromExpectedPosition: true" in HOST
    assert "both verified video and audio are required" in HOST
    assert "RC_STALE_INSERTION_CONFIRM_REQUIRED" in HOST
    assert "repairStaleInsertions" in PANEL
    assert "staleSourcePairForRepair(" in HOST
    assert "uniqueStaleSourcePairAnywhere(" in HOST
    assert "More than one compatible stale V/A source pair exists" in HOST
    assert 'markerState.status === "stale_source"' in HOST
    assert "markerState.timelineSeconds" in HOST
    assert "Premiere did not persist the exact completion marker" in HOST
    assert "Object identity is therefore not a" in HOST
    assert "asString(persistedMarker.name)" in HOST
    assert "Final sequence duration does not match the authoritative timeline" in HOST


def test_batch_conform_uses_stable_block_identity_for_fresh_placeholders() -> None:
    assert '"RC_BLOCK " + String(segment.block_uid)' in PANEL
    assert "placeholderIdentity: placeholderIdentity" in PANEL
    assert "var stableIdentity = asString(params.placeholderIdentity);" in HOST
    assert "return [stableIdentity];" in HOST
    assert "Target sequence is stale or legacy" in HOST
    assert "Re-import the latest XML" in HOST
    assert "item.blockUid || item.segmentId" in HOST


def test_ripple_replace_requires_exact_placeholder_then_inserts() -> None:
    assert "placeholderForRipple(target, params)" in HOST
    assert "found.clip.remove(1, 1)" in HOST
    assert "function sequenceDurationSeconds(sequence)" in HOST
    assert "track.clips[track.clips.numItems - 1].end" in HOST
    assert "placeholderLabelMatches(projectName, expectedNames[j])" in HOST
    assert "placeholderLabelMatches(clipName, expectedNames[j])" in HOST
    assert "projectItem.setInPoint(secondsToTicks(inSeconds), 4)" in HOST
    assert "projectItem.getOutPoint(2)" in HOST
    assert "function verifyInsertedRange(" in HOST
    assert "function insertThenRippleReplace(" in HOST
    assert "Original placeholder was preserved" in HOST
    assert "function replaceWithExplicitTrackShift(" in HOST
    assert "shiftDownstreamItems(target, downstream, deltaSeconds)" in HOST
    assert "verifyDownstreamItems(target, downstream, deltaSeconds)" in HOST
    assert "found.clip.remove(0, 1)" in HOST
    assert 'mode: "overwrite"' in HOST
    assert "target.setPlayerPosition(secondsToTicks(params.timelineSeconds))" in HOST
    assert "playerPositionForced: playerPositionForced" in HOST
    assert "alreadyConformed: true" in HOST
    assert "recoveredMissingMarker: !!skippedPlan.recoveredMissingMarker" in HOST
    assert 'mode: "overwrite"' in HOST
    assert 'requestedMode === "ripple_replace"' in HOST
    assert "placeholderName: pathFilename(segment.placeholder_path)" in PANEL
    assert "placeholderNames: [" in PANEL
    assert "names.unshift(single)" in HOST
    assert "Canonical RC_BLOCK identity is the only candidate" in HOST
    assert "if (matches.length === 1)" in HOST
    assert "Math.abs(matches[i].startSeconds - expectedStart) <= 0.08" in HOST
    assert "if (positionMatches.length === 1)" in HOST
    assert '<option value="ripple_replace">' not in HTML


def test_production_panel_hides_manual_diagnostics_and_keeps_conform() -> None:
    assert "rcStage2_inspectActiveSequenceLayout" not in PANEL
    assert "function sequenceLayout(" in HOST
    assert 'id="inspectLayout"' not in HTML
    assert 'id="insertRange"' not in HTML
    assert 'id="sourceIn"' not in HTML
    assert 'id="sourceOut"' not in HTML
    assert 'id="timelineAt"' not in HTML
    assert 'id="insertMode"' not in HTML
    assert 'id="placeholderName"' not in HTML
    assert 'id="videoTrack"' not in HTML
    assert 'id="audioTrack"' not in HTML
    assert 'id="conformAll"' in HTML
    assert 'id="conformStatus"' in HTML
    assert "Batch conform is temporarily disabled" in HOST
    assert "function conformOnePass(" in HOST
    assert "moveBatchEntriesOnce(target, entries)" in HOST
    assert "enableExplicitTrackShiftBatch: true" in PANEL


def test_panel_literal_dom_references_have_matching_elements() -> None:
    referenced_ids = set(re.findall(r'byId\("([^"]+)"\)', PANEL))
    html_ids = set(re.findall(r'id="([^"]+)"', HTML))
    assert referenced_ids <= html_ids


def test_panel_has_no_workstation_specific_defaults() -> None:
    assert "/Users/mac/" not in PANEL
    assert '|| "BRIK"' not in PANEL
