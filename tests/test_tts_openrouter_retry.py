from types import SimpleNamespace
from unittest.mock import patch

import pytest

from stage1.gemini_tts import TtsCancelledError, _synthesize_with_retry, synthesize_phrase


@pytest.mark.parametrize("header_location", ["http", "metadata"])
@pytest.mark.parametrize("entrypoint", ["batch", "phrase"])
def test_in_flight_budget_waits_before_retrying(header_location, entrypoint, tmp_path):
    metadata = {"limit_source": "openrouter_in_flight_budget"}
    headers = {"Retry-After": "120"}
    if header_location == "metadata":
        metadata["headers"] = headers
    rejected = SimpleNamespace(
        status_code=402,
        headers=headers if header_location == "http" else {},
        json=lambda: {"error": {"metadata": metadata}},
        close=lambda: None,
    )
    accepted = SimpleNamespace(
        status_code=200, headers={}, content=b"\x00\x01" * 240, close=lambda: None
    )
    settings = SimpleNamespace(
        openrouter_tts_model="microsoft/mai-voice-2",
        openrouter_api_key="test-key",
        openrouter_base_url="https://example.invalid",
        openrouter_tts_sample_rate=24000,
        tts_provider="openrouter",
        tts_gender="female",
    )
    with (
        patch("requests.post", side_effect=[rejected, accepted]),
        patch("stage1.gemini_tts.time.sleep") as sleep,
        patch("stage1.gemini_tts.get_settings", return_value=settings),
    ):
        if entrypoint == "batch":
            audio, attempts, errors, final_error, _ = _synthesize_with_retry(
                "openrouter", "Текст", settings, "voice", 1.0, None, None
            )
            assert attempts == 2
            assert len(errors) == 1
            assert final_error is None
        else:
            result = synthesize_phrase("Текст", tmp_path, voice_name="voice")
            audio = (tmp_path / "phrase_preview.wav").read_bytes()
            assert result["dry_run"] is False
    assert audio.startswith(b"RIFF")
    assert sum(call.args[0] for call in sleep.call_args_list) == 120


@pytest.mark.parametrize(
    ("limit_source", "headers"),
    [
        ("openrouter_credits", {"Retry-After": "120"}),
        ("openrouter_key_limit", {"Retry-After": "120"}),
        ("openrouter_in_flight_budget", {}),
        ("openrouter_in_flight_budget", {"Retry-After": "invalid"}),
    ],
)
def test_non_retryable_402_does_not_wait(limit_source, headers):
    rejected = SimpleNamespace(
        status_code=402,
        headers=headers,
        json=lambda: {"error": {"metadata": {"limit_source": limit_source}}},
        close=lambda: None,
    )
    settings = SimpleNamespace(
        openrouter_tts_model="microsoft/mai-voice-2",
        openrouter_api_key="test-key",
        openrouter_base_url="https://example.invalid",
        openrouter_tts_sample_rate=24000,
    )
    with (
        patch("requests.post", return_value=rejected),
        patch("stage1.gemini_tts.time.sleep") as sleep,
    ):
        audio, attempts, _, final_error, _ = _synthesize_with_retry(
            "openrouter", "Текст", settings, "voice", 1.0, None, None
        )
    assert audio is None
    assert attempts == 1
    assert final_error is not None
    sleep.assert_not_called()


def test_waiting_for_provider_can_be_cancelled():
    error = RuntimeError("temporarily exhausted")
    error.status_code = 402
    error.limit_source = "openrouter_in_flight_budget"
    error.retry_after_seconds = 120
    cancelled = False

    def sleep(_seconds):
        nonlocal cancelled
        cancelled = True

    with (
        patch("stage1.gemini_tts._synthesize_wav_bytes", side_effect=error),
        patch("stage1.gemini_tts.time.sleep", side_effect=sleep),
        pytest.raises(TtsCancelledError),
    ):
        _synthesize_with_retry(
            "openrouter",
            "Текст",
            SimpleNamespace(),
            "voice",
            1.0,
            lambda: cancelled,
            None,
        )
