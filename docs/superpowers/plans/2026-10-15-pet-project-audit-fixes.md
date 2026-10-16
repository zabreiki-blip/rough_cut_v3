# Pet project audit fixes implementation plan

> **For Codex:** execute with `superpowers:executing-plans`; keep the user's
> existing dirty files, use red-green TDD for every behavior change, and stage
> only files owned by the current task.

**Goal:** Make final review recoverable, cancellable, cost-bounded, safe under
concurrent Apply calls, and covered by production dependency checks.

**Architecture:** Reuse the existing `JobRegistry`, project transition locks,
atomic JSON persistence, and isolated transcription worker. Add a pure Decimal
budget component, pass serializable budget/deadline options through the Whisper
process boundary, and keep CEP recovery state tied to the persisted final-review
job and inspected Premiere sequence.

**Tech stack:** Python 3.11, Flask, pytest, asyncio/httpx, vanilla CEP JavaScript,
Node behavior tests, GitHub Actions, Docker.

**Spec:** `docs/superpowers/specs/2026-10-15-pet-project-audit-fixes-design.md`

---

## File map

- Create `src/cost_budget.py`: Decimal config parsing, estimate, reservation,
  settlement, and budget errors.
- Create `tests/test_cost_budget.py`: pure cost-budget tests.
- Modify `src/stage2/cloud_asr.py`: shared chunk planning and per-attempt budget.
- Modify `src/stage2/whisper_runner.py`: forward cloud cancel/budget options.
- Modify `src/stage2/whisper_process.py`: pass serializable options through IPC
  and retain hard cancel/deadline behavior.
- Modify `src/api_final_review_jobs.py`: cancel-event lifecycle, deadline,
  progress/cost persistence, and serialized iteration creation.
- Modify `src/api_final_review_routes.py`: preflight, latest, cancel, and
  idempotent Apply contracts.
- Modify `src/api_stage2_routes.py` and `src/api_sync_worker.py`: one budget
  shared across every cloud source in a sync job.
- Modify `src/api_service_contracts.py` and `src/api.py`: expose the new job
  dependencies through the compatibility facade.
- Modify `cep/index.html` and `cep/js/main.js`: recovery, retries, cancel,
  visibility lifecycle, cost confirmation, and Apply request IDs.
- Modify tests beside each component plus `.env.example`, `README.md`,
  `docs/STAGE1_GUIDE.md`, requirements, and `.github/workflows/ci.yml`.

## Task 1: Patched dependency and validated settings

**Files:**
- Modify: `requirements.txt`
- Modify: `requirements-lock.txt`
- Modify: `src/stage1/config.py`
- Modify: `.env.example`
- Test: `tests/test_config.py`
- Test: `tests/test_security.py`

**Interfaces:**
- Produces validated env names `FINAL_REVIEW_TIMEOUT_SECONDS`,
  `ROUGH_CUT_MAX_USD_PER_JOB`, and
  `ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE`.

- [ ] **Step 1: Write failing configuration and dependency tests**

Add tests named:

- `test_validate_env_rejects_invalid_final_review_cost_guard()` asserting that
  negative/non-numeric money values and a positive limit without a positive
  estimate produce actionable warnings without echoing secrets;
- `test_validate_env_accepts_disabled_or_complete_cost_guard()` asserting that
  limit `0` and a positive limit+estimate are accepted;
