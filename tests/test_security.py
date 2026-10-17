from __future__ import annotations

import base64
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pytest

import api
import macos_connector
import stage1.gemini_tts as gemini_tts


ROOT_DIR = Path(__file__).resolve().parents[1]


class ApiAuthenticationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.projects_patch = patch.object(api, "PROJECTS_DIR", Path(self.temp.name) / "projects")
        self.projects_patch.start()

        self.had_api_token = "API_TOKEN" in api.app.config
        self.previous_api_token = api.app.config.get("API_TOKEN")
        self.previous_testing = api.app.config.get("TESTING", False)
        api.app.config.update(TESTING=True)
        self.client = api.app.test_client()

    def tearDown(self) -> None:
        if self.had_api_token:
            api.app.config["API_TOKEN"] = self.previous_api_token
        else:
            api.app.config.pop("API_TOKEN", None)
        api.app.config["TESTING"] = self.previous_testing
        self.projects_patch.stop()
        self.temp.cleanup()

    def test_missing_token_configuration_fails_closed(self) -> None:
        api.app.config["API_TOKEN"] = ""

        response = self.client.get("/")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.get_json()["error"],
            "ROUGH_CUT_API_TOKEN is not configured",
        )

    def test_wrong_token_is_unauthorized(self) -> None:
        api.app.config["API_TOKEN"] = "correct-token"

        response = self.client.get("/", headers={"Authorization": "Bearer wrong-token"})

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"], "unauthorized")
        self.assertNotIn("WWW-Authenticate", response.headers)

    def test_direct_browser_request_keeps_basic_challenge(self) -> None:
        api.app.config["API_TOKEN"] = "correct-token"

        response = self.client.get("/")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.headers["WWW-Authenticate"],
            'Basic realm="Rough Cut", charset="UTF-8"',
        )

    def test_bearer_token_is_accepted(self) -> None:
        api.app.config["API_TOKEN"] = "correct-token"

        response = self.client.get("/", headers={"Authorization": "Bearer correct-token"})

        self.assertEqual(response.status_code, 200)

    def test_basic_password_is_accepted(self) -> None:
        api.app.config["API_TOKEN"] = "correct-token"
        encoded = base64.b64encode(b"roughcut:correct-token").decode("ascii")

        response = self.client.get("/", headers={"Authorization": f"Basic {encoded}"})

        self.assertEqual(response.status_code, 200)

    def test_local_cep_cors_preflight_is_explicit_and_auth_remains_required(self) -> None:
        api.app.config["API_TOKEN"] = "correct-token"
        api.app.config["CEP_CORS_ENABLED"] = True
        self.addCleanup(api.app.config.pop, "CEP_CORS_ENABLED", None)

        preflight = self.client.options(
            "/stage2/sync-sources",
            headers={
                "Origin": "null",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        self.assertEqual(preflight.status_code, 200)
        self.assertEqual(preflight.headers["Access-Control-Allow-Origin"], "null")
        self.assertIn(
            "Authorization",
            preflight.headers["Access-Control-Allow-Headers"],
        )

        unauthenticated = self.client.get(
            "/stage2/sync-sources?project=demo",
            headers={"Origin": "null"},
        )
        self.assertEqual(unauthenticated.status_code, 401)
        self.assertNotIn("WWW-Authenticate", unauthenticated.headers)
        self.assertEqual(
            unauthenticated.headers["Access-Control-Allow-Origin"],
            "null",
        )

        untrusted_origin = self.client.options(
            "/stage2/sync-sources",
            headers={
                "Origin": "https://example.invalid",
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertEqual(untrusted_origin.status_code, 401)
        self.assertNotIn(
            "Access-Control-Allow-Origin",
            untrusted_origin.headers,
        )

    def test_health_is_public_when_token_is_not_configured(self) -> None:
        api.app.config["API_TOKEN"] = ""

        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])

    def test_request_body_limit_preserves_audio_limit_plus_multipart_headroom(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SYNC_AUDIO_MAX_BYTES": "1000000",
                "ROUGH_CUT_MAX_REQUEST_BODY_BYTES": "1",
            },
            clear=False,
        ):
            limit = api._request_body_limit()

        self.assertEqual(limit, 1000000 + api._REQUEST_BODY_MULTIPART_HEADROOM)

    def test_request_body_limit_returns_json_413(self) -> None:
        previous = api.app.config["MAX_CONTENT_LENGTH"]
        self.addCleanup(api.app.config.__setitem__, "MAX_CONTENT_LENGTH", previous)
        api.app.config["MAX_CONTENT_LENGTH"] = 1

        response = self.client.post("/stage1/analyze", json={"project": "demo"})

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.get_json()["error"], "request body is too large")


