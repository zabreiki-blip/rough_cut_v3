"""Failure-mode tests for the macOS TTS backend and the connector HTTP app.

``say`` / ``afconvert`` / ``subprocess`` are always mocked, so these tests do
not depend on macOS.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import macos_connector
from stage1.macos_tts_backend import (
    list_macos_voices,
    resolve_macos_voice,
    run_say_and_convert,
)


def _completed(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout, stderr)


def _called_error(stderr: str = "", stdout: str = "") -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(1, ["cmd"], output=stdout, stderr=stderr)


class _FakeRunner:
    """Scripted ``subprocess.run`` replacement that records every command."""

    def __init__(self, *outcomes: Any, wav: bytes = b"RIFFwav") -> None:
        self.outcomes = list(outcomes)
        self.commands: list[list[str]] = []
        self.timeouts: list[Any] = []
        self.wav = wav

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        self.timeouts.append(kwargs.get("timeout"))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, BaseException):
            raise outcome
        if command[0] == "afconvert":
            Path(command[-1]).write_bytes(self.wav)
        return _completed()


def _synth(runner: _FakeRunner, **kwargs: Any) -> bytes:
    params: dict[str, Any] = {
        "voice": "Milena",
        "rate": 175,
        "sample_rate": 24000,
        "timeout": 30,
        "run": runner,
    }
    params.update(kwargs)
    return run_say_and_convert("Привет", **params)


# --- list_macos_voices / resolve_macos_voice -------------------------------


def test_list_voices_returns_empty_when_say_is_missing() -> None:
    run = MagicMock()
    assert list_macos_voices(run=run, which=lambda _name: None) == ()
    run.assert_not_called()


def test_list_voices_parses_names_in_system_order_and_skips_blank_lines() -> None:
    stdout = "Milena ru_RU # Привет\n\n   \nAlex en_US # Hello\n"
    voices = list_macos_voices(run=lambda *_a, **_k: _completed(stdout), which=None)

    assert voices == ("Milena", "Alex")


@pytest.mark.parametrize(
    "error",
    [
        subprocess.TimeoutExpired(["say"], 15),
        _called_error("boom"),
        FileNotFoundError("say"),
    ],
)
def test_list_voices_logs_warning_and_returns_empty_on_failure(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    def fail(*_a: Any, **_k: Any) -> Any:
        raise error

    logger = logging.getLogger("test.macos.voices")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        assert list_macos_voices(run=fail, which=None, logger=logger) == ()

    assert "Could not enumerate macOS voices" in caplog.text


def test_list_voices_failure_without_logger_is_silent() -> None:
    def fail(*_a: Any, **_k: Any) -> Any:
        raise OSError("nope")

    assert list_macos_voices(run=fail, which=None) == ()


def test_resolve_voice_fallback_order_and_empty_inventory() -> None:
    assert resolve_macos_voice("", ()) == "Alex"
    assert resolve_macos_voice("Zoe", ()) == "Zoe"
    assert resolve_macos_voice("Zoe", ("Samantha", "Alex")) == "Samantha"
    # None of the preferred fallbacks installed -> first available voice.
    assert resolve_macos_voice("Zoe", ("Kyoko", "Daniel")) == "Kyoko"
    assert resolve_macos_voice("Daniel", ("Kyoko", "Daniel")) == "Daniel"


# --- run_say_and_convert ----------------------------------------------------


def test_say_and_convert_returns_wav_and_builds_expected_commands() -> None:
    runner = _FakeRunner()
    wav = _synth(runner, sample_rate=16000, rate=200)

    assert wav == b"RIFFwav"
    say, afconvert = runner.commands
    assert say[:5] == ["say", "-v", "Milena", "-r", "200"]
    assert afconvert[:5] == ["afconvert", "-f", "WAVE", "-d", "LEI16@16000"]
    assert runner.timeouts == [30, 30]


def test_say_without_voice_omits_voice_flag() -> None:
    runner = _FakeRunner()
    _synth(runner, voice="")

    assert "-v" not in runner.commands[0]


def test_say_timeout_is_reported_with_command_name() -> None:
    runner = _FakeRunner(subprocess.TimeoutExpired(["say"], 30))

    with pytest.raises(RuntimeError, match=r"macOS 'say' timed out after 30 seconds"):
        _synth(runner)

    assert len(runner.commands) == 1


def test_say_failure_without_output_uses_generic_message() -> None:
    runner = _FakeRunner(_called_error())

    with pytest.raises(RuntimeError, match="unknown say error"):
        _synth(runner)


def test_say_failure_falls_back_to_stdout_when_stderr_empty() -> None:
    runner = _FakeRunner(_called_error(stderr="", stdout="stdout details"))

    with pytest.raises(RuntimeError, match="stdout details"):
        _synth(runner)


def test_retry_with_default_voice_succeeds_on_second_attempt() -> None:
    runner = _FakeRunner(_called_error("voice not found"))

    wav = _synth(runner, retry_with_default_voice=True)

    assert wav == b"RIFFwav"
    first, second, afconvert = runner.commands
    assert "-v" in first and "-v" not in second
    assert afconvert[0] == "afconvert"


def test_retry_with_default_voice_is_skipped_when_no_voice_requested() -> None:
    runner = _FakeRunner(_called_error("broken"))

    with pytest.raises(RuntimeError, match="broken"):
        _synth(runner, voice="", retry_with_default_voice=True)

    assert len(runner.commands) == 1


def test_retry_failure_reports_retry_stderr() -> None:
    runner = _FakeRunner(_called_error("first"), _called_error("second"))

    with pytest.raises(RuntimeError, match="second"):
        _synth(runner, retry_with_default_voice=True)

    assert len(runner.commands) == 2


def test_retry_failure_without_details_reports_original_error() -> None:
    runner = _FakeRunner(_called_error("original problem"), _called_error())

    with pytest.raises(RuntimeError, match="original problem"):
        _synth(runner, retry_with_default_voice=True)


def test_retry_timeout_is_reported() -> None:
    runner = _FakeRunner(_called_error("first"), subprocess.TimeoutExpired(["say"], 30))

    with pytest.raises(RuntimeError, match=r"macOS 'say' timed out after 30 seconds"):
        _synth(runner, retry_with_default_voice=True)


def test_afconvert_timeout_and_failure_use_second_command_name() -> None:
    timeout_runner = _FakeRunner(None, subprocess.TimeoutExpired(["afconvert"], 30))
    with pytest.raises(RuntimeError, match=r"^afconvert timed out after 30 seconds$"):
        _synth(timeout_runner, command_names=("say", "afconvert"))

    failing_runner = _FakeRunner(None, _called_error("bad format"))
    with pytest.raises(RuntimeError, match=r"^afconvert failed: bad format$"):
        _synth(failing_runner, command_names=("say", "afconvert"))

    silent_runner = _FakeRunner(None, _called_error())
    with pytest.raises(RuntimeError, match="unknown afconvert error"):
        _synth(silent_runner)


def test_temp_directory_is_removed_after_failure() -> None:
    seen: list[Path] = []

    def run(command: list[str], **_kwargs: Any) -> Any:
        seen.append(Path(command[command.index("-f") + 1]).parent)
        raise _called_error("x")

    with pytest.raises(RuntimeError):
        run_say_and_convert("t", voice="", rate=175, sample_rate=24000, timeout=1, run=run)

    assert seen and not seen[0].exists()


# --- connector HTTP app -----------------------------------------------------


@pytest.fixture
def client():
    macos_connector.app.config.update(TESTING=True)
    macos_connector._available_voices.cache_clear()
    with patch.dict("os.environ", {"MACOS_CONNECTOR_TOKEN": "tok"}):
        yield macos_connector.app.test_client()
    macos_connector._available_voices.cache_clear()


AUTH = {"Authorization": "Bearer tok"}


def test_health_reports_tool_availability(client) -> None:
    with patch.object(
        macos_connector,
        "shutil_which",
        side_effect=lambda cmd: "/bin/say" if cmd == "say" else None,
    ):
        body = client.get("/health").get_json()

    assert body == {"ok": True, "say_available": True, "afconvert_available": False}


def test_shutil_which_delegates_to_shutil() -> None:
    with patch("shutil.which", return_value="/usr/bin/say") as which:
        assert macos_connector.shutil_which("say") == "/usr/bin/say"

    which.assert_called_once_with("say")


@pytest.mark.parametrize("missing", ["say", "afconvert"])
def test_missing_host_tools_return_500_with_tool_name(client, missing: str) -> None:
    with patch.object(
        macos_connector, "shutil_which", side_effect=lambda cmd: None if cmd == missing else "/x"
    ):
        response = client.post("/synthesize", json={"text": "Тест"}, headers=AUTH)

    assert response.status_code == 500
    assert response.get_json()["error"] == f"{missing} command not found"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({}, "text is required"),
        ({"text": "   "}, "text is required"),
        ({"text": "я" * 12001}, "text too long"),
    ],
)
def test_invalid_text_is_rejected_before_synthesis(client, payload: dict, expected: str) -> None:
    with (
        patch.object(macos_connector, "shutil_which", return_value="/x"),
        patch.object(macos_connector, "_run_say_and_convert") as synth,
    ):
        response = client.post("/synthesize", json=payload, headers=AUTH)

    assert response.status_code == 400
    assert response.get_json()["error"] == expected
    synth.assert_not_called()


def test_non_json_body_is_treated_as_missing_text(client) -> None:
    with patch.object(macos_connector, "shutil_which", return_value="/x"):
        response = client.post(
            "/synthesize", data="not json", headers={**AUTH, "Content-Type": "text/plain"}
        )

    assert response.status_code == 400


def test_text_at_limit_is_accepted(client) -> None:
    with (
        patch.object(macos_connector, "shutil_which", return_value="/x"),
        patch.object(macos_connector, "_run_say_and_convert", return_value=b"W") as synth,
    ):
        response = client.post("/synthesize", json={"text": "я" * 12000}, headers=AUTH)

    assert response.status_code == 200
    assert len(synth.call_args.kwargs["text"]) == 12000


def test_oversized_body_returns_json_413(client) -> None:
    limit = macos_connector.app.config["MAX_CONTENT_LENGTH"]
    with patch.object(macos_connector, "shutil_which", return_value="/x"):
        response = client.post("/synthesize", json={"text": "я" * (limit + 10)}, headers=AUTH)

    assert response.status_code == 413
    assert response.get_json() == {"ok": False, "error": "request body is too large"}


def test_rate_and_sample_rate_are_clamped_and_known_voice_is_used(client) -> None:
    with (
        patch.object(macos_connector, "shutil_which", return_value="/x"),
        patch.object(macos_connector, "_available_voices", return_value=frozenset({"Milena"})),
        patch.object(macos_connector, "_run_say_and_convert", return_value=b"WAV") as synth,
    ):
        response = client.post(
            "/synthesize",
            json={"text": "Тест", "voice": "Milena", "rate": 9999, "sample_rate": "bad"},
            headers=AUTH,
        )

    assert response.status_code == 200
    assert response.headers["X-Voice-Used"] == "Milena"
    assert response.mimetype == "audio/wav"
    kwargs = synth.call_args.kwargs
    assert kwargs["rate"] == 420
    assert kwargs["voice"] == "Milena"
    assert kwargs["sample_rate"] == 24000


def test_low_rate_is_clamped_up(client) -> None:
    with (
        patch.object(macos_connector, "shutil_which", return_value="/x"),
        patch.object(macos_connector, "_run_say_and_convert", return_value=b"W") as synth,
    ):
        client.post("/synthesize", json={"text": "Тест", "rate": 1, "sample_rate": 1}, headers=AUTH)

    assert synth.call_args.kwargs["rate"] == 80
    assert synth.call_args.kwargs["sample_rate"] == 8000


def test_x_connector_token_header_is_accepted_and_bad_one_rejected(client) -> None:
    with (
        patch.object(macos_connector, "shutil_which", return_value="/x"),
        patch.object(macos_connector, "_run_say_and_convert", return_value=b"W"),
    ):
        ok = client.post("/synthesize", json={"text": "Т"}, headers={"X-Connector-Token": "tok"})
        bad = client.post("/synthesize", json={"text": "Т"}, headers={"X-Connector-Token": "no"})
        none = client.post("/synthesize", json={"text": "Т"})

    assert (ok.status_code, bad.status_code, none.status_code) == (200, 401, 401)


def test_timeout_during_synthesis_returns_safe_500(client) -> None:
    runner_error = subprocess.TimeoutExpired(["say"], 900)
    with (
        patch.object(macos_connector, "shutil_which", return_value="/x"),
        patch.object(macos_connector.subprocess, "run", side_effect=runner_error),
    ):
        response = client.post("/synthesize", json={"text": "Тест"}, headers=AUTH)

    assert response.status_code == 500
    assert response.get_json() == {"ok": False, "error": "macOS synthesis failed"}


def test_run_say_and_convert_uses_env_timeout_clamped_to_minimum() -> None:
    with (
        patch.dict("os.environ", {"MACOS_COMMAND_TIMEOUT": "1"}),
        patch.object(macos_connector, "run_say_and_convert", return_value=b"W") as backend,
    ):
        assert macos_connector._run_say_and_convert("т", "", 175, 24000) == b"W"

    assert backend.call_args.kwargs["timeout"] == 30
    assert backend.call_args.kwargs["command_names"] == ("say", "afconvert")


def test_available_voices_returns_empty_set_when_say_fails() -> None:
    macos_connector._available_voices.cache_clear()
    try:
        with patch.object(
            macos_connector.subprocess, "run", side_effect=subprocess.TimeoutExpired(["say"], 15)
        ):
            assert macos_connector._available_voices() == frozenset()
            assert macos_connector._voice_exists("Milena") is False
            assert macos_connector._voice_exists("") is False
    finally:
        macos_connector._available_voices.cache_clear()


def test_to_int_clamps_and_defaults() -> None:
    assert macos_connector._to_int(None, 5, 1, 10) == 5
    assert macos_connector._to_int("abc", 5, 1, 10) == 5
    assert macos_connector._to_int("50", 5, 1, 10) == 10
    assert macos_connector._to_int(-3, 5, 1, 10) == 1
