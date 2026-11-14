# Recorded voiceover bulk confirmation and type checks

**Goal:** Resolve all 23 mypy diagnostics and let a reviewer confirm all current matched narrator ranges in one click.

**Design:** Add a button beside the recorded-voiceover controls and an atomic bulk endpoint. The browser sends the pending matched ranges with any edited in/out values. The server checks revision, map generation, active jobs, current text hashes, recording bounds and final confirmed-range overlaps before one save. Missing/stale blocks are excluded; deliberate sound-source choices are preserved. Publication remains the existing separate action.

**Constraints:** Preserve runtime behavior during type cleanup. No ignores or casts that merely hide diagnostics. Preserve all existing worktree changes. No live-project confirmations, publications, paid calls or data edits during verification.

- [x] Fix the 16 narrowing/argument diagnostics in alignment, assembly, builds and Stage 3 routes; run existing behavior tests.
- [x] Fix the remaining seven diagnostics in recorded-voiceover routes while adding atomic bulk confirmation.
- [x] Add the button, state binding, eligibility/busy handling and success/error feedback; cover behavior and API atomicity with failing tests before implementation.
- [x] Verify stale revision/generation, missing/stale blocks, overlap and invalid bounds, preservation of source choices, and single-generation commit.
- [x] Independently review changes; run mypy, Ruff, UI tests and complete Python suite.
- [x] Reload verified code into the idle local server, check health/UI, and preserve active project data.

## Verification

- Mypy: success across all 90 source files; all 23 original diagnostics resolved through narrowing/explicit arguments, without adding suppression.
- Complete Python suite: 3270 passed, 35 subtests passed. UI behavior suite: 65 passed. Ruff, whitespace and generated API route checks passed.
- Independent read-only review found no actionable defects after the targeted fixes.
- Real Chrome with an isolated Flask server and synthetic recording confirmed two ranges with one generation increment, retained edited bounds and untouched full-precision bounds/selected take, skipped the absent range, enabled the separate Apply action and did not publish. Screenshot: `artifacts/reviews/recorded-confirm-all-browser.png`.
- Reloaded the idle local server; health HTTP 200. Normalization and recorded-voiceover maps in live projects were unchanged.