- `test_runtime_requests_pin_is_patched()` asserting both requirement files use
  the same Requests pin and its parsed version is at least `2.32.4`.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/pytest -q tests/test_config.py tests/test_security.py`

Expected: FAIL because the new env names are not validated and Requests is
still `2.32.3`.

- [ ] **Step 3: Implement the minimal settings/dependency change**

Pin `requests==2.32.4` in direct and lock requirements. Extend `validate_env()`
with positive decimal validation and integer validation for the final-review
deadline. Document defaults in `.env.example`; limit `0` disables enforcement,
deadline defaults to `7200`, and estimate `0` means unknown.

- [ ] **Step 4: Verify GREEN**

Run: `.venv/bin/pytest -q tests/test_config.py tests/test_security.py`

- [ ] **Step 5: Commit**

Commit message: `fix: patch requests and validate review limits`

## Task 2: Reusable cost budget and shared chunk estimate

**Files:**
- Create: `src/cost_budget.py`
- Create: `tests/test_cost_budget.py`
- Modify: `src/stage2/cloud_asr.py`
- Test: `tests/test_stage2_cloud_asr.py`

**Interfaces:**
- Produces `CostBudgetExceeded(RuntimeError)`.
- Produces `CostGuardConfig.from_environ(environ: Mapping[str, str])` with
  `max_usd_per_job: Decimal` and `cloud_asr_usd_per_minute: Decimal`.
- Produces `CostBudget(limit_usd: Decimal, usd_per_minute: Decimal,
  initial_spent_usd: Decimal = Decimal("0"))` with
  `estimate_seconds(seconds: float) -> Decimal`,
  `reserve_attempt(seconds: float) -> str`,
  `settle(reservation_id: str, actual_usd: object) -> None`, and
  `snapshot() -> dict[str, float]`.
- Produces `plan_cloud_chunks(duration_sec: float, chunk_minutes: float,
  overlap_sec: float) -> list[tuple[float, float]]`; the adapter delegates to it.

- [ ] **Step 1: Write failing pure tests**

Cover disabled budget, Decimal-safe estimates, rejection before a reservation
that crosses the limit, replacement of a reservation by actual provider cost,
retention of an estimate when provider cost is unknown, and inclusion of
overlap in the sum of planned chunk durations.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/pytest -q tests/test_cost_budget.py tests/test_stage2_cloud_asr.py -k "budget or plan_cloud_chunks"`

Expected: collection/import FAIL because `cost_budget` and the public planner
do not exist.

- [ ] **Step 3: Implement pure budget and planner**

Use `Decimal(str(value))`, reject non-finite/negative values, and generate an
opaque reservation ID per attempt. Keep reservations sequential and local to
one transcription job; no global ledger state belongs in this class.

- [ ] **Step 4: Verify GREEN**

Run: `.venv/bin/pytest -q tests/test_cost_budget.py tests/test_stage2_cloud_asr.py`

- [ ] **Step 5: Commit**

Commit message: `feat: add cloud ASR cost budget`

## Task 3: Carry cancellation, deadline, and budget through ASR

**Files:**
- Modify: `src/stage2/cloud_asr.py`
- Modify: `src/stage2/whisper_runner.py`
- Modify: `src/stage2/whisper_process.py`
- Test: `tests/test_stage2_cloud_asr.py`
- Test: `tests/test_stage2_whisper_process.py`
- Test: `tests/test_stage2_whisper_runner.py`

**Interfaces:**
- Extend `transcribe_cloud(..., cancel=None, max_cost_usd=None,
  usd_per_minute_estimate=None, initial_cost_usd=None, timeout_sec=None,
  budget_callback=None)`.
- Extend `whisper_runner.transcribe_audio(..., cloud_options=None,
  budget_callback=None)`.
- Extend `transcribe_audio_in_process(..., cloud_options=None,
  deadline_monotonic=None)` and send Decimal values as strings in a
  JSON/pickle-safe mapping through IPC.

- [ ] **Step 1: Write failing adapter tests**

Add tests proving: reservation happens before each HTTP attempt; a retry is not
sent when its reservation would cross the limit; unknown cost retains its
reservation; actual cost settles it; and timeout/cancel prevents further chunk
dispatch. Assert the mocked HTTP call count, not private implementation calls.

- [ ] **Step 2: Verify adapter RED**

Run: `.venv/bin/pytest -q tests/test_stage2_cloud_asr.py -k "budget or deadline or cancellation"`

Expected: FAIL because cloud options are unsupported.

- [ ] **Step 3: Implement adapter option flow**

Create one `CostBudget` per `transcribe_cloud()` call, seeded by prior-source
cost for sync. Reserve immediately before `_call_model`; publish its snapshot
through `budget_callback`; settle after a successful response; leave the
reservation in place if cost is absent. Attach `cost_usd`,
`reserved_cost_usd`, and `cost_limit_usd` to results and budget/ASR errors.

- [ ] **Step 4: Write and verify failing process-boundary tests**

Add tests proving `cloud_options` reach the child runner, budget progress
messages return the last known reserved amount, queue time counts against the
supplied deadline, cancellation while waiting for `_worker_lock` is prompt, and
cancel/timeout during inference discards the worker promptly.

Run: `.venv/bin/pytest -q tests/test_stage2_whisper_process.py tests/test_stage2_whisper_runner.py -k "cloud_options or cancel or timeout"`

