from __future__ import annotations

from pathlib import Path
import subprocess
import threading
import time
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


class MacosConnectorSerializationTests(unittest.TestCase):
    def test_concurrent_requests_do_not_run_say_in_parallel(self) -> None:
        active = 0
        peak = 0
        guard = threading.Lock()

        def fake_run(command, **_kwargs):
            nonlocal active, peak
            if command[0] == "say":
                with guard:
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.15)
                with guard:
                    active -= 1
            if command[0] == "afconvert":
                Path(command[-1]).write_bytes(b"RIFF-test")
            return subprocess.CompletedProcess(command, 0, "", "")

        results: list[bytes] = []

        def worker() -> None:
            results.append(macos_connector._run_say_and_convert("Текст", "Milena", 175, 24000))

        with patch.object(macos_connector.subprocess, "run", side_effect=fake_run):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertEqual(results, [b"RIFF-test"] * 2)
        self.assertEqual(peak, 1)

    def test_slot_is_released_when_synthesis_raises(self) -> None:
        with patch.object(macos_connector, "run_say_and_convert", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                macos_connector._run_say_and_convert("Текст", "Milena", 175, 24000)

        results: list[bytes] = []

        def second() -> None:
            results.append(macos_connector._run_say_and_convert("Текст", "Milena", 175, 24000))

        with patch.object(macos_connector, "run_say_and_convert", return_value=b"RIFF-ok"):
            thread = threading.Thread(target=second, daemon=True)
            thread.start()
            thread.join(timeout=5)

        self.assertFalse(thread.is_alive(), "semaphore was not released after an exception")
        self.assertEqual(results, [b"RIFF-ok"])


if __name__ == "__main__":
    unittest.main()
