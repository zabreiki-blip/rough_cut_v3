from __future__ import annotations

import base64
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

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
