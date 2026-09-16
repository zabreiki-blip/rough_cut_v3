from dataclasses import dataclass
import os
from collections.abc import Mapping


@dataclass
class Settings:
    tts_provider: str
    tts_gender: str
    openrouter_api_key: str
    openrouter_model: str
    openrouter_base_url: str
    openrouter_tts_model: str
    openrouter_tts_voice: str
    openrouter_tts_voice_female: str
    openrouter_tts_voice_male: str
    openrouter_tts_sample_rate: int
    openrouter_gemini_tts_voice: str
    openrouter_gemini_tts_voice_female: str
    openrouter_gemini_tts_voice_male: str
    macos_tts_voice: str
    macos_tts_voice_female: str
    macos_tts_voice_male: str
    macos_tts_rate: int
    macos_tts_sample_rate: int
    macos_connector_url: str
    macos_connector_token: str
    macos_connector_timeout: int
    macos_command_timeout: int
    project_name: str
    max_zk_blocks: int


def validate_env(environ: Mapping[str, str] | None = None) -> list[str]:
    """Return actionable configuration warnings without exposing secret values."""
    env = os.environ if environ is None else environ
    warnings: list[str] = []

    def integer(name: str, minimum: int, maximum: int | None = None) -> None:
        raw = str(env.get(name, "")).strip()
        if not raw:
            return
        try:
            value = int(raw)
        except ValueError:
            warnings.append(f"{name}={raw!r} is not an integer; the default will be used")
            return
        if value < minimum or (maximum is not None and value > maximum):
            range_text = f"{minimum}..{maximum}" if maximum is not None else f">={minimum}"
            warnings.append(f"{name} must be {range_text}; the value will be adjusted")

    provider = str(env.get("TTS_PROVIDER", "openrouter")).strip().lower()
    if provider not in {"openrouter", "macos"}:
        warnings.append("TTS_PROVIDER must be openrouter or macos")
    gender = str(env.get("TTS_GENDER", "female")).strip().lower()
    if gender not in {"female", "male"}:
        warnings.append("TTS_GENDER must be female or male")
    if provider == "openrouter" and not str(env.get("OPENROUTER_API_KEY", "")).strip():
        warnings.append("OPENROUTER_API_KEY is empty; OpenRouter analysis/TTS is unavailable")
    if not str(env.get("ROUGH_CUT_API_TOKEN", "")).strip():
        warnings.append("ROUGH_CUT_API_TOKEN is empty; authenticated API routes will return 503")
    connector_url = str(env.get("MACOS_CONNECTOR_URL", "")).strip()
    if connector_url and not connector_url.startswith(("http://", "https://")):
        warnings.append("MACOS_CONNECTOR_URL must start with http:// or https://")

    integer("MAX_ZK_BLOCKS", 1)
    integer("OPENROUTER_TTS_SAMPLE_RATE", 8000, 192000)
    integer("MACOS_TTS_RATE", 80, 420)
    integer("MACOS_TTS_SAMPLE_RATE", 8000, 192000)
    integer("MACOS_CONNECTOR_TIMEOUT", 5)
    integer("MACOS_COMMAND_TIMEOUT", 30, 7200)
    integer("SYNC_WHISPER_TIMEOUT_SECONDS", 1)
    integer("SYNC_AUDIO_MAX_BYTES", 1)
    return warnings


def get_settings() -> Settings:
    max_blocks_raw = os.getenv("MAX_ZK_BLOCKS", "40").strip()
    try:
        max_blocks = int(max_blocks_raw)
    except ValueError:
        max_blocks = 40
    if max_blocks < 1:
        max_blocks = 40

    sample_rate_raw = os.getenv("OPENROUTER_TTS_SAMPLE_RATE", "24000").strip()
    try:
        sample_rate = int(sample_rate_raw)
    except ValueError:
        sample_rate = 24000
    if sample_rate < 8000:
        sample_rate = 24000

    macos_rate_raw = os.getenv("MACOS_TTS_RATE", "175").strip()
    try:
        macos_rate = int(macos_rate_raw)
    except ValueError:
        macos_rate = 175
    if macos_rate < 80:
        macos_rate = 80
    if macos_rate > 420:
        macos_rate = 420

    macos_sr_raw = os.getenv("MACOS_TTS_SAMPLE_RATE", "24000").strip()
    try:
        macos_sr = int(macos_sr_raw)
    except ValueError:
        macos_sr = 24000
    if macos_sr < 8000:
        macos_sr = 24000

    macos_connector_timeout_raw = os.getenv("MACOS_CONNECTOR_TIMEOUT", "90").strip()
    try:
        macos_connector_timeout = int(macos_connector_timeout_raw)
    except ValueError:
        macos_connector_timeout = 90
    if macos_connector_timeout < 5:
        macos_connector_timeout = 5

    macos_command_timeout_raw = os.getenv("MACOS_COMMAND_TIMEOUT", "900").strip()
    try:
        macos_command_timeout = int(macos_command_timeout_raw)
    except ValueError:
        macos_command_timeout = 900
    if macos_command_timeout < 30:
        macos_command_timeout = 30

    return Settings(
        tts_provider=os.getenv("TTS_PROVIDER", "openrouter").strip().lower(),
        tts_gender=os.getenv("TTS_GENDER", "female").strip().lower(),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
        openrouter_model=os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini").strip(),
        openrouter_base_url=os.getenv(
            "OPENROUTER_BASE_URL",
            "https://openrouter.ai/api/v1",
        ).strip(),
        openrouter_tts_model=os.getenv(
            "OPENROUTER_TTS_MODEL",
            "microsoft/mai-voice-2",
        ).strip(),
        openrouter_tts_voice=os.getenv("OPENROUTER_TTS_VOICE", "ru-RU-Masha:MAI-Voice-2").strip(),
        openrouter_tts_voice_female=os.getenv(
            "OPENROUTER_TTS_VOICE_FEMALE",
            "ru-RU-Masha:MAI-Voice-2",
        ).strip(),
        openrouter_tts_voice_male=os.getenv(
            "OPENROUTER_TTS_VOICE_MALE",
            "ru-RU-Lev:MAI-Voice-2",
        ).strip(),
        openrouter_tts_sample_rate=sample_rate,
        openrouter_gemini_tts_voice=os.getenv("OPENROUTER_GEMINI_TTS_VOICE", "Aoede").strip(),
        openrouter_gemini_tts_voice_female=os.getenv(
            "OPENROUTER_GEMINI_TTS_VOICE_FEMALE", "Aoede"
        ).strip(),
        openrouter_gemini_tts_voice_male=os.getenv(
            "OPENROUTER_GEMINI_TTS_VOICE_MALE", "Charon"
        ).strip(),
        macos_tts_voice=os.getenv("MACOS_TTS_VOICE", "Milena").strip(),
        macos_tts_voice_female=os.getenv("MACOS_TTS_VOICE_FEMALE", "Milena").strip(),
        macos_tts_voice_male=os.getenv("MACOS_TTS_VOICE_MALE", "Yuri").strip(),
        macos_tts_rate=macos_rate,
        macos_tts_sample_rate=macos_sr,
        macos_connector_url=os.getenv("MACOS_CONNECTOR_URL", "").strip(),
        macos_connector_token=os.getenv("MACOS_CONNECTOR_TOKEN", "").strip(),
        macos_connector_timeout=macos_connector_timeout,
        macos_command_timeout=macos_command_timeout,
        project_name=os.getenv("PROJECT_NAME", "rough_cut_v3").strip(),
        max_zk_blocks=max_blocks,
    )
