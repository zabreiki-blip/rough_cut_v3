# Dependency and quality policy

The project has four supported dependency surfaces with different platform
constraints:

- `requirements.txt` is the portable shared application runtime set;
- `requirements-whisper.txt` adds the Linux/Docker transcription backend;
- `requirements-macos-apple-silicon.txt` adds the native MLX backend;
- `requirements-dev.txt` adds the test, coverage, and lint tools used by CI and
  local development.

Direct application and development dependencies use either exact pins or
explicit compatible ranges. Binary and platform-specific packages use separate
requirement surfaces because a single macOS-generated lock would not be a valid
Linux/CPython 3.11 lock.

A single cross-platform lock file is therefore intentionally not generated.
If fully reproducible deployment beyond the current Docker build becomes a
release requirement, pin the base image by digest and generate and maintain
separate resolver outputs for Linux
CPython 3.11 and native Apple Silicon; do not reuse a lock produced by one
platform on the other.

CI measures branch coverage for `src/` and enforces a 79% floor. The initial
measured baseline on 2026-08-04 was 77.5%. New work should raise the floor
gradually rather than introducing a large unreviewed test rewrite.

Ruff currently enforces correctness rules only. Formatting and static typing
are separate staged initiatives: `ruff format`/additional lint families and a
scoped type-check baseline should be adopted only with dedicated cleanup
changes, not mixed into functional refactors.
