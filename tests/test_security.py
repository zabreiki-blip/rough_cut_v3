from __future__ import annotations

import base64
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import api
import macos_connector


ROOT_DIR = Path(__file__).resolve().parents[1]


class ApiAuthenticationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.projects_patch = patch.object(
            api, "PROJECTS_DIR", Path(self.temp.name) / "projects"
        )
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

        response = self.client.get(
            "/", headers={"Authorization": "Bearer wrong-token"}
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"], "unauthorized")

    def test_bearer_token_is_accepted(self) -> None:
        api.app.config["API_TOKEN"] = "correct-token"

        response = self.client.get(
            "/", headers={"Authorization": "Bearer correct-token"}
        )

        self.assertEqual(response.status_code, 200)

    def test_basic_password_is_accepted(self) -> None:
        api.app.config["API_TOKEN"] = "correct-token"
        encoded = base64.b64encode(b"roughcut:correct-token").decode("ascii")

        response = self.client.get(
            "/", headers={"Authorization": f"Basic {encoded}"}
        )

        self.assertEqual(response.status_code, 200)

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


class ContainerSecurityConfigurationTest(unittest.TestCase):
    def test_compose_exposes_only_localhost_without_reload(self) -> None:
        compose = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn('"127.0.0.1:8000:8000"', compose)
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
            line.strip().removeprefix("- ")
            for line in volumes_match.group("mounts").splitlines()
        }
        self.assertEqual(
            mounts,
            {"./projects:/app/projects", "./data:/app/data"},
        )

    def test_dockerfile_runs_as_non_root_user(self) -> None:
        dockerfile = (ROOT_DIR / "Dockerfile").read_text(encoding="utf-8")
        users = re.findall(r"^USER\s+(\S+)\s*$", dockerfile, flags=re.MULTILINE)

        self.assertTrue(users, "Dockerfile must declare a runtime USER")
        self.assertNotEqual(users[-1].lower(), "root")


if __name__ == "__main__":
    unittest.main()
