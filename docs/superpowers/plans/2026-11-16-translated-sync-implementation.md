# Translated Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Execution status (16.11.2026):** Tasks 1–4 implemented and automatically verified. Full suite: 3366 passed + 35 subtests. The original checklists below are retained as the execution specification; final evidence, TDD limitations, review corrections and manual acceptance boundaries are in [completion report](2026-11-16-translated-sync-completion.md). No commits or live-provider/project operations.

**Goal:** Import timed Russian Word transcripts for foreign syncs and assemble generated/recorded Russian interpretation over original media, extending slots with black video.

**Architecture:** Keep scenario normalization unchanged. Import Word as independent timed cue data attached to existing sync sources; a dedicated matching action writes ordinary reviewable SyncMatch records. Store translation variants in match metadata, project them into additive timeline audio fields, and export a separate translation lane in Stage 3.

**Tech Stack:** Existing Python 3.11, Flask, python-docx, ffmpeg, vanilla ES modules, pytest, FCP7 XML.

**Spec:** docs/superpowers/plans/2026-11-16-translated-sync-voiceover-draft.md

## Global Constraints

- Inputs are existing review media + exported DOCX + Russian scenario; no new JSON export or scenario ID markup required.
- Fuzzy Russian matching, source/speaker/time hints, manual review; never claim interpolated Russian words are exact foreign word timestamps.
- Both TTS-first and recorded replacement; one selected translation audio per sync, independent from narration.
- Longer translation automatically extends slot, shifts later items and leaves black video. Original source in/out stays unchanged; no duration-conflict gate.
- Preserve unrelated working-tree changes. No commits/pushes, installs, paid/media generation or live project mutation by agents. Use fixtures for external calls.
- TDD for feature behavior. Use .venv/bin/python -m pytest; parent runs broad verification once after integration.

## Review Focus

- Real TRANSCRIBE DOCX has role+speaker on one line, TC+original on next, and unlabeled bold Russian translation in a separate paragraph.
- Sparse starts and duplicate times represent coarse windows; derive end from next strictly greater start or explicit media duration, never fabricate certainty.
- Same quote across speakers/sources must remain reviewable; preserve confirmed manual matches on reimport/rematching.
- Source duration differs from timeline slot: black extension and final sequence duration must include all translation audio, without drift on rebuild/replacement.
- Stale text, missing selected audio, recorded in/out and content changes must invalidate build; no silent fallback.

---

### Task 1: Word cues and fuzzy matching domain

**Files:** Create src/translated_sync/__init__.py, transcript.py, matching.py; tests/test_translated_sync_transcript.py, tests/test_translated_sync_matching.py.

**Interfaces:**
- TranscriptCue dataclass in transcript.py: id:str, source_id:str, speaker:str, russian_text:str, original_text:str, start_sec:float, end_sec:float, approximate:bool, origin_index:int; to_dict() and from_dict(dict).
- parse_docx_transcript(path:Path, source_id:str, *, duration_sec:float, fps:float=25.0, timecode_mode:str="relative", sequence_start_sec:float=0.0) -> list[TranscriptCue].
- match_translated_quote(request:AlignmentRequest, cues:Sequence[TranscriptCue], sources:Sequence[SyncSource]) -> SyncMatch. Full-cue/window ranges, needs_review for candidates, not_found otherwise; strategy translated_docx; metadata document cue provenance. Caller adds revision/unit hash.

- [ ] Write and run failing tests with synthetic DOCX matching real export layout: source headings, ОТВЕТ speaker newline [TC] original, bold unlabeled Russian translation, optional pronunciation to exclude; Russian-only; explicit range; table rows; sparse TC; display offset; malformed/out-of-bounds input.
- [ ] Implement parsing without flattening bilingual text into one search string. Retain coarse-window precision; no timed text or no Russian candidates yields actionable ValueError. Stable cue IDs depend on source/content/timing, not filename alone.
- [ ] Write and run failing tests for shortened/reordered Russian text, repeated quotes across speakers and source hints, approximate TC, no overlap not_found; then implement bounded scoring with existing Russian normalization. Preserve alternative candidates. No ASR/LLM calls.
- [ ] Run focused tests; write report with RED/GREEN evidence. No commit.

### Task 2: Translation audio, timeline extension and XML

**Files:** Create src/translated_sync/audio.py; modify src/stage2/models.py, timeline_parser.py, assembly_plan.py, xml_export.py; src/stage3/nested_xml.py, builds.py; src/stage_common.py, src/api_stage3_routes.py, src/api_stage2_routes.py (legacy XML error only); tests/test_translated_sync_timeline.py, tests/test_translated_sync_xml.py (plus appropriate existing tests if needed).

**Interfaces:**
- Match metadata translation_audio stores active (tts|recorded), delay_sec=0.8, duck_db=-18.0, gain_db=0.0, fade_sec=0.2, variants map. Each variant has path, source_in_sec, source_out_sec, duration_sec (entire WAV), sha256, text_sha256=sha256(stripped match.query_text UTF8).
- audio.text_sha256(text:str)->str; audio.resolve_translation_audio(config:dict, query_text:str)->dict returns selected validated projection: kind,path,source_in_sec,source_out_sec,source_duration_sec,sha256,text_sha256,delay_sec,duck_db,gain_db,fade_sec. Reject invalid/stale/missing selection; unselected variants retained only in match store.
- TimelineSegment.translation_audio: dict|None, additive default None; output projection also includes timeline_offset_sec computed from speech head padding + delay.

