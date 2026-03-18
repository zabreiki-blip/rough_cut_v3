from dataclasses import dataclass
import os


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
    gemini_api_key: str
    gemini_tts_model: str
    gemini_tts_voice: str
    gemini_tts_voice_female: str
    gemini_tts_voice_male: str
    macos_tts_voice: str
    macos_tts_voice_female: str
    macos_tts_voice_male: str
    macos_tts_rate: int
    macos_tts_sample_rate: int
    macos_connector_url: str
    macos_connector_token: str
    macos_connector_timeout: int
    project_name: str
    max_zk_blocks: int


def get_settings() -> Settings:
    max_blocks_raw = os.getenv("MAX_ZK_BLOCKS", "200").strip()
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

    return Settings(
        tts_provider=os.getenv("TTS_PROVIDER", "gemini").strip().lower(),
        tts_gender=os.getenv("TTS_GENDER", "female").strip().lower(),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
        openrouter_model=os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini").strip(),
        openrouter_base_url=os.getenv(
            "OPENROUTER_BASE_URL",
            "https://openrouter.ai/api/v1",
        ).strip(),
        openrouter_tts_model=os.getenv(
            "OPENROUTER_TTS_MODEL",
            "openai/gpt-4o-audio-preview",
        ).strip(),
        openrouter_tts_voice=os.getenv("OPENROUTER_TTS_VOICE", "alloy").strip(),
        openrouter_tts_voice_female=os.getenv(
            "OPENROUTER_TTS_VOICE_FEMALE",
            "shimmer",
        ).strip(),
        openrouter_tts_voice_male=os.getenv(
            "OPENROUTER_TTS_VOICE_MALE",
            "onyx",
        ).strip(),
        openrouter_tts_sample_rate=sample_rate,
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        gemini_tts_model=os.getenv(
            "GEMINI_TTS_MODEL",
            "gemini-2.5-pro-preview-tts",
        ).strip(),
        gemini_tts_voice=os.getenv("GEMINI_TTS_VOICE", "Kore").strip(),
        gemini_tts_voice_female=os.getenv("GEMINI_TTS_VOICE_FEMALE", "Sharon").strip(),
        gemini_tts_voice_male=os.getenv("GEMINI_TTS_VOICE_MALE", "Kore").strip(),
        macos_tts_voice=os.getenv("MACOS_TTS_VOICE", "Milena").strip(),
        macos_tts_voice_female=os.getenv("MACOS_TTS_VOICE_FEMALE", "Milena").strip(),
        macos_tts_voice_male=os.getenv("MACOS_TTS_VOICE_MALE", "Yuri").strip(),
        macos_tts_rate=macos_rate,
        macos_tts_sample_rate=macos_sr,
        macos_connector_url=os.getenv("MACOS_CONNECTOR_URL", "").strip(),
        macos_connector_token=os.getenv("MACOS_CONNECTOR_TOKEN", "").strip(),
        macos_connector_timeout=macos_connector_timeout,
        project_name=os.getenv("PROJECT_NAME", "rough_cut_v3").strip(),
        max_zk_blocks=max_blocks,
    )
