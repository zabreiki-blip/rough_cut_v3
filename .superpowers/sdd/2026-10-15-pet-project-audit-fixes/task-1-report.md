# Task 1 report: patched dependency and validated settings

## What I implemented

- Updated both direct and lock requirements to pin `requests==2.32.4`.
- Added environment validation for `FINAL_REVIEW_TIMEOUT_SECONDS` as a positive integer, with the documented 7200-second default.
- Added non-negative decimal validation for `ROUGH_CUT_MAX_USD_PER_JOB` and `ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE`. A positive per-job limit now requires a positive estimate; zero disables the limit and represents an unknown estimate respectively.
- Kept new validation messages actionable and free of supplied environment values.
- Documented all three variables and their defaults in `.env.example`.
- Added configuration acceptance/rejection tests and a dependency pin consistency/version test.

## Tests and results

- Focused: `.venv/bin/pytest -q tests/test_config.py tests/test_security.py` — 27 passed.
- Full suite: `.venv/bin/pytest -q` — 858 passed.
- `git diff --check` — passed.

## TDD RED/GREEN evidence

- RED: with the new tests added before implementation, the focused suite had 3 failures: cost/deadline validation was absent, and Requests remained pinned to 2.32.3.
- GREEN: after implementation, the focused suite passed all 27 tests.

## Files changed

- `.env.example`
- `requirements.txt`
- `requirements-lock.txt`
- `src/stage1/config.py`
- `tests/test_config.py`
- `tests/test_security.py`
- `.superpowers/sdd/2026-10-15-pet-project-audit-fixes/task-1-report.md`

## Self-review findings

- Malformed values, including non-finite decimal values, are rejected without echoing their contents.
- The disabled cost guard accepts zero limit and zero estimate; enabled cost validation requires both values to be positive.
- Direct and lock requirements have exactly one matching Requests pin at or above the required version.
- No unrelated edits were present in the isolated worktree.

## Issues/concerns

- None identified. Dependencies were not installed or changed in the virtual environment.
