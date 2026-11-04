"""Exercise public installers in an isolated root; never install real packages."""

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture
def sandbox(tmp_path):
    shutil.copytree(ROOT / "scripts", tmp_path / "scripts")
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    (tmp_path / "src/stage1").mkdir(parents=True)
    shutil.copy(ROOT / "src/stage1/config.py", tmp_path / "src/stage1/config.py")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "PYTHON_BIN": str(bin_dir / "python3"),
        "TEST_LOG": str(tmp_path / "commands"),
        "TEST_OS": "Darwin",
        "TEST_ARCH": "arm64",
        "TEST_PY_ARCH": "arm64",
        "TEST_PY_VERSION": "3.11",
        "TEST_TRANSLATED": "0",
    }
    for key in ("ROUGH_CUT_PROFILE", "ROUGH_CUT_VENV"):
        env.pop(key, None)
    executable(
        bin_dir / "uname", '#!/bin/bash\n[[ "$1" == -s ]] && echo "$TEST_OS" || echo "$TEST_ARCH"\n'
    )
    executable(bin_dir / "sysctl", '#!/bin/bash\necho "$TEST_TRANSLATED"\n')
    python_stub = f"""#!{sys.executable}
import os, pathlib, subprocess, sys
args = sys.argv[1:]
if args[:2] == ['-m', 'venv']:
    target = pathlib.Path(args[2]) / 'bin'
    target.mkdir(parents=True, exist_ok=True)
    for name in ['python', 'python3', 'pip', 'gunicorn']:
        destination = target / name
        destination.write_text(pathlib.Path(__file__).read_text())
        destination.chmod(0o755)
    with open(os.environ['TEST_LOG'], 'a') as f: f.write('venv ' + args[2] + '\\n')
elif pathlib.Path(sys.argv[0]).name in ['pip', 'gunicorn'] or args[:2] == ['-m', 'pip']:
    with open(os.environ['TEST_LOG'], 'a') as f: f.write(sys.argv[0] + ' ' + ' '.join(args) + '\\n')
elif args == ['--version']:
    print('Python ' + os.environ['TEST_PY_VERSION'])
elif args[:1] == ['-c']:
    code = args[1]
    if 'platform.machine' in code: print(os.environ['TEST_PY_ARCH'])
    elif 'print' in code and 'version_info' in code: print(os.environ['TEST_PY_VERSION'])
    elif 'version_info' in code:
        version = os.environ['TEST_PY_VERSION']
        sys.exit(0 if version in (['3.11', '3.12'] if os.environ.get('ROUGH_CUT_PROFILE') == 'mlx' else ['3.11']) else 1)
    else: sys.exit(0)
else:
    sys.exit(subprocess.call([{sys.executable!r}] + args))
"""
    executable(bin_dir / "python3", python_stub)
    for name in ("ffmpeg", "ffprobe", "say", "afconvert", "node", "curl"):
        executable(bin_dir / name, "#!/bin/bash\nexit 0\n")
    executable(bin_dir / "openssl", "#!/bin/bash\nexit 99\n")
    executable(bin_dir / "docker", '#!/bin/bash\necho "docker $*" >> "$TEST_LOG"\nexit 0\n')
    shutil.copy(tmp_path / "scripts/doctor", tmp_path / "scripts/doctor-real")
    executable(
        tmp_path / "scripts/doctor",
        '#!/bin/bash\necho "doctor $ROUGH_CUT_PROFILE $ROUGH_CUT_VENV" >> "$TEST_LOG"\n',
    )
    return tmp_path, env


def run(sandbox, script="setup-local", **updates):
    root, env = sandbox
    return subprocess.run(
        ["/bin/bash", str(root / "scripts" / script), "--quick"],
        env={**env, **updates},
        text=True,
        capture_output=True,
        timeout=15,
    )


def log(sandbox):
    path = sandbox[0] / "commands"
    return path.read_text() if path.exists() else ""


