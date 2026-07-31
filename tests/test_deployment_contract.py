from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_apple_silicon_profile_uses_native_mlx() -> None:
    requirements = (ROOT / "requirements-macos-apple-silicon.txt").read_text(
        encoding="utf-8"
    )
    setup = (ROOT / "scripts/setup-macos-apple-silicon").read_text(
        encoding="utf-8"
    )
    runner = (ROOT / "scripts/run-native-macos").read_text(encoding="utf-8")

    assert "mlx-whisper==0.4.3" in requirements
    assert '"$(uname -m)" != "arm64"' in setup
    assert 'PYTHON_ARCH' in setup
    assert 'resolve_backend("auto") == "mlx-whisper"' in setup
    assert ".venv-macos-arm64" in setup
    assert "gunicorn" in runner
    assert "source \"${ROOT_DIR}/.env\"" not in runner
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
