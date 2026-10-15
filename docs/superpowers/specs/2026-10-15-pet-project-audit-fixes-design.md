# Design: audit fixes for final review, cost control, and runtime verification

Date: 2026-10-15

## Intent and scope

Implement findings A1–A8 from
`docs/archive/audits/PET_PROJECT_INDEPENDENT_AUDIT_2026-10-15.md` without a
general rewrite of the application. The goal is to make the final-review flow
recoverable, cancellable, bounded, and safe under concurrent apply requests;
remove the vulnerable Requests pin; and verify the production ASR dependency
surface in CI.

Finding A9 is limited to extractions required by these changes. There is no
standalone rewrite of `run_sync_job` in this scope. Existing user changes in
the worktree remain untouched.

## User-visible behavior

### Recoverable final review

The backend adds:

- `GET /stage2/final-review/latest?project=<project>` to return the latest
  persisted job for the project;
- `POST /stage2/final-review/jobs/<job_id>/cancel` to request cancellation.

On panel startup and project change, CEP asks for the latest job. A queued or
running job resumes polling. A completed job restores its source-sequence
metadata, but Apply is enabled only after the host inspects the active sequence
and its `sequenceId` matches the sequence stored in the job. Interrupted,
failed, and cancelled jobs are displayed as terminal states and are not
applied.

Polling retries transient status failures with bounded exponential delays of
3, 6, 12, 24, and 30 seconds. The panel keeps the job ID, displays a reconnect
message, and exposes an immediate Retry action after the automatic retry budget
is exhausted. A polling error never starts another final-review job.

The final-review timer participates in the existing panel visibility lifecycle:
it is cleared when the panel is hidden and one immediate status refresh occurs
when the panel becomes visible.

### Cancellation and deadline

Final-review jobs use the same per-job `threading.Event` pattern as TTS and sync
jobs. The cancel endpoint changes an active job to `cancelling`, persists it,
and sets its event. The worker checks cancellation:

1. before transcription;
2. while the cloud request is pending, using the cloud adapter's existing
   cancellable await;
3. between cloud chunks;
4. before writing the word index and report.

Cancellation finishes as `cancelled`, records `finished_at`, and releases the
project reservation in `finally`. A request already accepted by a provider may
still be billable; the panel says so.

`FINAL_REVIEW_TIMEOUT_SECONDS` defines the whole-job deadline and defaults to
7200 seconds. The worker passes the remaining deadline into transcription and
treats expiry as `failed` with an explicit timeout error. Per-request cloud
timeouts remain capped by the smaller of the adapter timeout and remaining job
time.

After process restart, persisted `queued`, `running`, or `cancelling` jobs are
reported as `interrupted`, since their worker and cancellation event no longer
exist.

### Safe apply semantics

Iteration allocation and history writing execute under the existing project
transition lock. Each Apply request carries `request_id`:

- replaying the same `request_id` returns the already prepared iteration;
- a new `request_id` intentionally creates a new iteration, preserving E2E
  checklist F4;
- concurrent different request IDs are serialized and receive different
  iteration IDs.

The CEP panel creates one request ID for a single user action and reuses it for
network retries. It creates a new ID only for a new explicit Apply click.

## Cost guard

The usage ledger remains the source of actual and pending cost reporting. The
guard adds two optional settings:

- `ROUGH_CUT_MAX_USD_PER_JOB`, default `0` (disabled);
- `ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE`, default `0` (unknown).

When a positive job limit is configured for cloud ASR, a positive per-minute
estimate is required. Before start, the server calculates a conservative
estimate from planned chunk durations including overlap. It rejects the job
with HTTP 409 when the estimate exceeds the job limit and with HTTP 503 when a
limit is enabled but estimation is not configured.

During transcription, the cloud adapter receives the job limit and estimate.
Before each provider attempt, including retries, it reserves the estimated
cost of that chunk attempt against the job budget. After a response, it replaces
the reservation with the provider-reported actual cost when available. If the
next attempt would exceed the limit, processing stops before dispatch with a
budget-specific error. Unknown provider cost remains represented as the
reserved estimate, so the guard fails closed when enabled.

The job status exposes `estimated_cost_usd`, `cost_limit_usd`, and the actual or
reserved `cost_usd`. CEP shows the estimate before confirmation and the final
amount/status afterward. The limit is per job; it does not reinterpret the
project-wide ledger total as available budget.

The same reusable budget object is passed to cloud sync transcription. Semantic
LLM review keeps its existing request-count limit in this change; extending
monetary reservations to every LLM/TTS operation is a later, separately scoped
change.

## Dependency and CI changes

`requests` is pinned to a patched release at or above 2.32.4 in both direct and
lock requirement files. Existing Python and JS quality gates remain unchanged.

CI adds a separate Linux runtime job that:

1. resolves and installs `requirements.txt` plus `requirements-whisper.txt`;
2. imports the configured faster-whisper backend and the application ASR
   modules without downloading a model;
3. builds the Docker image.

The smoke job does not run inference or contact a paid API. Docker layer caching
may be added only if the uncached job is too slow; it is not required for the
first correct implementation.

## Internal boundaries

- `api_final_review_jobs.py` owns final-review state, cancellation events,
  deadline handling, and serialized iteration allocation.
- `api_final_review_routes.py` owns validation and the latest/cancel/apply HTTP
  contracts.
- `cloud_asr.py` owns request-level cancellation, deadline enforcement, and
  budget reservations.
- a small cost-budget module owns Decimal parsing, conservative reservation,
  settlement, and budget errors so final review and sync use identical rules.
- `cep/js/main.js` owns restored UI state, retry timers, visibility handling,
  confirmation, and Apply request IDs.

Existing service Protocols are extended only with dependencies actually needed
by these functions. No new global service locator or second job framework is
introduced.

## Error handling and compatibility

- New settings are optional; existing local-only and cloud workflows behave as
  before when the cost limit is `0`.
- Unknown jobs remain 404. Cancelling a terminal job returns its existing state
  without changing it, matching the current TTS/sync style.
- A restored completed job whose sequence is missing or different stays visible
  but Apply remains disabled with an actionable message.
- Budget rejection occurs before the first paid request whenever a limit is
  enabled. A provider request already sent before cancellation or timeout may
  still appear in usage accounting.
- Persistence writes continue to use atomic JSON helpers.

## Test strategy

All behavior changes follow red-green TDD.

Backend tests cover latest-job recovery, all cancellation state transitions,
deadline expiry, cancellation during cloud await, cancellation between chunks,
budget preflight and retry reservation, persisted terminal state, and project
release. Apply tests cover same-request replay and concurrent different request
IDs.

CEP tests cover automatic retry, retry exhaustion/manual retry, reload recovery,
sequence mismatch, cancel UI, Apply request-ID reuse, and hidden/visible timer
lifecycle. Contract tests verify the two new routes and controls.

CI/config tests verify the patched Requests version, new environment settings,
and runtime smoke job. Final verification runs the full Python suite with
coverage, Ruff check/format, mypy, ESLint, both JS behavior suites, Compose
config, and a Docker build if the environment permits it.

## Completion criteria

The change is complete when all automated tests pass, a completed final-review
job can be restored without retranscription, cancellation releases the project,
concurrent Apply calls cannot lose an iteration, the configured budget blocks a
provider dispatch that would exceed its reservation, the vulnerable Requests
pin is gone, and CI validates the production Whisper dependency surface.

Premiere behavior still requires the manual F1–F4 pilot. Automated completion
does not claim that pilot has run.