@pytest.mark.parametrize(
    "system,arch,translated,profile,venv,requirements",
    [
        (
            "Darwin",
            "arm64",
            "0",
            "mlx",
            ".venv-macos-arm64",
            "requirements-macos-apple-silicon.txt",
        ),
        ("Darwin", "x86_64", "0", "cpu", ".venv", "requirements-whisper.txt"),
        ("Darwin", "arm64", "1", "cpu", ".venv", "requirements-whisper.txt"),
        ("Linux", "aarch64", "0", "cpu", ".venv", "requirements-whisper.txt"),
    ],
)
def test_auto_profile_installs_asr_and_dev(
    sandbox, system, arch, translated, profile, venv, requirements
):
    result = run(sandbox, TEST_OS=system, TEST_ARCH=arch, TEST_TRANSLATED=translated)
    assert result.returncode == 0, result.stderr
    commands = log(sandbox)
    assert f"venv {sandbox[0] / venv}\n" in commands
    assert requirements in commands
    assert "requirements-dev.txt" in commands
    assert f"doctor {profile} {sandbox[0] / venv}" in commands
    assert "install-cep" not in commands and "launchagent" not in commands
    assert "~1.5" in result.stdout and "http://127.0.0.1:8000" in result.stdout


@pytest.mark.parametrize(
    "initial",
    [
        None,
        "TTS_PROVIDER=custom\nCUSTOM=keep\n",
        "ROUGH_CUT_API_TOKEN=\nTTS_PROVIDER=custom\nCUSTOM=keep\n",
        "# preserved\nROUGH_CUT_API_TOKEN='canary-secret'\nTTS_PROVIDER=custom\nMACOS_CONNECTOR_URL=http://custom\n",
    ],
)
def test_env_initialization_is_secure_and_preserves_settings(sandbox, initial):
    path = sandbox[0] / ".env"
    if initial is not None:
        path.write_text(initial)
    result = run(sandbox)
    assert result.returncode == 0, result.stderr
    content = path.read_text()
    if initial and "canary-secret" in initial:
        assert content == initial
    else:
        assert re.search(r"^ROUGH_CUT_API_TOKEN=[0-9a-f]{64}$", content, re.M)
    if initial is None:
        assert "TTS_PROVIDER=macos\n" in content
    else:
        assert "TTS_PROVIDER=custom\n" in content
    again = run(sandbox)
    assert again.returncode == 0
    assert path.read_text() == content
    assert "canary-secret" not in result.stdout + result.stderr + again.stdout + again.stderr


@pytest.mark.parametrize(
    "version,profile,ok",
    [
        ("3.12", "mlx", True),
        ("3.10", "mlx", False),
        ("3.13", "mlx", False),
        ("3.12", "cpu", False),
        ("3.11", "cpu", True),
    ],
)
def test_python_policy(sandbox, version, profile, ok):
    result = run(sandbox, TEST_PY_VERSION=version, ROUGH_CUT_PROFILE=profile)
    assert (result.returncode == 0) is ok
    if not ok:
        assert "venv " not in log(sandbox)
        assert "3.11" in result.stderr


@pytest.mark.parametrize(
    "updates", [{"TEST_ARCH": "x86_64"}, {"TEST_TRANSLATED": "1"}, {"TEST_PY_ARCH": "x86_64"}]
)
def test_explicit_mlx_rejects_non_native_execution(sandbox, updates):
    result = run(sandbox, ROUGH_CUT_PROFILE="mlx", **updates)
    assert result.returncode == 2
    assert "venv " not in log(sandbox)
    assert "arm64" in result.stderr or "Rosetta" in result.stderr


def test_override_shared_by_setup_runner_and_doctor(sandbox):
    chosen = str(sandbox[0] / "custom environment")
    settings = {"ROUGH_CUT_PROFILE": "cpu", "ROUGH_CUT_VENV": chosen}
    assert run(sandbox, **settings).returncode == 0
    assert run(sandbox, "run-local", **settings).returncode == 0
    doctor = run(sandbox, "doctor-real", **settings)
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    assert f"{chosen}/bin/gunicorn" in log(sandbox)
    assert "cpu" in doctor.stdout and chosen in doctor.stdout
    assert "GEMINI_API_KEY" not in doctor.stdout
    assert "connector не отвечает" not in doctor.stdout