Expected: FAIL because IPC currently sends only backend/model/language.

- [ ] **Step 5: Implement process-boundary option forwarding**

Add the mapping to the IPC request tuple and forward it only to the cloud
backend. Send a distinct `budget` IPC message from the child callback; the
parent stores the latest snapshot without changing the existing three-argument
progress callback. Attach that snapshot to cancellation/timeout exceptions.
Replace the blocking worker-lock context manager
with bounded acquire polling that checks cancel and the absolute whole-job
deadline; pass `min(remaining, configured whisper timeout)` into inference.
Preserve existing defaults for local backends. Parent cancellation/deadline
remain the hard stop and terminate the child, interrupting an in-flight request.

- [ ] **Step 6: Verify GREEN**

Run: `.venv/bin/pytest -q tests/test_stage2_cloud_asr.py tests/test_stage2_whisper_process.py tests/test_stage2_whisper_runner.py`

- [ ] **Step 7: Commit**

Commit message: `feat: enforce ASR budget and deadline`

## Task 4: Final-review latest, cancellation, progress, and preflight

**Files:**
- Modify: `src/api.py`
- Modify: `src/api_service_contracts.py`
- Modify: `src/api_final_review_jobs.py`
- Modify: `src/api_final_review_routes.py`
- Test: `tests/test_api_final_review_routes.py`
- Test: `tests/test_api_route_contract.py`

**Interfaces:**
- Add `_FINAL_REVIEW_CANCEL_EVENTS = _FINAL_REVIEW_RUNTIME.cancel_events`.
- Add `get_latest_final_review_job(services, project) -> dict | None` and
  `cancel_final_review_job(services, job_id) -> tuple[dict | None, str | None]`.
- Register `GET /stage2/final-review/latest`,
  `POST /stage2/final-review/estimate`, and
  `POST /stage2/final-review/jobs/<job_id>/cancel`.

- [ ] **Step 1: Write failing route/lifecycle tests**

Test in-memory latest selection and persisted fallback; missing/bad project;
active cancellation to `cancelling`; terminal cancellation returns the
existing state without mutation; persisted
`cancelling` restoration as `interrupted`; cancel before transcription;
cancel after transcription before artifact writes; queue time included in the
whole-job deadline; deadline failure; cost fields on queued/running/failed/
completed jobs; reserved cost retained when the child is terminated; and
project release in every terminal path. Extend the route-contract expected map.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/pytest -q tests/test_api_final_review_routes.py tests/test_api_route_contract.py`

Expected: FAIL for missing routes/events and unsupported worker options.

- [ ] **Step 3: Implement job lifecycle**

Create the cancellation event with the job under the registry lock. Pass it and
an absolute deadline derived from `FINAL_REVIEW_TIMEOUT_SECONDS` into
transcription. Add a progress callback that persists processed/duration and
last-known budget fields at a bounded cadence. Catch
`WhisperCancelledError` separately as `cancelled`; preserve provider cost on
failure; remove the event and release the project in `finally`.

- [ ] **Step 4: Implement route preflight and contracts**

Factor one pure estimate helper used by both Estimate and Start. Use
`_wav_duration`, `plan_cloud_chunks`, and `CostGuardConfig`; make no provider
call. Return 400 for an enabled limit without an estimate, 409 when estimated
cost exceeds the limit, and 409 when recomputation differs from
`confirmed_estimate_usd`. Put estimate/limit into the job payload. Implement
latest/cancel responses using the TTS/sync conventions.

- [ ] **Step 5: Verify GREEN**

Run: `.venv/bin/pytest -q tests/test_api_final_review_routes.py tests/test_api_route_contract.py`

- [ ] **Step 6: Commit**

Commit message: `feat: make final review recoverable and cancellable`

## Task 5: Serialize and deduplicate Apply preparation

**Files:**
- Modify: `src/api_final_review_jobs.py`
- Modify: `src/api_final_review_routes.py`
- Modify: `src/api_service_contracts.py`
- Test: `tests/test_api_final_review_routes.py`

**Interfaces:**
- Change `record_final_review_iteration(services, project, job, corrections,
  request_id) -> dict`.
- Accept an optional bounded `request_id` in Apply JSON. Identical IDs for the
  same job return the stored iteration; the same ID for another job is 409;
  missing IDs always create a new iteration for old-client compatibility.

- [ ] **Step 1: Write failing Apply tests**

Add tests for an oversized request ID, a missing ID creating a new iteration,
same-job replay returning the same iteration, cross-job replay returning 409,
sequential different IDs preserving F4, and two synchronized threads with
different IDs producing `iteration_0001` and `iteration_0002` with neither
entry lost.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/pytest -q tests/test_api_final_review_routes.py -k "apply or iteration"`