class MacosConnectorAuthenticationTest(unittest.TestCase):
    def setUp(self) -> None:
        macos_connector.app.config.update(TESTING=True)
        self.client = macos_connector.app.test_client()

    def test_empty_token_fails_closed(self) -> None:
        with patch.dict(os.environ, {"MACOS_CONNECTOR_TOKEN": ""}):
            response = self.client.post("/synthesize", json={"text": "Тест"})

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.get_json()["error"],
            "MACOS_CONNECTOR_TOKEN is not configured",
        )

    def test_wrong_token_is_unauthorized(self) -> None:
        with patch.dict(os.environ, {"MACOS_CONNECTOR_TOKEN": "correct-token"}):
            response = self.client.post(
                "/synthesize",
                json={"text": "Тест"},
                headers={"Authorization": "Bearer wrong-token"},
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"], "unauthorized")

    def test_correct_token_reaches_synthesis(self) -> None:
        fake_wav = b"RIFF-fake-wave"
        with (
            patch.dict(os.environ, {"MACOS_CONNECTOR_TOKEN": "correct-token"}),
            patch.object(macos_connector, "shutil_which", return_value="/usr/bin/tool"),
            patch.object(
                macos_connector, "_run_say_and_convert", return_value=fake_wav
            ) as synthesize,
        ):
            response = self.client.post(
                "/synthesize",
                json={"text": "Тест"},
                headers={"Authorization": "Bearer correct-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, fake_wav)
        synthesize.assert_called_once()

    def test_synthesis_failure_does_not_expose_raw_connector_error(self) -> None:
        secret = "connector-internal-secret-123"
        with (
            patch.dict(os.environ, {"MACOS_CONNECTOR_TOKEN": "correct-token"}),
            patch.object(macos_connector, "shutil_which", return_value="/usr/bin/tool"),
            patch.object(
                macos_connector,
                "_run_say_and_convert",
                side_effect=RuntimeError(f"say failed: {secret}"),
            ),
        ):
            response = self.client.post(
                "/synthesize",
                json={"text": "Тест"},
                headers={"Authorization": "Bearer correct-token"},
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["error"], "macOS synthesis failed")
        self.assertNotIn(secret, response.get_data(as_text=True))

    def test_unexpected_synthesis_failure_also_returns_safe_json(self) -> None:
        secret = "connector-internal-secret-456"
        with (
            patch.dict(os.environ, {"MACOS_CONNECTOR_TOKEN": "correct-token"}),
            patch.object(macos_connector, "shutil_which", return_value="/usr/bin/tool"),
            patch.object(
                macos_connector,
                "_run_say_and_convert",
                side_effect=OSError(f"host failure: {secret}"),
            ),
        ):
            response = self.client.post(
                "/synthesize",
                json={"text": "Тест"},
                headers={"Authorization": "Bearer correct-token"},
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["error"], "macOS synthesis failed")
        self.assertNotIn(secret, response.get_data(as_text=True))

    def test_voice_inventory_is_cached_between_blocks(self) -> None:
        result = subprocess.CompletedProcess(
            ["say", "-v", "?"],
            0,
            "Milena ru_RU # Russian\nYuri ru_RU # Russian\n",
            "",
        )
        macos_connector._available_voices.cache_clear()
        try:
            with patch.object(macos_connector.subprocess, "run", return_value=result) as run:
                self.assertTrue(macos_connector._voice_exists("Milena"))
                self.assertTrue(macos_connector._voice_exists("Yuri"))
            run.assert_called_once()
        finally:
            macos_connector._available_voices.cache_clear()

    def test_multiline_dash_text_is_read_from_file_not_argv(self) -> None:
        commands = []
        file_contents = []

        def fake_run(command, **_kwargs):
            commands.append(command)
            if command[0] == "say":
                path = Path(command[command.index("-f") + 1])
                file_contents.append(path.read_text(encoding="utf-8"))
            if command[0] == "afconvert":
                Path(command[-1]).write_bytes(b"RIFF-test")
            return subprocess.CompletedProcess(command, 0, "", "")

        text = "-\nПервая строка\n\nВторой абзац"
        with patch.object(macos_connector.subprocess, "run", side_effect=fake_run):
            result = macos_connector._run_say_and_convert(text, "Milena", 175, 24000)

        self.assertEqual(result, b"RIFF-test")
        say_command = commands[0]
        self.assertNotIn(text, say_command)
        self.assertIn("-f", say_command)
        input_path = Path(say_command[say_command.index("-f") + 1])
        self.assertEqual(input_path.name, "tts.txt")
        self.assertEqual(file_contents, [text])

    def test_macos_command_timeout_becomes_actionable_error(self) -> None:
        with patch.object(
            macos_connector.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["say"], 900),
        ):
            with self.assertRaisesRegex(RuntimeError, "say timed out"):
                macos_connector._run_say_and_convert("Текст", "Milena", 175, 24000)

    def test_local_macos_fallback_also_reads_multiline_dash_text_from_file(self) -> None:
        commands = []
        file_contents = []
        say_calls = 0

        def fake_run(command, **_kwargs):
            nonlocal say_calls
            commands.append(command)
            if command[0] == "say":
                path = Path(command[command.index("-f") + 1])
                file_contents.append(path.read_text(encoding="utf-8"))
                say_calls += 1
                if say_calls == 1:
                    raise subprocess.CalledProcessError(1, command, stderr="voice unavailable")
            if command[0] == "afconvert":
                Path(command[-1]).write_bytes(b"RIFF-local")
            return subprocess.CompletedProcess(command, 0, "", "")

        settings = SimpleNamespace(macos_tts_rate=175, macos_tts_sample_rate=24000)
        text = "-\nПервая строка\n\nВторой абзац"
        with (
            patch.object(gemini_tts.shutil, "which", return_value="/usr/bin/tool"),
            patch.object(gemini_tts, "_resolve_macos_voice", return_value="Milena"),
            patch.object(gemini_tts.subprocess, "run", side_effect=fake_run),
        ):
            result = gemini_tts._synthesize_macos_wav(text, settings, "Milena")

        self.assertEqual(result, b"RIFF-local")
        say_commands = [command for command in commands if command[0] == "say"]
        self.assertEqual(len(say_commands), 2)
        self.assertTrue(all("-f" in command for command in say_commands))
        self.assertTrue(all(text not in command for command in say_commands))
        self.assertEqual(file_contents, [text, text])


class ContainerSecurityConfigurationTest(unittest.TestCase):
    def test_compose_exposes_only_localhost_without_reload(self) -> None:
        compose = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn(
            '"${ROUGH_CUT_BIND:-127.0.0.1}:${ROUGH_CUT_PORT:-8000}:8000"',
            compose,
        )
        self.assertNotIn("--reload", compose)
        self.assertRegex(compose, r"(?:^|\s)--workers\s+1\b")

    def test_compose_uses_only_narrow_runtime_mounts(self) -> None:
        compose = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")
        volumes_match = re.search(
            r"^\s{4}volumes:\s*\n(?P<mounts>(?:\s{6}-[^\n]+\n)+)",
            compose,
            flags=re.MULTILINE,
        )

        self.assertIsNotNone(volumes_match, "service volumes section is missing")
        mounts = {
            line.strip().removeprefix("- ") for line in volumes_match.group("mounts").splitlines()
        }
        self.assertEqual(
            mounts,
            {
                "./projects:/app/projects",
                "./data:/app/data",
                "whisper_cache:/app/.cache/huggingface",
            },
        )

    def test_dockerfile_runs_as_non_root_user(self) -> None:
        dockerfile = (ROOT_DIR / "Dockerfile").read_text(encoding="utf-8")
        users = re.findall(r"^USER\s+(\S+)\s*$", dockerfile, flags=re.MULTILINE)

        self.assertTrue(users, "Dockerfile must declare a runtime USER")
        self.assertNotEqual(users[-1].lower(), "root")


if __name__ == "__main__":
    unittest.main()


def _lock_versions(filename: str, name: str) -> set[str]:
    """Versions of ``name`` pinned in a uv-generated hashed lock (marker variants included)."""
    wanted = name.lower().replace("_", "-")
    versions = set()
    for line in (ROOT_DIR / filename).read_text().splitlines():
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)", line)
        if match and re.sub(r"[-_.]+", "-", match.group(1).lower()) == wanted:
            versions.add(match.group(2))
    return versions


def _runtime_pin(name: str) -> str:
    direct = [
        line.split("==", 1)[1].strip()
        for line in (ROOT_DIR / "requirements.txt").read_text().splitlines()
        if line.lower().startswith(f"{name}==")
    ]
    assert len(direct) == 1, f"expected one {name} pin in requirements.txt"
    # The runtime lock must resolve the direct pin to exactly the same version.
    assert _lock_versions("requirements-lock.txt", name) == {direct[0]}
    return direct[0]


def test_runtime_requests_pin_is_patched() -> None:
    from packaging.version import Version

    # CVE-2026-25645 / PYSEC-2026-2275 fixed in 2.33.0.
    assert Version(_runtime_pin("requests")) >= Version("2.33.0")


def test_runtime_flask_pin_is_patched() -> None:
    from packaging.version import Version

    # CVE-2026-27205 / PYSEC-2026-2151 / GHSA-68rp-wp8r-4726 fixed in 3.1.3.
    assert Version(_runtime_pin("flask")) >= Version("3.1.3")


def test_runtime_gunicorn_pin_is_patched() -> None:
    from packaging.version import Version

    assert Version(_runtime_pin("gunicorn")) >= Version("23.0.0")


def test_runtime_ci_checks_production_asr_dependencies() -> None:
    workflow = (ROOT_DIR / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    match = re.search(r"^  runtime-smoke:\n(?P<job>.*?)(?=^  \S|\Z)", workflow, re.M | re.S)
    assert match is not None, "production dependencies need a separate runtime-smoke job"
    job = match.group("job")
    assert "runs-on: ubuntu-24.04" in job
    assert 'python-version: "3.11"' in job
    assert "cache: pip" in job
    assert "requirements-lock.txt" in job
    assert "python -m pip install --require-hashes -r requirements-lock.txt" in job
    assert "python tests/runtime_import_smoke.py" in job
    assert "SYNC_WHISPER_BACKEND: faster-whisper" in job
    assert "docker build ." in job
    assert (ROOT_DIR / "tests/runtime_import_smoke.py").is_file()


def test_ci_cancels_superseded_runs_and_pins_node_for_npm() -> None:
    workflow = (ROOT_DIR / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert re.search(
        r"^concurrency:\n  group: \$\{\{ github\.workflow \}\}-\$\{\{ github\.ref \}\}\n"
        r"  cancel-in-progress: true$",
        workflow,
        re.M,
    )
    match = re.search(r"^  verify:\n(?P<job>.*?)(?=^  \S|\Z)", workflow, re.M | re.S)
    assert match is not None
    job = match.group("job")
    node_setup = re.search(
        r"uses: actions/setup-node@v4\n\s+with:\n\s+node-version: \"(\d+)\"\n\s+cache: npm", job
    )
    assert node_setup is not None, "verify must pin a Node major version with npm cache"
    assert job.index("actions/setup-node@v4") < job.index("npm ci")


def test_ci_audits_runtime_dependencies_for_vulnerabilities() -> None:
    workflow = (ROOT_DIR / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert re.search(r"pip install pip-audit==\d+\.\d+\.\d+", workflow)
    match = re.search(r"^  dependency-audit:\n(?P<job>.*?)(?=^  \S|\Z)", workflow, re.M | re.S)
    assert match is not None
    job = match.group("job")
    # Runtime lock (what Docker ships) and dev lock (what CI verify runs) are both audited.
    assert "pip-audit --require-hashes --disable-pip -r requirements-lock.txt" in job
    assert "pip-audit --require-hashes --disable-pip -r requirements-dev-lock.txt" in job
    assert "continue-on-error" not in job


def test_dependabot_covers_python_actions_and_npm_weekly() -> None:
    config = (ROOT_DIR / ".github/dependabot.yml").read_text(encoding="utf-8")
    assert re.search(r"^version: 2$", config, re.M)
    blocks = re.split(r"^  - ", config, flags=re.M)[1:]
    updates = {
        match.group(1): block
        for block in blocks
        if (match := re.search(r'package-ecosystem: "?([\w-]+)"?', block))
    }
    assert {"pip", "github-actions", "npm"} <= set(updates)
    for block in updates.values():
        assert re.search(r'directory: "/"', block)
        assert re.search(r"interval: weekly", block)
    assert "open-pull-requests-limit: 5" in updates["pip"]
    assert "groups:" in updates["pip"] and "minor" in updates["pip"] and "patch" in updates["pip"]


@pytest.mark.parametrize("resolved_backend", ["faster-whisper", "openai-whisper"])
def test_runtime_smoke_requires_application_faster_whisper_backend(resolved_backend: str) -> None:
    # The verify job intentionally has no ASR dependency. Only replace the external
    # import and resolver result; execute the smoke's real acceptance check.
    with (
        patch.dict(sys.modules, {"faster_whisper": SimpleNamespace()}),
        patch("stage2.whisper_runner.resolve_backend", return_value=resolved_backend),
    ):
        if resolved_backend == "faster-whisper":
            runpy.run_path(str(ROOT_DIR / "tests/runtime_import_smoke.py"), run_name="__main__")
        else:
            with pytest.raises(AssertionError, match="faster-whisper"):
                runpy.run_path(str(ROOT_DIR / "tests/runtime_import_smoke.py"), run_name="__main__")


def test_final_review_operator_docs_cover_recovery_and_cost_contracts() -> None:
    readme = (ROOT_DIR / "README.md").read_text(encoding="utf-8")
    guide = (ROOT_DIR / "docs/STAGE1_GUIDE.md").read_text(encoding="utf-8")
    for document in (readme, guide):
        for contract in (
            "/stage2/final-review/estimate",
            "/stage2/final-review/latest?project=",
            "/stage2/final-review/jobs/<job_id>/cancel",
            "FINAL_REVIEW_TIMEOUT_SECONDS",
            "7200",
            "ROUGH_CUT_MAX_USD_PER_JOB",
            "ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE",
            "confirmed_estimate_usd",
            "reserved_cost_usd",
            "cost_pending",
        ):
            assert contract in document, f"operator documentation missing {contract}"
    checklist = (ROOT_DIR / "docs/E2E_PILOT_CHECKLIST.md").read_text(encoding="utf-8")
    for contract in (
        "confirmed_estimate_usd",
        "3, 6, 12, 24, 30",
        "Retry",
        "Cancel",
        "sequenceId",
        "request_id",
        "interrupted",
        "cost_pending",
    ):
        assert contract in checklist, f"E2E final review coverage missing {contract}"
    assert "тихий отказ" not in checklist
