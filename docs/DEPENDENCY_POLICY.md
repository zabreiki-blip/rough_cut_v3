# Dependency and quality policy

The project has three supported dependency surfaces with different platform
constraints:

- `requirements.txt` is the portable API/Stage 1 runtime set;
- `requirements-whisper.txt` adds the Linux/Docker transcription backend;
- `requirements-macos-apple-silicon.txt` adds the native MLX backend.

Pure-Python application dependencies are pinned exactly where the same release
is expected on every platform. Binary or platform-specific packages keep a
bounded compatible range when a single macOS-generated lock would not be a
valid Linux/CPython 3.11 lock.

A single cross-platform lock file is therefore intentionally not generated.
If reproducible deployment beyond the pinned Docker image becomes a release
requirement, generate and maintain separate resolver outputs for Linux
CPython 3.11 and native Apple Silicon; do not reuse a lock produced by one
platform on the other.

CI measures branch coverage for `src/` and enforces a 75% floor. The initial
measured baseline on 2026-08-04 was 77.5%. New work should raise the floor
gradually rather than introducing a large unreviewed test rewrite.

Ruff currently enforces correctness rules only. Formatting and static typing
are separate staged initiatives: `ruff format`/additional lint families and a
scoped type-check baseline should be adopted only with dedicated cleanup
changes, not mixed into functional refactors.