- [ ] Tests first: source range [5,15], speech starts 5, translation [0,14], delay .8 => source video/audio 10 sec, slot 14.8 sec rounded UP to frames, later items shift; repeat build idempotent; final sequence duration full; replacing translation with shorter recording recomputes.
- [ ] Resolve metadata before merging sync fragments; don't merge independently translated units. Apply extension after padding trim. Preserve original source ranges. Ensure old projects remain unchanged.
- [ ] Assembly plan includes translation_audio, validates selected WAV/range/hash and stale text. Build input WAV hashing covers translation assets including host/container path resolution. Extend stage_common.sync_matches_fingerprint to include translation_audio (currently arbitrary metadata is omitted), and api_stage3_routes path conversion for the nested translation path. No duration overflow error.
- [ ] XML third audio lane for translation; source nested clip ends after original duration, leaving video gap; independent translation in/out and slot offset, no freeze/repeat. Emit editable volume/gain envelope on overlapping original and gain on translation. Keep regular ZK/source track behavior.
- [ ] Legacy Stage2 XML explicitly rejects translated audio with direction to final Stage3 export, rather than omitting it. Update exporter version if necessary for freshness.
- [ ] Run focused + prior timeline/XML/assembly tests; report RED/GREEN. No commit.

### Task 3: Project API and audio preparation

**Files:** Create src/api_translated_sync_routes.py and src/translated_sync/service.py; minimally register routes in src/api.py; tests/test_translated_sync_api.py. May modify src/api_stage2_routes.py only if required for accurate build readiness/invalidation; preserve all existing edits elsewhere.

**Interfaces:**
- register_translated_sync_routes(app, services) follows existing auth/error/active-job guards.
- GET/POST /stage2/sync-sources/<source_id>/transcript-docx: multipart file, project, timecode_mode, optional offset via sequence_start_tc; store cues JSON under work/translated_sync/transcripts/<safe source_id>.json, original DOCX retained; GET returns import summary/cues. Import validates source and duration and preserves previous valid import on failure.
- POST /stage2/translated-sync/match JSON project (optional source_ids): uses confirmed _sync_alignment_units, domain matcher, SyncMatchStore, normal revision/unit-text metadata. Preserve current confirmed matches and successful ordinary auto_matched results; replace unconfirmed affected candidates within selected sources. No standard ASR launch. If selected imports mix relative/display modes, require an explicit timecode_mode or per-source action rather than assuming relative.
- GET/PATCH /stage2/translated-sync/<segment_id>/audio: returns variants and accepts enabled:boolean (default true), active/settings or recorded source ranges. Disabled translation retains variants but produces no overlay or enlarged slot; re-enabling validates assets/text. Unavailable variants are errors, not fallback.
- POST /stage2/translated-sync/<segment_id>/tts: use services.synthesize_phrase with text from current unit, provider/voice/speed/model/style, existing budget/usage conventions, fingerprinted immutable file; store variant atomically then select TTS.
- POST /stage2/translated-sync/<segment_id>/recording multipart audio + optional source_in_sec/source_out_sec: normalized project WAV via existing recording preparation; store variant and select recorded. Also allow assigning an already prepared project recording via recording_id+range in PATCH so one recording can serve several syncs.
- GET /stage2/translated-sync/<segment_id>/audio-file streams chosen owned audio (optional kind), validates project-relative containment; no arbitrary host paths.

- [ ] Tests first: authenticated import/match on isolated project, invalid DOCX keeps old import, confirmed manual range survives reimport, repeated imports, unknown source/segment/revision/active job, and safe filenames/paths.
- [ ] Add source transcript metadata summary to existing source store for UI. Save cue/document hashes; if document changed, invalidate affected unconfirmed candidates and require reconfirmation of affected confirmed translated matches without overwriting manual ranges.
- [ ] Add generation/upload/switching tests with only external synthesis stubbed, real small WAVs; validate finite in/out vs actual duration, current text hash and source selection. Reuse existing budget controls and record usage. No paid tests or generation.
- [ ] Audio selection saved in SyncMatch.metadata, so existing sync fingerprint invalidates timeline after changes. Ensure full build includes translation data via Task 2. Report focused tests and limitations. No commit.

### Task 4: Browser workflow, integration and user guide

**Files:** Create src/static/stage1/translated_sync.js; minimally modify src/static/stage1/sync.js and/or src/static/stage1.js only to wire lifecycle/events, preserving existing modifications. Modify docs/USER_GUIDE.md; add tests/translated_sync_behavior_test.cjs (or existing browser test harness integration); integration fixtures/tests as needed.

**Interfaces:**
- Word upload on existing registered source card, Russian status/count and explicit Найти по Word action (calls Task 3 route, refreshes existing match UI).
- Per sync review card: opt-in translation controls, Generate TTS, Upload recording, selected source toggle, range fields for shared recording, delay/gain controls, audio preview. Default doesn't require a translation for ordinary Russian syncs; allow Original only (enabled:false) without deleting variants. Joint preview streams bounded guide/voice ranges, delays actual voice playback, and uses guide_offset_sec.
- Existing source/match review, confirm and build controls remain usable. Generating translation requires current source assignment; no forced ASR/LLM.

- [ ] Tests first for upload/match requests, selected TTS/recorded source, validation error visibility, preview and refresh; existing JS harness conventions.
- [ ] Implement separate ES module using existing core request/toast helpers and lifecycle; avoid growing monolithic inline HTML/handlers unnecessarily. Persist no frontend-only source of truth.
- [ ] Add end-to-end fixture test DOCX -> matches -> confirm -> generated fixture/recording selection -> timeline -> Stage3 XML; verify black gap and subsequent timing, old Russian path unaffected.
- [ ] Update guide with concrete workflow and difference of approximate document timing vs reviewed ranges, automatic black extension and TTS replacement.
- [ ] Run focused checks then parent full pytest and applicable node checks. Manual Premiere import/listening remains a named acceptance check if unavailable. No commit.