Expected: FAIL because request IDs and serialized history writes do not exist.

- [ ] **Step 3: Implement serialized idempotency**

Validate optional `request_id` at the route. Hold
`_project_transition_lock(project)` around history read/lookup/append/write.
Store non-empty IDs in iterations; return a same-job match, reject a different-
job match, or allocate the next ID when absent/new.

- [ ] **Step 4: Verify GREEN**

Run: `.venv/bin/pytest -q tests/test_api_final_review_routes.py`

- [ ] **Step 5: Commit**

Commit message: `fix: serialize final review apply`

## Task 6: CEP recovery, retry, cancel, cost confirmation, and visibility

**Files:**
- Modify: `cep/index.html`
- Modify: `cep/js/main.js`
- Modify: `tests/cep_behavior_test.cjs`
- Modify: `tests/test_cep_contract.py`

**Interfaces:**
- Add controls `retryFinalReview` and `cancelFinalReview`.
- Add test hooks for `restoreFinalReview`, `pollFinalReviewJob`, retry state,
  visibility state, and current job/sequence state.

- [ ] **Step 1: Write failing behavior tests**

Extend the VM harness with tests that assert:

- transient failures schedule delays 3/6/12/24/30 seconds and preserve job ID;
- exhaustion exposes manual Retry and Retry sends one status request;
- latest-job recovery restores polling or completed state without calling start;
- Apply stays disabled until host inspection returns the stored `sequenceId`;
- sequence mismatch is visible and blocks Apply;
- Cancel calls the new route and renders `cancelling/cancelled`;
- hide clears final-review timer and show issues one immediate refresh;
- project change/start/cancel increments a generation and stale responses from
  an older generation cannot re-arm timers or enable Apply;
- Estimate runs before confirmation; Start carries `confirmed_estimate_usd`;
  a 409 stale estimate returns the user to confirmation without provider work;
- one Apply click creates a request ID and a network retry reuses it.

Add contract assertions for controls, latest/cancel URLs, and status labels.

- [ ] **Step 2: Verify RED**

Run: `node tests/cep_behavior_test.cjs && .venv/bin/pytest -q tests/test_cep_contract.py`

Expected: FAIL because the controls/state machine do not exist.

- [ ] **Step 3: Implement the CEP state machine**

Track timer, retry index, generation, visibility resume flag, job, stored source
sequence, and Apply request ID explicitly. Restore after project load/change.
Discard callbacks from stale generations. Use the existing host inspection path
to verify the active sequence before enabling Apply. Call Estimate and confirm
its value before Start; display that unknown accounting is not a cap. Never call
Start from status recovery/retry.

- [ ] **Step 4: Verify GREEN**

Run: `node tests/cep_behavior_test.cjs && .venv/bin/pytest -q tests/test_cep_contract.py`

- [ ] **Step 5: Commit**

Commit message: `feat: recover final review in CEP`

## Task 7: One budget across all cloud sources in a sync job

**Files:**
- Modify: `src/api_stage2_routes.py`
- Modify: `src/api_sync_worker.py`
- Modify: `src/api_service_contracts.py`
- Test: `tests/test_stage2_sync_api.py`

**Interfaces:**
- Sync job fields match final review: `estimated_cost_usd`, `cost_limit_usd`,
  and `cost_usd`.
- Each cloud transcription receives serialised `max_cost_usd`,
  `usd_per_minute_estimate`, and the cost already consumed by prior sources.

- [ ] **Step 1: Write failing sync budget tests**

