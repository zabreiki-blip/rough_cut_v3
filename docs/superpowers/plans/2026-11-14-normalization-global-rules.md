# Global normalization rules implementation plan

> **For agentic workers:** Use bounded parallel implementation with primary-agent integration and verification. No project-data repair scripts are part of the solution.

**Goal:** Correct the reported parsing defects for new scripts and repeat runs without project-specific repairs or copying classifications from history.

**Architecture:** Identify production metadata before speaker inference. Parse file-credit headers by their syntax and validate the person/role separately. Keep author-number suffixes in metadata, and carry source information through sync continuations. Same-source review persistence and explicit reset retain their documented distinction.

**Tech stack:** Python normalizer and pytest; JavaScript interface and its behavior tests.

**Spec:** User request in this session: “надо глобально править — а не костыль для этого прогона”.

## Constraints

- No project names, block positions, speaker names, absolute user paths, or historical payloads in production rules.
- No paid LLM calls or modifications of active project data for verification.
- Metadata recognition must not eat marked narration, real speaker credits, or pre-timecode speech.
- An unknown media filename stays unknown unless the source or an explicit reviewer edit supplies it.
- Existing user changes and explicit reset semantics are preserved.

## Review focus

- Title prefixes resemble uppercase speaker names.
- All-capitals narration resembles an outline caption.
- File headers vary in name casing, role presence, repeated shorthand, spacing and filenames.
- Number suffixes resemble single-letter words after a dash.
- Regeneration must preserve explicit metadata corrections without reintroducing resolved warnings; reset must discard them.

## Tasks

- [x] Add failing tests for generic production metadata and implement conservative metadata detection in `src/stage1/scenario_metadata.py`.
- [x] Add clean-generation integration tests covering metadata, file credits, suffixes, and speech preservation. Run them before wiring the new rules.
- [x] Integrate metadata detection and generalize unmarked file-credit parsing in `src/stage1/normalizer.py`.
- [x] Verify same-source metadata/warning persistence and explicit reset using existing behavior; fix only evidenced defects.
- [x] Run a fresh parse of the actual DOCX in a temporary directory, with fresh memory and no LLM/history, inspecting the reported defect classes.
- [x] Review diffs and run relevant Python, UI, static and repository checks; report any unrelated baseline failures explicitly.

## Verification results

- New clean-source tests first reproduced the missing metadata/file-header handling and stale source warning, then passed after implementation. They include explicit reset/reparse and repeated regeneration with edited source metadata.
- Full repository run: 3226 passed, 35 subtests passed. Final expanded date-span checks were subsequently verified in 297 related passing tests (6 subtests).
- UI behavior: all 62 tests passed. Ruff and diff whitespace checks passed.
- Independent code review found no actionable defects.
- Fresh actual-DOCX parse used a temporary output directory and an empty memory database, with no LLM/history: opening metadata is technical, all seven formerly unnamed quotes inherit the credited speaker, and all 16 suffixed headers stay out of TTS without false duplicate numbers.
- The rules-only parse leaves 52 unrelated paragraphs unknown for normal classification; it does not copy old decisions. The missing media filename remains a genuine warning until a source is explicitly supplied.
- Reloaded the idle local Gunicorn server; new worker started and health returned HTTP 200. Active project draft bytes remained unchanged during verification and reload.

## Continuation: grammar and reviewed sync lifecycle

- [x] Reproduce narrative colon phrases incorrectly interpreted as speaker labels, including classifier-induced loss of sentence prefixes; preserve full narration with grammatical phrase checks.
- [x] Canonicalize lowercase/glued letter suffixes and typographic metadata hyphens without rewriting original text or consuming sentence-leading words.
- [x] Reconcile context-only sync headers after a reviewer supplies actual speech; preserve this decision on regeneration and repair pre-fix saved metadata.
- [x] Run integration checks, independent review, fresh source parsing and final repository checks; load the verified code into the idle local server without rewriting projects.

### Continuation verification notes

- Narrative phrase regressions reproduce both loss of voiceover inheritance and sentence-prefix truncation when a classifier incorrectly marks a prose prefix as a label. All 12 cases now preserve full text.
- Suffix checks include lowercase/glued forms, Unicode hyphens in marker/number/suffix positions, uppercase canonical IDs, and distinct Е/Ё; matching views do not rewrite source text.
- Sync-header review checks cover timed and untimed quotes, metadata-only edits, legacy pre-fix saved flags, confirmation validation, alignment eligibility and model-canonicalized legacy headers.
- Fresh DOCX parsing now preserves and recognizes all three previously misread prose-colon phrases as voiceover without history or LLM calls.
- Five changed production modules pass mypy. Full mypy retains 23 pre-existing errors in five untouched files (`voiceover/alignment.py`, `stage2/assembly_plan.py`, `stage3/builds.py`, `api_stage3_routes.py`, `api_voiceover_routes.py`); the same errors were reproduced on an isolated `git archive HEAD` baseline.

- Final continuation full run: 3262 passed, 35 subtests passed; UI behavior 62 passed; Ruff and diff checks passed. Idle local server reloaded, health HTTP 200, current draft unchanged.
