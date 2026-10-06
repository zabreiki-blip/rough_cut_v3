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

`requirements-lock.txt` is a pinned Python 3.11 snapshot generated from the
development and Whisper requirements. CI and Docker install the direct
requirements files, not this snapshot. Native Apple Silicon has a separate
requirements surface and is not represented by that lock file. Do not reuse a
lock produced for one platform on another; a fully reproducible release would
also need a digest-pinned base image and platform-specific resolver outputs.

CI measures branch coverage for `src/` and enforces a 79% floor. The initial
measured baseline on 2026-08-04 was 77.5%. New work should raise the floor
gradually rather than introducing a large unreviewed test rewrite.

Ruff enforces correctness rules and the repository-wide formatting baseline.
Both pre-commit and CI run `ruff check src tests`; pre-commit formats the same
trees with `ruff format`, while CI verifies them with
`ruff format --check src tests`. Additional lint families remain separate
staged initiatives and should be adopted only with dedicated cleanup changes.