Test preflight sum across multiple source durations including overlap; reject an
over-limit start before worker creation; pass the remaining budget to each
source; stop before a later source would exceed the shared job limit; and retain
actual/reserved cost fields on failure/cancellation.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/pytest -q tests/test_stage2_sync_api.py -k "budget or cost_limit"`

Expected: FAIL because sync only limits LLM block count and has no shared job
budget.

- [ ] **Step 3: Implement shared sync preflight and accounting**

Use the same planner/config as final review. Compute one estimate across selected
cloud sources before reserving the project, store it on the job, and reduce the
remaining serialized limit by actual or reserved cost after each source. Leave
local ASR behavior unchanged.

- [ ] **Step 4: Verify GREEN**

Run: `.venv/bin/pytest -q tests/test_stage2_sync_api.py`

- [ ] **Step 5: Commit**

Commit message: `feat: bound cloud sync job cost`

## Task 8: Production dependency CI and operator documentation

**Files:**
- Modify: `.github/workflows/ci.yml`
- Create: `tests/runtime_import_smoke.py`
- Modify: `README.md`
- Modify: `docs/STAGE1_GUIDE.md`
- Modify: `docs/E2E_PILOT_CHECKLIST.md`
- Test: `tests/test_security.py`

**Interfaces:**
- Runtime smoke imports `faster_whisper`, `stage2.whisper_runner`, and
  `stage2.cloud_asr` and exits without model creation/download.

- [ ] **Step 1: Write failing static CI/documentation tests**

Assert CI has a separate `runtime-smoke` job, installs runtime+Whisper
requirements, runs the smoke script, and builds the Docker image. Assert README
documents the three new routes/settings and the E2E checklist expects estimate
reconfirmation, recovery, cancellation, retry, and intentional repeated Apply.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/pytest -q tests/test_security.py -k "runtime or final_review"`

Expected: FAIL because the runtime job and documentation are absent.

- [ ] **Step 3: Implement CI and docs**

Add a Linux job with Python 3.11 and pip cache for both runtime requirement
files, then run the import smoke and `docker build .`. Do not download a model
or use API credentials. Update the active docs and remove the old E2E
expectation of a quiet reload failure.

- [ ] **Step 4: Verify GREEN and workflow syntax**

Run:

- `.venv/bin/pytest -q tests/test_security.py`
- `cp .env.example /private/tmp/rough-cut-audit.env`
- `docker compose --env-file /private/tmp/rough-cut-audit.env config --quiet`

- [ ] **Step 5: Commit**

Commit message: `ci: verify production ASR runtime`

## Task 9: Full verification and audit closure

**Files:**
- Modify: `docs/archive/audits/PET_PROJECT_INDEPENDENT_AUDIT_2026-10-15.md`
- Modify: `docs/archive/audits/README.md`

- [ ] **Step 1: Run the full local quality gates**

Run:

- `.venv/bin/python -m pytest -q --cov=src --cov-report=term --timeout=120`
- `.venv/bin/ruff check src tests`
- `.venv/bin/ruff format --check src tests`
- `.venv/bin/mypy src`
- `npm run lint:cep`
- `node tests/cep_behavior_test.cjs`
- `node tests/stage1_behavior_test.cjs`
- `docker compose --env-file /private/tmp/rough-cut-audit.env config --quiet`
- `docker build -t rough-cut-v3:audit-fixes .`

Expected: every command exits 0. If Docker/network is unavailable, report that
specific unverified gate rather than treating it as passed.

- [ ] **Step 2: Re-read the spec and inspect the complete diff**

Run: `git diff --check` and inspect `git diff --stat` plus every changed hunk.
Confirm A1–A8 each maps to code/tests, sync uses one per-job budget across
sources, and no existing user change was reverted.

- [ ] **Step 3: Update the archived audit status**

Mark implemented findings with the verifying test/commit and leave the Premiere
F1–F4 pilot explicitly pending. Do not call the archive a live backlog.

- [ ] **Step 4: Commit**

Commit message: `docs: record audit fix verification`

## Review focus

Before completion, specifically review these failure modes and their owning
tests:

1. A cancellation arriving after provider dispatch but before result
   persistence must preserve the last reserved cost and release the project
   (Tasks 3–4 lifecycle tests).
2. A hidden-panel in-flight response must not re-arm a stale timer or enable
   Apply for another project (Task 6 visibility/race test).
3. Unknown provider cost with an enabled limit must consume the conservative
   reservation and block the next over-budget attempt (Task 3 adapter test).
4. Concurrent different Apply request IDs must retain both iterations, while a
   replayed ID returns one (Task 5 threaded test).
5. Restart recovery must never label an orphaned `cancelling` job as live or
   automatically retranscribe it (Task 4 persistence and Task 6 recovery tests).
6. A sync job with several sources must not reset the per-job limit for each
   source (Task 7 multi-source budget test).