def test_auto_never_migrates_existing_cpu_environment(sandbox):
    assert run(sandbox, ROUGH_CUT_PROFILE="cpu").returncode == 0
    result = run(sandbox, "run-local")
    assert result.returncode == 2
    assert "ROUGH_CUT_PROFILE=cpu" in result.stderr
    assert (sandbox[0] / ".venv/bin/gunicorn").exists()
    assert not (sandbox[0] / ".venv-macos-arm64").exists()
    assert run(sandbox).returncode == 0
    assert run(sandbox, "run-local").returncode == 0
    assert f"{sandbox[0]}/.venv-macos-arm64/bin/gunicorn" in log(sandbox)


def test_native_wrappers_force_mlx(sandbox):
    assert run(sandbox, "setup-macos-apple-silicon", ROUGH_CUT_PROFILE="cpu").returncode == 0
    assert run(sandbox, "run-native-macos", ROUGH_CUT_PROFILE="cpu").returncode == 0
    assert f"{sandbox[0]}/.venv-macos-arm64/bin/gunicorn" in log(sandbox)


def test_bootstrap_passes_selected_venv_to_connector(sandbox):
    root = sandbox[0]
    for name in ("install-macos-connector-launchagent", "install-cep-macos"):
        executable(
            root / "scripts" / name,
            '#!/bin/bash\necho "integration $ROUGH_CUT_VENV" >> "$TEST_LOG"\n',
        )
    assert run(sandbox, "bootstrap-macos").returncode == 0
    assert f"integration {root}/.venv-macos-arm64" in log(sandbox)


def test_docker_repairs_blank_token_without_host_python(sandbox):
    root, env = sandbox
    (root / ".env").write_text("ROUGH_CUT_API_TOKEN=\nTTS_PROVIDER=custom\n")
    executable(root / "bin/openssl", '#!/bin/bash\nprintf "%064d\\n" 7\n')
    (root / "bin/python3").unlink()
    result = run(sandbox, "bootstrap-docker")
    assert result.returncode == 0, result.stderr
    assert re.search(r"^ROUGH_CUT_API_TOKEN=[0-9a-f]{64}$", (root / ".env").read_text(), re.M)
    assert "TTS_PROVIDER=custom" in (root / ".env").read_text()
    assert "docker compose up -d stage1-api" in log(sandbox)


@pytest.mark.parametrize("token", ["", '""', "''", "   # unset"])
def test_docker_fallback_fills_empty_values_without_evaluating_env(sandbox, token):
    root, _env = sandbox
    original = (
        f"ROUGH_CUT_API_TOKEN={token}\nTTS_PROVIDER=custom\nCUSTOM=$(touch should-not-exist)\n"
    )
    (root / ".env").write_text(original)
    executable(root / "bin/openssl", '#!/bin/bash\nprintf "%064d\\n" 7\n')
    (root / "bin/python3").unlink()
    result = run(sandbox, "bootstrap-docker")
    assert result.returncode == 0, result.stderr
    content = (root / ".env").read_text()
    assert re.search(r"^ROUGH_CUT_API_TOKEN=[0-9a-f]{64}$", content, re.M)
    assert content.endswith("TTS_PROVIDER=custom\nCUSTOM=$(touch should-not-exist)\n")
    assert not (root / "should-not-exist").exists()
    assert (
        "0000000000000000000000000000000000000000000000000000000000000007"
        not in result.stdout + result.stderr
    )


def test_doctor_native_tts_does_not_require_connector_or_cloud_keys(sandbox):
    root, _env = sandbox
    assert run(sandbox).returncode == 0
    result = run(sandbox, "doctor-real")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "say найден" in result.stdout and "afconvert найден" in result.stdout
    assert "connector" not in result.stdout and "GEMINI_API_KEY" not in result.stdout
    assert "mlx_whisper" in result.stdout


