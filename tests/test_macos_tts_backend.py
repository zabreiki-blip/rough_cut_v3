from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

import macos_connector
from stage1.macos_tts_backend import resolve_macos_voice, run_say_and_convert


class MacosVoicePolicyTests(unittest.TestCase):
    def test_local_policy_keeps_requested_voice_and_uses_ordered_fallbacks(self) -> None:
        voices = ("Alex", "Yuri", "Samantha")

        self.assertEqual(resolve_macos_voice("Samantha", voices), "Samantha")
        self.assertEqual(resolve_macos_voice("Missing", voices), "Yuri")

    def test_connector_policy_uses_system_default_for_unknown_voice(self) -> None:
        fake_wav = b"RIFF-test"
        with (
            patch.dict("os.environ", {"MACOS_CONNECTOR_TOKEN": "token"}),
            patch.object(macos_connector, "shutil_which", return_value="/usr/bin/tool"),
            patch.object(macos_connector, "_available_voices", return_value=frozenset({"Yuri"})),
            patch.object(macos_connector, "_run_say_and_convert", return_value=fake_wav) as run,
        ):
            response = macos_connector.app.test_client().post(
                "/synthesize",
                json={"text": "Тест", "voice": "Missing"},
                headers={"Authorization": "Bearer token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Voice-Used"], "default")
        self.assertEqual(run.call_args.kwargs["voice"], "")

    def test_shared_runner_does_not_retry_when_connector_policy_disables_it(self) -> None:
        commands: list[list[str]] = []

        def fail_say(command, **_kwargs):
            commands.append(command)
            raise subprocess.CalledProcessError(1, command, stderr="bad voice")

        with self.assertRaisesRegex(RuntimeError, "bad voice"):
            run_say_and_convert(
                "Тест",
                voice="Milena",
                rate=175,
                sample_rate=24000,
                timeout=30,
                retry_with_default_voice=False,
                run=fail_say,
            )

        self.assertEqual(len(commands), 1)


if __name__ == "__main__":
    unittest.main()
