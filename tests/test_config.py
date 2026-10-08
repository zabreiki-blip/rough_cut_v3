from __future__ import annotations

import pytest

from stage1.config import get_settings, validate_env


def test_validate_env_reports_typos_without_echoing_secrets() -> None:
    warnings = validate_env(
        {
            "TTS_PROVIDER": "gemni",
            "TTS_GENDER": "robot",
            "MAX_ZK_BLOCKS": "forty",
            "ROUGH_CUT_API_TOKEN": "",
            "OPENROUTER_API_KEY": "super-secret",
        }
    )

    rendered = "\n".join(warnings)
    assert "TTS_PROVIDER" in rendered
    assert "TTS_GENDER" in rendered
    assert "MAX_ZK_BLOCKS" in rendered
    assert "ROUGH_CUT_API_TOKEN" in rendered
    assert "super-secret" not in rendered


def test_validate_env_accepts_valid_local_configuration() -> None:
    warnings = validate_env(
        {
            "TTS_PROVIDER": "macos",
            "TTS_GENDER": "female",
            "ROUGH_CUT_API_TOKEN": "configured",
            "MAX_ZK_BLOCKS": "40",
            "MACOS_COMMAND_TIMEOUT": "900",
            "MACOS_CONNECTOR_URL": "http://127.0.0.1:8001/synthesize",
        }
    )

    assert warnings == []


def test_validate_env_rejects_removed_direct_gemini_provider() -> None:
    warnings = validate_env(
        {
            "TTS_PROVIDER": "gemini",
            "TTS_GENDER": "female",
            "ROUGH_CUT_API_TOKEN": "configured",
        }
    )

    assert "TTS_PROVIDER must be openrouter or macos" in warnings


def test_validate_env_checks_openrouter_max_tokens() -> None:
    warnings = validate_env({"OPENROUTER_MAX_TOKENS": "0", "ROUGH_CUT_API_TOKEN": "x"})

    assert any("OPENROUTER_MAX_TOKENS" in item for item in warnings)


def test_connector_timeout_default_exceeds_command_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MACOS_CONNECTOR_TIMEOUT", raising=False)
    monkeypatch.delenv("MACOS_COMMAND_TIMEOUT", raising=False)

    settings = get_settings()

    assert settings.macos_connector_timeout > settings.macos_command_timeout


def test_connector_timeout_default_follows_custom_command_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MACOS_CONNECTOR_TIMEOUT", raising=False)
    monkeypatch.setenv("MACOS_COMMAND_TIMEOUT", "1200")

    settings = get_settings()

    assert settings.macos_command_timeout == 1200
    assert settings.macos_connector_timeout > 1200


def test_explicit_connector_timeout_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MACOS_CONNECTOR_TIMEOUT", "45")
    monkeypatch.setenv("MACOS_COMMAND_TIMEOUT", "900")

    assert get_settings().macos_connector_timeout == 45


def test_validate_env_warns_when_connector_timeout_below_command_timeout() -> None:
    warnings = validate_env(
        {
            "ROUGH_CUT_API_TOKEN": "configured",
            "MACOS_CONNECTOR_TIMEOUT": "90",
            "MACOS_COMMAND_TIMEOUT": "900",
        }
    )

    assert any(
        "MACOS_CONNECTOR_TIMEOUT" in item and "MACOS_COMMAND_TIMEOUT" in item for item in warnings
    )


def test_validate_env_accepts_connector_timeout_above_command_timeout() -> None:
    warnings = validate_env(
        {
            "ROUGH_CUT_API_TOKEN": "configured",
            "OPENROUTER_API_KEY": "configured",
            "MACOS_CONNECTOR_TIMEOUT": "1000",
            "MACOS_COMMAND_TIMEOUT": "900",
        }
    )

    assert warnings == []


@pytest.mark.parametrize("value", ["0", "9", "abc"])
def test_validate_env_checks_connector_max_parallel(value: str) -> None:
    warnings = validate_env(
        {
            "ROUGH_CUT_API_TOKEN": "configured",
            "OPENROUTER_API_KEY": "configured",
            "MACOS_CONNECTOR_MAX_PARALLEL": value,
        }
    )

    assert any("MACOS_CONNECTOR_MAX_PARALLEL" in warning for warning in warnings)


def test_validate_env_accepts_valid_connector_max_parallel() -> None:
    warnings = validate_env(
        {
            "ROUGH_CUT_API_TOKEN": "configured",
            "OPENROUTER_API_KEY": "configured",
            "MACOS_CONNECTOR_MAX_PARALLEL": "2",
        }
    )

    assert warnings == []