def test_doctor_reads_token_as_data_and_never_prints_it(sandbox):
    root, _env = sandbox
    assert run(sandbox).returncode == 0
    (root / ".env").write_text(
        'ROUGH_CUT_API_TOKEN="canary-secret-$(touch should-not-exist)"\nTTS_PROVIDER=macos\n'
    )
    result = run(sandbox, "doctor-real")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "canary-secret" not in result.stdout + result.stderr
    assert not (root / "should-not-exist").exists()


@pytest.mark.parametrize("value", ['"   "', "'  '"])
def test_setup_repairs_quoted_whitespace_token(sandbox, value):
    root, _env = sandbox
    (root / ".env").write_text(f"ROUGH_CUT_API_TOKEN={value}\nTTS_PROVIDER=macos\n")
    result = run(sandbox)
    assert result.returncode == 0, result.stderr
    assert re.search(r"^ROUGH_CUT_API_TOKEN=[0-9a-f]{64}$", (root / ".env").read_text(), re.M)


def test_doctor_keeps_configuration_warnings_and_rejects_removed_provider(sandbox):
    root, _env = sandbox
    assert run(sandbox).returncode == 0
    (root / ".env").write_text(
        "ROUGH_CUT_API_TOKEN=canary-secret\nTTS_PROVIDER=gemini\nMACOS_TTS_RATE=fast\n"
    )
    result = run(sandbox, "doctor-real")
    assert "TTS_PROVIDER must be openrouter or macos" in result.stdout
    assert "MACOS_TTS_RATE" in result.stdout
    assert "GEMINI_API_KEY" not in result.stdout
    assert "canary-secret" not in result.stdout + result.stderr


def limit_env_write_size():
    """Make partial regular-file writes fail with EFBIG in the child process."""
    import resource
    import signal

    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    resource.setrlimit(resource.RLIMIT_FSIZE, (64, 64))


@pytest.mark.parametrize("helper", ["python", "docker"])
def test_failed_env_write_preserves_original_and_cleans_temporary(sandbox, helper):
    root, env = sandbox
    original = b"ROUGH_CUT_API_TOKEN=\nTTS_PROVIDER=custom\nCUSTOM=" + b"x" * 256 + b"\n"
    target = root / ".env"
    target.write_bytes(original)
    target.chmod(0o640)
    executable(root / "bin/openssl", '#!/bin/bash\nprintf "%064d\\n" 7\n')
    if helper == "python":
        argv = [sys.executable, str(root / "scripts/lib/init-env.py"), str(root), "macos"]
    else:
        argv = [
            "/bin/bash",
            "-ec",
            'source "$ROOT_DIR/scripts/lib/docker-env.sh"; initialize_docker_env',
        ]
    result = subprocess.run(
        argv,
        env={**env, "ROOT_DIR": str(root)},
        text=True,
        capture_output=True,
        preexec_fn=limit_env_write_size,
        timeout=15,
    )
    assert result.returncode != 0
    assert target.read_bytes() == original
    assert target.stat().st_mode & 0o777 == 0o640
    assert not [path for path in root.glob(".env.*") if path.name != ".env.example"]


@pytest.mark.parametrize("helper", ["python", "docker"])
@pytest.mark.parametrize("existing", [True, False])
def test_env_replacement_preserves_permissions(sandbox, helper, existing):
    root, env = sandbox
    target = root / ".env"
    if existing:
        target.write_text("ROUGH_CUT_API_TOKEN=\nTTS_PROVIDER=custom\n")
        target.chmod(0o640)
    executable(root / "bin/openssl", '#!/bin/bash\nprintf "%064d\\n" 7\n')
    if helper == "python":
        argv = [sys.executable, str(root / "scripts/lib/init-env.py"), str(root), "macos"]
    else:
        argv = [
            "/bin/bash",
            "-ec",
            'source "$ROOT_DIR/scripts/lib/docker-env.sh"; initialize_docker_env',
        ]
    result = subprocess.run(
        argv, env={**env, "ROOT_DIR": str(root)}, capture_output=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
    assert target.stat().st_mode & 0o777 == (0o640 if existing else 0o600)
    assert not [path for path in root.glob(".env.*") if path.name != ".env.example"]
