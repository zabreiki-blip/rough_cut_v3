from __future__ import annotations

import base64
from datetime import datetime, UTC
from pathlib import Path
import re
import io
import time
import wave
import json as pyjson
import shutil
import subprocess
import tempfile
from typing import Callable

from .config import get_settings
from .io_utils import read_json


def _apply_tempo(wav_bytes: bytes, speed: float) -> bytes:
    """Stretch/compress WAV tempo via ffmpeg atempo filter. speed=1.0 = no-op."""
    speed = round(max(0.5, min(2.0, speed)), 2)
    if speed == 1.0:
        return wav_bytes
    if shutil.which("ffmpeg") is None:
        return wav_bytes  # ffmpeg not available, skip silently
    # atempo supports 0.5–2.0; chain filters for values outside that range
    if 0.5 <= speed <= 2.0:
        atempo = f"atempo={speed}"
    elif speed < 0.5:
        atempo = f"atempo=0.5,atempo={round(speed/0.5, 4)}"
    else:
        atempo = f"atempo=2.0,atempo={round(speed/2.0, 4)}"
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.wav"
        dst = Path(td) / "out.wav"
        src.write_bytes(wav_bytes)
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-filter:a", atempo, str(dst)],
            capture_output=True,
        )
        if result.returncode != 0 or not dst.exists():
            return wav_bytes  # fallback to original on error
        return dst.read_bytes()


def _provider_unavailable_message(provider: str, settings=None) -> str:
    if provider == "macos":
        connector = ""
        if settings and settings.macos_connector_url:
            connector = (
                f" Connector configured ({settings.macos_connector_url}) but request failed."
            )
        return (
            "macOS TTS unavailable: command 'say' is not found in backend environment. "
            "If API runs in Docker/Linux, this is expected. "
            "Run API locally on macOS or set MACOS_CONNECTOR_URL to a macOS connector service."
            + connector
        )
    return f"TTS provider '{provider}' is unavailable in this environment."


def _extract_audio_payload(response: object) -> tuple[bytes, str]:
    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        if not content:
            continue
        parts = getattr(content, "parts", None) or []
        for part in parts:
            inline_data = getattr(part, "inline_data", None)
            if not inline_data:
                continue
            mime_type = str(getattr(inline_data, "mime_type", "") or "").strip()
            data = getattr(inline_data, "data", None)
            if isinstance(data, bytes):
                return data, mime_type
            if isinstance(data, str):
                try:
                    return base64.b64decode(data), mime_type
                except Exception:
                    return data.encode("utf-8"), mime_type
    raise RuntimeError("Gemini TTS response does not contain audio bytes")


