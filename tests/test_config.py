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


def test_validate_env_rejects_invalid_final_review_cost_guard() -> None:
    secret = "private-cost-input"
    warnings = validate_env(
        {
            "FINAL_REVIEW_TIMEOUT_SECONDS": "not-a-deadline",
            "ROUGH_CUT_MAX_USD_PER_JOB": "-2.50",
            "ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE": secret,
        }
    )

    rendered = "\n".join(warnings)
    assert "FINAL_REVIEW_TIMEOUT_SECONDS" in rendered
    assert "ROUGH_CUT_MAX_USD_PER_JOB" in rendered
    assert "ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE" in rendered
    assert secret not in rendered

    incomplete_guard = validate_env(
        {
            "TTS_PROVIDER": "macos",
            "ROUGH_CUT_API_TOKEN": "configured",
            "ROUGH_CUT_MAX_USD_PER_JOB": "1.25",
            "ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE": "0",
        }
    )
    assert any(
        "ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE" in warning and "positive" in warning
        for warning in incomplete_guard
    )


def test_validate_env_accepts_disabled_or_complete_cost_guard() -> None:
    assert (
        validate_env(
            {
                "FINAL_REVIEW_TIMEOUT_SECONDS": "7200",
                "ROUGH_CUT_MAX_USD_PER_JOB": "0",
                "ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE": "0",
                "TTS_PROVIDER": "macos",
                "ROUGH_CUT_API_TOKEN": "configured",
            }
        )
        == []
    )
    assert (
        validate_env(
            {
                "FINAL_REVIEW_TIMEOUT_SECONDS": "300",
                "ROUGH_CUT_MAX_USD_PER_JOB": "2.75",
                "ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE": "0.04",
                "TTS_PROVIDER": "macos",
                "ROUGH_CUT_API_TOKEN": "configured",
            }
        )
        == []
    )


def test_validate_env_rejects_nonfinite_costs_and_cost_guard_boundaries() -> None:
    for raw in ("NaN", "Infinity", "-Infinity"):
        warnings = validate_env(
            {
                "TTS_PROVIDER": "macos",
                "ROUGH_CUT_API_TOKEN": "configured",
                "ROUGH_CUT_MAX_USD_PER_JOB": raw,
                "ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE": raw,
            }
        )
        rendered = "\n".join(warnings)
        assert "ROUGH_CUT_MAX_USD_PER_JOB" in rendered
        assert "ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE" in rendered
        assert raw not in rendered

    nonpositive_timeout = validate_env(
        {
            "TTS_PROVIDER": "macos",
            "ROUGH_CUT_API_TOKEN": "configured",
            "FINAL_REVIEW_TIMEOUT_SECONDS": "0",
        }
    )
    assert any("FINAL_REVIEW_TIMEOUT_SECONDS" in warning for warning in nonpositive_timeout)

    missing_estimate = validate_env(
        {
            "TTS_PROVIDER": "macos",
            "ROUGH_CUT_API_TOKEN": "configured",
            "ROUGH_CUT_MAX_USD_PER_JOB": "1.25",
        }
    )
    assert any(
        "ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE" in warning and "positive" in warning
        for warning in missing_estimate
    )
