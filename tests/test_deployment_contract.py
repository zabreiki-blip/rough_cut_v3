import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_gunicorn_disables_control_socket_in_docker_and_compose() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    cmd_match = re.search(r"^CMD\s+(\[.*\])\s*$", dockerfile, flags=re.MULTILINE)
    assert cmd_match is not None, "Dockerfile CMD must be a JSON array"
    argv = json.loads(cmd_match.group(1))
    assert "--no-control-socket" in argv
    assert argv.index("--no-control-socket") < argv.index("api:app")

    command_match = re.search(r"^\s+command:\s*(.*)$", compose, flags=re.MULTILINE)
    assert command_match is not None, "compose service must define command"
    command = command_match.group(1)
    assert "--no-control-socket" in command.split()
    assert command.index("--no-control-socket") < command.index("api:app")


def test_apple_silicon_profile_uses_native_mlx() -> None:
    requirements = (ROOT / "requirements-macos-apple-silicon.txt").read_text(encoding="utf-8")
    setup = (ROOT / "scripts/setup-macos-apple-silicon").read_text(encoding="utf-8")
    runner = (ROOT / "scripts/run-native-macos").read_text(encoding="utf-8")

    assert "mlx-whisper==0.4.3" in requirements
    assert '"$(uname -m)" != "arm64"' in setup
    assert "PYTHON_ARCH" in setup
    assert 'resolve_backend("auto") == "mlx-whisper"' in setup
    assert ".venv-macos-arm64" in setup
    assert "gunicorn" in runner
    assert 'source "${ROOT_DIR}/.env"' not in runner
    assert "api:app" in runner


def test_portable_docker_bootstrap_preserves_host_data() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    bootstrap = (ROOT / "scripts/bootstrap-docker").read_text(encoding="utf-8")

    assert "./projects:/app/projects" in compose
    assert "./data:/app/data" in compose
    assert "whisper_cache:/app/.cache/huggingface" in compose
    assert "platform:" not in compose
    assert "docker compose config --quiet" in bootstrap
    assert "docker compose build stage1-api" in bootstrap
    assert "docker compose up -d stage1-api" in bootstrap
    assert "command -v openssl" in bootstrap
    assert "openssl rand -hex 32" in bootstrap


def test_macos_cep_installer_never_overwrites_existing_target() -> None:
    installer = (ROOT / "scripts/install-cep-macos").read_text(encoding="utf-8")
    assert '[[ -e "${TARGET}" || -L "${TARGET}" ]]' in installer
    assert "Move it aside manually" in installer
    assert 'ln -s "${ROOT_DIR}/cep" "${TARGET}"' in installer


def test_compose_has_parameterized_resource_limits() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert re.search(r"^\s+mem_limit:\s*\$\{ROUGH_CUT_MEM_LIMIT:-[^}]+\}\s*$", compose, re.M)
    # Default 0 = no CPU limit: a fixed default above the Docker CPU count
    # makes `docker compose up` fail with "range of CPUs is from 0.01 to N".
    assert re.search(r"^\s+cpus:\s*\$\{ROUGH_CUT_CPUS:-0\}\s*$", compose, re.M)
    assert "SYNC_WHISPER_CPU_THREADS=${SYNC_WHISPER_CPU_THREADS:-8}" in compose
    assert "ROUGH_CUT_MEM_LIMIT" in env_example
    assert "ROUGH_CUT_CPUS" in env_example


def test_compose_keeps_image_uid_for_named_cache_volume() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    # Overriding `user:` with the host uid would break writes to /app and the
    # whisper_cache volume, both owned by the image user.
    assert not re.search(r"^\s+user:", compose, re.M)
    assert "--uid 10001" in dockerfile


def test_bootstrap_warns_about_linux_bind_mount_ownership_without_sudo() -> None:
    bootstrap = (ROOT / "scripts/bootstrap-docker").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert '"$(uname -s)" == "Linux"' in bootstrap
    assert "chown -R 10001:10001 projects data" in bootstrap
    assert "setfacl" in bootstrap
    assert not re.search(r"^\s*sudo\s", bootstrap, re.M), "bootstrap must never run sudo"
    assert "10001" in readme
    assert "ROUGH_CUT_MEM_LIMIT" in readme


def test_asr_concurrency_limit_documented() -> None:
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "ROUGH_CUT_MAX_ASR_JOBS" in env_example
    assert "ROUGH_CUT_MAX_ASR_JOBS" in readme
