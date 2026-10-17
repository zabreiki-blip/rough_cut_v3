# Dependency and quality policy

The project has four supported dependency surfaces with different platform
constraints:

- `requirements.txt` is the portable shared application runtime set;
- `requirements-whisper.txt` adds the Linux/Docker transcription backend;
- `requirements-macos-apple-silicon.txt` adds the native MLX backend;
- `requirements-dev.txt` adds the test, coverage, and lint tools used by CI and
  local development.

Direct application and development dependencies use either exact pins or
explicit compatible ranges. Platform-specific packages (MLX) use a separate
requirement surface.

## Locks and reproducibility

The `requirements*.txt` input files say what we want (exact pins for direct
runtime dependencies, compatible ranges such as `Pillow>=10` or
`faster-whisper>=1.1.0,<2`). Two generated, hashed lock files say what is
actually installed:

- `requirements-lock.txt` is the runtime lock: `requirements.txt` +
  `requirements-whisper.txt`, direct and transitive dependencies, no dev tools.
  The Dockerfile and the CI `runtime-smoke` job install it with
  `pip install --require-hashes -r requirements-lock.txt`.
- `requirements-dev-lock.txt` is the dev lock: `requirements-dev.txt`
  (runtime + pytest, coverage, ruff, mypy), resolved against the runtime lock
  so CI tests the same runtime versions that Docker ships. The CI `verify` job
  installs it with `--require-hashes`.

Both locks are produced by [uv](https://docs.astral.sh/uv/) with
`--python-version 3.11 --universal --generate-hashes`. A universal lock carries
environment markers and the hashes of every published wheel, so the same file
is valid on linux x86_64 and aarch64 (Docker Desktop on Apple Silicon), macOS
and Windows; a platform-specific lock would break the image build on the other
architecture. Python-version dependent pins (`av`, `numpy`) are selected by
markers; the image and CI target Python 3.11.

## Supported Python versions

- **3.11** is the reference version: the Docker image (`python:3.11-slim`),
  CI and both locks (`--python-version 3.11`) use it. The pinned wheels and
  hashes were resolved for 3.11.
- **3.12** is supported for the native macOS Apple Silicon path
  (`scripts/setup-macos-apple-silicon`, README), which installs from
  `requirements.txt` and `requirements-macos-apple-silicon.txt`, not from the
  locks. The universal lock also carries environment markers for Python >= 3.12
  (for example the `av` and `numpy` pins), so it stays installable there, but
  CI does not exercise 3.12.

Do not raise or lower either version without updating the Dockerfile, the CI
matrix, `scripts/lock-deps` and this section together.

## Local hygiene

`scripts/clean-pyc` removes `__pycache__` directories and `*.pyc` files under
`src/`, `tests/` and `scripts/` only (never `.venv`, `projects/` or `data/`).
Run it after switching Python versions to avoid stale bytecode.

Update procedure (never edit lock files by hand):

1. Change the version or range in the relevant `requirements*.txt` file.
2. Run `scripts/lock-deps` (keeps already locked versions) or
   `scripts/lock-deps --upgrade` (newest allowed versions). It needs `uv`.
3. Review the lock diff, run the full test suite, and rebuild the Docker image.
4. Commit the input file and both lock files together.

CI audits both locks with `pip-audit --require-hashes --disable-pip`, which
checks the exact locked versions without installing them. Dependabot's `pip`
ecosystem updates the `requirements*.txt` input files; its pull request does
not regenerate the locks, so run `scripts/lock-deps` on that branch before
merging (a consistency test fails if an input pin and the lock disagree).

`pre-commit` is an optional local tool (`pip install pre-commit`, see README)
and is deliberately not part of any requirements file or lock. Native Apple
Silicon (`requirements-macos-apple-silicon.txt`, MLX) has a separate
requirements surface and is not represented by the locks. A fully reproducible
release would also need a digest-pinned base image.

CI measures branch coverage for `src/` and enforces a 79% floor. The initial
measured baseline on 2026-08-04 was 77.5%. New work should raise the floor
gradually rather than introducing a large unreviewed test rewrite.

Ruff enforces correctness rules and the repository-wide formatting baseline.
Both pre-commit and CI run `ruff check src tests`; pre-commit formats the same
trees with `ruff format`, while CI verifies them with
`ruff format --check src tests`. Additional lint families remain separate
staged initiatives and should be adopted only with dedicated cleanup changes.
