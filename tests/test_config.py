from __future__ import annotations

from stage1.config import validate_env


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