def _build_wav_from_pcm(
    pcm_bytes: bytes,
    sample_rate: int = 24000,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def _ensure_wav_bytes(audio_bytes: bytes, mime_type: str) -> bytes:
    if audio_bytes.startswith(b"RIFF"):
        return audio_bytes

    sample_rate = 24000
    channels = 1
    sample_width = 2

    if mime_type:
        rate_match = re.search(r"rate=(\d+)", mime_type, flags=re.I)
        channels_match = re.search(r"channels=(\d+)", mime_type, flags=re.I)
        if rate_match:
            sample_rate = int(rate_match.group(1))
        if channels_match:
            channels = int(channels_match.group(1))

        # Typical Gemini raw payload: audio/L16;rate=24000
        if "l16" not in mime_type.lower() and "pcm" not in mime_type.lower():
            return audio_bytes

    return _build_wav_from_pcm(
        pcm_bytes=audio_bytes,
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
    )


def _write_silence_wav(path: Path, duration_ms: int = 1500) -> None:
    sample_rate = 24000
    n_channels = 1
    sample_width = 2
    frames = int(sample_rate * (duration_ms / 1000.0))
    silence = b"\x00\x00" * frames

    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(n_channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(silence)


def _is_valid_wav(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 44:
        return False
    try:
        with wave.open(str(path), "rb"):
            return True
    except Exception:
        return False


def _synthesize_openrouter_wav(text: str, settings, voice: str) -> bytes:
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("requests is not installed. Install requirements.txt.") from exc

    payload = {
        "model": settings.openrouter_tts_model,
        "messages": [
            {
                "role": "user",
                "content": (
                    "Прочитай вслух без добавлений и комментариев следующий текст:\n\n"
                    + text
                ),
            }
        ],
        "modalities": ["text", "audio"],
        "audio": {"voice": voice, "format": "pcm16"},
        "stream": True,
        "max_tokens": 512,
    }
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    response = requests.post(
        f"{settings.openrouter_base_url}/chat/completions",
        headers=headers,
        json=payload,
        timeout=240,
        stream=True,
    )
    if response.status_code != 200:
        try:
            err = response.json()
        except Exception:
            err = {"status": response.status_code, "body": response.text[:500]}
        raise RuntimeError(f"OpenRouter TTS failed: {err}")

    audio_chunks: list[bytes] = []
    for raw in response.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith("data: "):
            continue
        data = raw[6:].strip()
        if data == "[DONE]":
            break
        try:
            event = pyjson.loads(data)
        except Exception:
            continue
        choice = (event.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        audio = delta.get("audio")
        if isinstance(audio, dict) and audio.get("data"):
            audio_chunks.append(base64.b64decode(audio["data"]))

    if not audio_chunks:
        raise RuntimeError("OpenRouter stream returned no audio chunks")

    pcm_bytes = b"".join(audio_chunks)
    return _build_wav_from_pcm(
        pcm_bytes=pcm_bytes,
        sample_rate=settings.openrouter_tts_sample_rate,
        channels=1,
        sample_width=2,
    )


def _synthesize_gemini_wav(text: str, settings, voice: str, client, types) -> bytes:
    response = client.models.generate_content(
        model=settings.gemini_tts_model,
        contents=text,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
                )
            ),
        ),
    )
    audio_bytes, mime_type = _extract_audio_payload(response)
    return _ensure_wav_bytes(audio_bytes, mime_type)


def _list_macos_voices() -> list[str]:
    if shutil.which("say") is None:
        return []
    try:
        proc = subprocess.run(
            ["say", "-v", "?"],
            capture_output=True,
            text=True,
            check=True,
        )
    except Exception:
        return []
    voices: list[str] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        name = line.split(maxsplit=1)[0]
        if name:
            voices.append(name)
    return voices


def _resolve_macos_voice(voice: str) -> str:
    voices = _list_macos_voices()
    if not voices:
        return voice or "Alex"
    if voice in voices:
        return voice
    for fallback in ("Milena", "Yuri", "Samantha", "Alex"):
        if fallback in voices:
            return fallback
    return voices[0]


def _synthesize_macos_wav(text: str, settings, voice: str) -> bytes:
    if shutil.which("say") is None:
        raise RuntimeError("macOS command 'say' is not found.")
    if shutil.which("afconvert") is None:
        raise RuntimeError("macOS command 'afconvert' is not found.")

    voice = _resolve_macos_voice(voice)
    with tempfile.TemporaryDirectory() as td:
        src_aiff = Path(td) / "tts.aiff"
        out_wav = Path(td) / "tts.wav"

        say_cmd = [
            "say",
            "-v",
            voice,
            "-r",
            str(settings.macos_tts_rate),
            "-o",
            str(src_aiff),
            text,
        ]
        try:
            subprocess.run(say_cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            # Fallback to system default voice when the selected one is unavailable.
            fallback_cmd = [
                "say",
                "-r",
                str(settings.macos_tts_rate),
                "-o",
                str(src_aiff),
                text,
            ]
            try:
                subprocess.run(fallback_cmd, check=True, capture_output=True, text=True)
            except subprocess.CalledProcessError as fallback_exc:
                details = (fallback_exc.stderr or fallback_exc.stdout or "").strip()
                if not details:
                    details = (exc.stderr or exc.stdout or "").strip()
                if not details:
                    details = "unknown say error"
                raise RuntimeError(f"macOS 'say' failed: {details}") from fallback_exc

        af_cmd = [
            "afconvert",
            "-f",
            "WAVE",
            "-d",
            f"LEI16@{settings.macos_tts_sample_rate}",
            str(src_aiff),
            str(out_wav),
        ]
        try:
            subprocess.run(af_cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            details = (exc.stderr or exc.stdout or "").strip() or "unknown afconvert error"
            raise RuntimeError(f"macOS 'afconvert' failed: {details}") from exc
        return out_wav.read_bytes()


def _synthesize_macos_connector_wav(text: str, settings, voice: str) -> bytes:
    if not settings.macos_connector_url:
        raise RuntimeError("MACOS_CONNECTOR_URL is not set.")
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("requests is not installed. Install requirements.txt.") from exc

    headers = {"Content-Type": "application/json"}
    if settings.macos_connector_token:
        headers["Authorization"] = f"Bearer {settings.macos_connector_token}"

    payload = {
        "text": text,
        "voice": voice,
        "rate": settings.macos_tts_rate,
        "sample_rate": settings.macos_tts_sample_rate,
    }

    try:
        response = requests.post(
            settings.macos_connector_url,
            json=payload,
            headers=headers,
            timeout=settings.macos_connector_timeout,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"macOS connector request failed: {exc}") from exc

    if response.status_code != 200:
        details = response.text[:400].strip()
        raise RuntimeError(
            f"macOS connector failed ({response.status_code}): {details or 'empty response'}"
        )
    if not response.content:
        raise RuntimeError("macOS connector returned empty body")
    if not response.content.startswith(b"RIFF"):
        raise RuntimeError("macOS connector returned non-WAV data")
    return response.content


def _is_retryable_tts_error(provider: str, exc: Exception) -> bool:
    msg = str(exc).lower()
    if provider == "macos":
        # Local macOS `say` errors should fail fast, but transient connector/network
        # errors can be retried a few times.
        transient_markers = (
            "connector request failed",
            "timed out",
            "timeout",
            "connection reset",
            "connection aborted",
            "name or service not known",
            "temporary failure",
            "(502)",
            "(503)",
            "(504)",
            "bad gateway",
            "service unavailable",
            "gateway timeout",
        )
        return any(marker in msg for marker in transient_markers)
    if "api_key is not set" in msg:
        return False
    if "not found in backend environment" in msg:
        return False
    return True


def _resolve_gender(gender: str | None, default_gender: str) -> str:
    g = (gender or default_gender or "female").strip().lower()
    if g not in {"female", "male"}:
        return "female"
    return g


def _pick_voice(settings, provider: str, voice_name: str | None, gender: str | None) -> tuple[str, str]:
    if voice_name and voice_name.strip():
        resolved_gender = _resolve_gender(gender, settings.tts_gender)
        return voice_name.strip(), resolved_gender

    resolved_gender = _resolve_gender(gender, settings.tts_gender)
    if provider == "openrouter":
        if resolved_gender == "male":
            voice = settings.openrouter_tts_voice_male or settings.openrouter_tts_voice
        else:
            voice = settings.openrouter_tts_voice_female or settings.openrouter_tts_voice
    elif provider == "macos":
        if resolved_gender == "male":
            voice = settings.macos_tts_voice_male or settings.macos_tts_voice
        else:
            voice = settings.macos_tts_voice_female or settings.macos_tts_voice
    else:
        if resolved_gender == "male":
            voice = settings.gemini_tts_voice_male or settings.gemini_tts_voice
        else:
            voice = settings.gemini_tts_voice_female or settings.gemini_tts_voice
    return voice, resolved_gender


def _normalize_provider(raw: str | None, default_provider: str) -> str:
    provider = (raw or default_provider or "gemini").strip().lower()
    if provider not in {"openrouter", "gemini", "macos"}:
        return "gemini"
    return provider


def _build_tts_client(provider: str, settings):
    client = None
    types = None
    if provider == "gemini":
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise RuntimeError(
                "google-genai is not installed. Install dependencies from requirements.txt."
            ) from exc
        client = genai.Client(api_key=settings.gemini_api_key)
    return client, types


def _synthesize_wav_bytes(provider: str, text: str, settings, voice: str, client, types) -> bytes:
    if provider == "openrouter":
        return _synthesize_openrouter_wav(text, settings, voice)
    if provider == "macos":
        if shutil.which("say") is None:
            return _synthesize_macos_connector_wav(text, settings, voice)
        return _synthesize_macos_wav(text, settings, voice)
    return _synthesize_gemini_wav(
        text=text,
        settings=settings,
        voice=voice,
        client=client,
        types=types,
    )


def _safe_file_stem(name: str | None, fallback: str) -> str:
    raw = (name or "").strip()
    if not raw:
        return fallback
    stem = Path(raw).stem
    stem = re.sub(r"[^a-zA-Z0-9_-]+", "_", stem).strip("_")
    return stem[:80] if stem else fallback


def _emit_progress(
    progress_callback: Callable[[dict], None] | None,
    payload: dict,
) -> None:
    if progress_callback is None:
        return
    try:
        progress_callback(payload)
    except Exception:
        # Progress reporting must never break synthesis.
        return


def synthesize_tts(
    prepared_path: Path,
    speaker_dir: Path,
    provider: str | None = None,
    voice_name: str | None = None,
    gender: str | None = None,
    dry_run: bool = False,
    progress_callback: Callable[[dict], None] | None = None,
    speed: float = 1.0,
) -> dict:
    payload = read_json(prepared_path)
    items = payload.get("items", [])
    if not isinstance(items, list) or not items:
        raise ValueError("Prepared scenario has no items")

    settings = get_settings()
    provider = _normalize_provider(provider, settings.tts_provider)
    voice, resolved_gender = _pick_voice(settings, provider, voice_name, gender)
    speaker_dir.mkdir(parents=True, exist_ok=True)

    generated: list[str] = []
    skipped: list[str] = []
    used_dry_run = dry_run

    if not dry_run and provider == "gemini" and not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is not set. Use --dry-run for placeholders.")
    if not dry_run and provider == "openrouter" and not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set. Use --dry-run for placeholders.")
    if (
        not dry_run
        and provider == "macos"
        and shutil.which("say") is None
        and not settings.macos_connector_url
    ):
        raise RuntimeError(_provider_unavailable_message(provider, settings))

    client = None
    types = None
    if not dry_run:
        client, types = _build_tts_client(provider, settings)

    prepared_items: list[tuple[str, str]] = []
    for item in items:
        block_id = str(item.get("id", "")).strip()
        text = str(item.get("text_for_tts", "")).strip()
        if not block_id or not text:
            continue
        prepared_items.append((block_id, text))

    total_items = len(prepared_items)
    _emit_progress(
        progress_callback,
        {
            "event": "started",
            "total": total_items,
            "completed": 0,
            "generated": 0,
            "skipped": 0,
        },
    )

    for index, (block_id, text) in enumerate(prepared_items, start=1):
        out_file = speaker_dir / f"{block_id}.wav"
        _emit_progress(
            progress_callback,
            {
                "event": "item_started",
                "id": block_id,
                "index": index,
                "total": total_items,
                "completed": index - 1,
                "generated": len(generated),
                "skipped": len(skipped),
            },
        )
        if not dry_run and _is_valid_wav(out_file):
            skipped.append(str(out_file))
            _emit_progress(
                progress_callback,
                {
                    "event": "item_skipped",
                    "id": block_id,
                    "index": index,
                    "total": total_items,
                    "completed": index,
                    "generated": len(generated),
                    "skipped": len(skipped),
                },
            )
            continue

        if dry_run:
            est_duration_ms = max(900, int(len(text) * 55))
            _write_silence_wav(out_file, duration_ms=est_duration_ms)
            generated.append(str(out_file))
            _emit_progress(
                progress_callback,
                {
                    "event": "item_generated",
                    "id": block_id,
                    "index": index,
                    "total": total_items,
                    "completed": index,
                    "generated": len(generated),
                    "skipped": len(skipped),
                    "attempts": 1,
                },
            )
            continue

        attempts = 0
        while True:
            attempts += 1
            try:
                wav_bytes = _synthesize_wav_bytes(provider, text, settings, voice, client, types)
                wav_bytes = _apply_tempo(wav_bytes, speed)
                out_file.write_bytes(wav_bytes)
                generated.append(str(out_file))
                _emit_progress(
                    progress_callback,
                    {
                        "event": "item_generated",
                        "id": block_id,
                        "index": index,
                        "total": total_items,
                        "completed": index,
                        "generated": len(generated),
                        "skipped": len(skipped),
                        "attempts": attempts,
                    },
                )
                break
            except Exception as exc:
                retryable = attempts < 4 and _is_retryable_tts_error(provider, exc)
                _emit_progress(
                    progress_callback,
                    {
                        "event": "item_retrying" if retryable else "item_failed",
                        "id": block_id,
                        "index": index,
                        "total": total_items,
                        "completed": index - 1,
                        "generated": len(generated),
                        "skipped": len(skipped),
                        "attempts": attempts,
                        "error": str(exc),
                    },
                )
                if not retryable:
                    raise
                time.sleep(min(60, 8 * attempts))

    result = {
        "prepared_path": str(prepared_path),
        "speaker_dir": str(speaker_dir),
        "items_total": total_items,
        "files_count": len(generated),
        "skipped_count": len(skipped),
        "provider": provider,
        "voice": voice,
        "gender": resolved_gender,
        "dry_run": used_dry_run,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    _emit_progress(
        progress_callback,
        {
            "event": "completed",
            "total": total_items,
            "completed": total_items,
            "generated": len(generated),
            "skipped": len(skipped),
        },
    )
    return result


def synthesize_phrase(
    text: str,
    speaker_dir: Path,
    provider: str | None = None,
    voice_name: str | None = None,
    gender: str | None = None,
    file_name: str | None = None,
    dry_run: bool = False,
    speed: float = 1.0,
) -> dict:
    phrase = (text or "").strip()
    if not phrase:
        raise ValueError("text is required for phrase synthesis")

    settings = get_settings()
    provider = _normalize_provider(provider, settings.tts_provider)
    voice, resolved_gender = _pick_voice(settings, provider, voice_name, gender)
    speaker_dir.mkdir(parents=True, exist_ok=True)

    if not dry_run and provider == "gemini" and not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is not set. Use --dry-run for placeholders.")
    if not dry_run and provider == "openrouter" and not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set. Use --dry-run for placeholders.")
    if (
        not dry_run
        and provider == "macos"
        and shutil.which("say") is None
        and not settings.macos_connector_url
    ):
        raise RuntimeError(_provider_unavailable_message(provider, settings))

    stem = _safe_file_stem(file_name, "phrase_preview")
    out_file = speaker_dir / f"{stem}.wav"

    if dry_run:
        est_duration_ms = max(900, int(len(phrase) * 55))
        _write_silence_wav(out_file, duration_ms=est_duration_ms)
        return {
            "speaker_dir": str(speaker_dir),
            "file": str(out_file),
            "provider": provider,
            "voice": voice,
            "gender": resolved_gender,
            "dry_run": True,
            "text_chars": len(phrase),
            "generated_at": datetime.now(UTC).isoformat(),
        }

    client, types = _build_tts_client(provider, settings)
    attempts = 0
    while True:
        attempts += 1
        try:
            wav_bytes = _synthesize_wav_bytes(provider, phrase, settings, voice, client, types)
            wav_bytes = _apply_tempo(wav_bytes, speed)
            out_file.write_bytes(wav_bytes)
            break
        except Exception as exc:
            if attempts >= 4 or not _is_retryable_tts_error(provider, exc):
                raise
            time.sleep(min(60, 8 * attempts))

    return {
        "speaker_dir": str(speaker_dir),
        "file": str(out_file),
        "provider": provider,
        "voice": voice,
        "gender": resolved_gender,
        "dry_run": False,
        "text_chars": len(phrase),
        "generated_at": datetime.now(UTC).isoformat(),
    }
