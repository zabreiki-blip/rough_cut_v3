from __future__ import annotations

import base64
from datetime import datetime, UTC
import hashlib
from functools import lru_cache
import logging
from pathlib import Path
import re
import io
import time
import wave
import json as pyjson
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

from env_utils import env_int

from .config import get_settings
from .io_utils import read_json
from .macos_tts_backend import (
    list_macos_voices,
    resolve_macos_voice,
    run_say_and_convert,
)

logger = logging.getLogger(__name__)


def _apply_tempo(wav_bytes: bytes, speed: float) -> bytes:
    """Stretch/compress WAV tempo via ffmpeg atempo filter. speed=1.0 = no-op."""
    speed = round(max(0.5, min(2.0, speed)), 2)
    if speed == 1.0:
        return wav_bytes
    if shutil.which("ffmpeg") is None:
        logger.warning("ffmpeg is unavailable; requested TTS speed %.2f was not applied", speed)
        return wav_bytes
    # atempo supports 0.5–2.0; chain filters for values outside that range
    if 0.5 <= speed <= 2.0:
        atempo = f"atempo={speed}"
    elif speed < 0.5:
        atempo = f"atempo=0.5,atempo={round(speed / 0.5, 4)}"
    else:
        atempo = f"atempo=2.0,atempo={round(speed / 2.0, 4)}"
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.wav"
        dst = Path(td) / "out.wav"
        src.write_bytes(wav_bytes)
        try:
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", str(src), "-filter:a", atempo, str(dst)],
                capture_output=True,
                timeout=env_int(
                    "FFMPEG_TIMEOUT_SECONDS",
                    180,
                    minimum=30,
                    maximum=3600,
                ),
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "ffmpeg timed out; requested TTS speed %.2f was not applied",
                speed,
            )
            return wav_bytes
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


def _atomic_write_bytes(path: Path, data: bytes, *, validate_wav: bool = False) -> None:
    """Publish bytes atomically, preserving the old target if publication fails."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(data)
            temp_file.flush()
            os.fsync(temp_file.fileno())

        if validate_wav and not _is_valid_wav(temp_path):
            raise ValueError(f"Generated audio is not a valid WAV: {path.name}")
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _write_silence_wav(path: Path, duration_ms: int = 1500) -> None:
    sample_rate = 24000
    n_channels = 1
    sample_width = 2
    frames = int(sample_rate * (duration_ms / 1000.0))
    silence = b"\x00\x00" * frames

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(n_channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(silence)
    _atomic_write_bytes(path, buf.getvalue(), validate_wav=True)


def _is_valid_wav(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 44:
        return False
    try:
        with wave.open(str(path), "rb"):
            return True
    except Exception:
        return False


def _synthesis_fingerprint(
    text: str,
    provider: str,
    voice: str,
    gender: str,
    speed: float,
    model: str = "",
    sample_rate: int = 0,
    extra_rate: int = 0,
) -> str:
    payload = f"{text}\n{provider}\n{voice}\n{gender}\n{round(speed, 4)}\n{model}\n{sample_rate}\n{extra_rate}"
    return hashlib.sha256(payload.encode()).hexdigest()


def _meta_path(wav_path: Path) -> Path:
    return wav_path.with_suffix(".wav.meta")


def _read_meta_fingerprint(wav_path: Path) -> str | None:
    p = _meta_path(wav_path)
    try:
        data = pyjson.loads(p.read_text(encoding="utf-8"))
        return data.get("fingerprint")
    except Exception:
        return None


def _write_meta_fingerprint(wav_path: Path, fingerprint: str) -> None:
    _atomic_write_bytes(
        _meta_path(wav_path),
        pyjson.dumps({"fingerprint": fingerprint}).encode("utf-8"),
    )


def _synthesize_openrouter_wav(
    text: str,
    settings,
    voice: str,
    usage_callback: Callable[[dict[str, Any]], None] | None = None,
) -> bytes:
    """Synthesize through OpenRouter's dedicated OpenAI-compatible TTS endpoint."""
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("requests is not installed. Install requirements.txt.") from exc

    payload = {
        "model": settings.openrouter_tts_model,
        "input": text,
        "voice": voice,
        # MAI-Voice-2 emits 24 kHz mono PCM through this endpoint. Keep the
        # downstream project contract as WAV by wrapping it below.
        "response_format": "pcm",
    }
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
        "Accept": "audio/pcm",
    }

    response = requests.post(
        f"{settings.openrouter_base_url}/audio/speech",
        headers=headers,
        json=payload,
        timeout=240,
    )
    try:
        if response.status_code != 200:
            try:
                err = response.json()
            except Exception:
                err = {"status": response.status_code, "body": response.text[:500]}
            error = RuntimeError(f"OpenRouter TTS failed ({response.status_code}): {err}")
            error.status_code = response.status_code  # type: ignore[attr-defined]
            raise error

        audio_bytes = response.content
        response_headers = getattr(response, "headers", {})
        generation_id = str(response_headers.get("X-Generation-Id") or "").strip()
        if usage_callback is not None and generation_id:
            usage_callback(
                {
                    "generation_id": generation_id,
                    "model": settings.openrouter_tts_model,
                    "pending": True,
                    "base_url": settings.openrouter_base_url,
                }
            )
    finally:
        response.close()

    if not audio_bytes:
        raise RuntimeError("OpenRouter TTS returned an empty audio response")

    return _build_wav_from_pcm(
        pcm_bytes=audio_bytes,
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


@lru_cache(maxsize=4)
def _list_macos_voices(timeout: int = 15) -> tuple[str, ...]:
    return list_macos_voices(
        timeout=timeout,
        run=subprocess.run,
        which=shutil.which,
        logger=logger,
    )


def _resolve_macos_voice(voice: str, timeout: int = 15) -> str:
    return resolve_macos_voice(voice, _list_macos_voices(timeout=timeout))


def _synthesize_macos_wav(text: str, settings, voice: str) -> bytes:
    if shutil.which("say") is None:
        raise RuntimeError("macOS command 'say' is not found.")
    if shutil.which("afconvert") is None:
        raise RuntimeError("macOS command 'afconvert' is not found.")

    command_timeout = int(getattr(settings, "macos_command_timeout", 900))
    voice = _resolve_macos_voice(voice, timeout=min(30, command_timeout))
    return run_say_and_convert(
        text,
        voice=voice,
        rate=settings.macos_tts_rate,
        sample_rate=settings.macos_tts_sample_rate,
        timeout=command_timeout,
        retry_with_default_voice=True,
        run=subprocess.run,
    )


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
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status_code, int):
        return status_code == 429 or 500 <= status_code < 600

    status_match = re.search(r"(?:status(?:_code)?[=:\s]+|failed\s*\()([45]\d\d)", msg)
    if status_match:
        status = int(status_match.group(1))
        return status == 429 or 500 <= status < 600

    permanent_markers = (
        "invalid argument",
        "invalid_argument",
        "bad request",
        "permission denied",
        "permission_denied",
        "unauthenticated",
        "api_key is not set",
        "safety",
        "blocked",
        "prohibited content",
        "policy violation",
    )
    if any(marker in msg for marker in permanent_markers):
        return False

    transient_markers: tuple[str, ...] = (
        "429",
        "resource exhausted",
        "resource_exhausted",
        "rate limit",
        "timed out",
        "timeout",
        "connection reset",
        "connection aborted",
        "connection error",
        "name or service not known",
        "temporary failure",
        "bad gateway",
        "service unavailable",
        "gateway timeout",
        "internal server error",
        "deadline exceeded",
        "unavailable",
    )
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
    if "not found in backend environment" in msg:
        return False
    return any(marker in msg for marker in transient_markers)


def _resolve_gender(gender: str | None, default_gender: str) -> str:
    g = (gender or default_gender or "female").strip().lower()
    if g not in {"female", "male"}:
        return "female"
    return g


def _pick_voice(
    settings, provider: str, voice_name: str | None, gender: str | None
) -> tuple[str, str]:
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
    types: Any = None
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


def _synthesize_wav_bytes(
    provider: str,
    text: str,
    settings,
    voice: str,
    client,
    types,
    usage_callback: Callable[[dict[str, Any]], None] | None = None,
) -> bytes:
    if provider == "openrouter":
        return _synthesize_openrouter_wav(text, settings, voice, usage_callback)
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


class TtsCancelledError(RuntimeError):
    """Raised between synthesis items after a cooperative cancellation."""


def _raise_if_cancelled(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise TtsCancelledError("TTS synthesis cancelled")


def _emit_batch_progress(
    progress_callback: Callable[[dict], None] | None,
    event: str,
    total: int,
    completed: int,
    generated: list[str],
    skipped: list[str],
) -> None:
    _emit_progress(
        progress_callback,
        {
            "event": event,
            "total": total,
            "completed": completed,
            "generated": len(generated),
            "skipped": len(skipped),
        },
    )


def _emit_item_progress(
    progress_callback: Callable[[dict], None] | None,
    event: str,
    block_id: str,
    index: int,
    total: int,
    completed: int,
    generated: list[str],
    skipped: list[str],
    *,
    attempts: int | None = None,
    error: str | None = None,
) -> None:
    payload = {
        "event": event,
        "id": block_id,
        "index": index,
        "total": total,
        "completed": completed,
        "generated": len(generated),
        "skipped": len(skipped),
    }
    if attempts is not None:
        payload["attempts"] = attempts
    if error is not None:
        payload["error"] = error
    _emit_progress(progress_callback, payload)


def _fingerprint_parameters(provider: str, settings) -> tuple[str, int, int]:
    if provider == "gemini":
        return settings.gemini_tts_model, 0, 0
    if provider == "openrouter":
        return (
            settings.openrouter_tts_model,
            settings.openrouter_tts_sample_rate,
            0,
        )
    return "", settings.macos_tts_sample_rate, settings.macos_tts_rate


def _prepare_tts_items(items: list[dict]) -> list[tuple[str, str, str]]:
    prepared_items: list[tuple[str, str, str]] = []
    for item in items:
        block_id = str(item.get("id", "")).strip()
        block_uid = str(item.get("block_uid") or block_id).strip()
        text = str(item.get("text_for_tts", "")).strip()
        if block_id and text:
            prepared_items.append((block_id, block_uid, text))
    return prepared_items


def _asset_uid(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        return value
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _materialize_voiceover_alias(asset: Path, alias: Path, fingerprint: str) -> None:
    if _is_valid_wav(alias) and _read_meta_fingerprint(alias) == fingerprint:
        return
    if asset != alias:
        _atomic_write_bytes(alias, asset.read_bytes(), validate_wav=True)
    _write_meta_fingerprint(alias, fingerprint)


def _plan_tts_items(
    prepared_items: list[tuple[str, str, str]],
    speaker_dir: Path,
    provider: str,
    voice: str,
    resolved_gender: str,
    speed: float,
    settings,
    dry_run: bool,
    cancel_check: Callable[[], bool] | None,
) -> list[tuple[int, str, str, str, Path, Path, str, str]]:
    fp_model, fp_sample_rate, fp_extra_rate = _fingerprint_parameters(provider, settings)
    plan: list[tuple[int, str, str, str, Path, Path, str, str]] = []

    for index, (block_id, block_uid, text) in enumerate(prepared_items, start=1):
        _raise_if_cancelled(cancel_check)
        out_file = speaker_dir / f"{block_id}.wav"
        fingerprint = _synthesis_fingerprint(
            text,
            provider,
            voice,
            resolved_gender,
            speed,
            fp_model,
            fp_sample_rate,
            fp_extra_rate,
        )
        if dry_run:
            # A placeholder must never become a production cache entry. Keep
            # its fingerprint distinct too: the legacy positional alias may
            # be the only file present on older projects.
            fingerprint = hashlib.sha256(f"dry-run\\n{fingerprint}".encode()).hexdigest()
            asset_file = speaker_dir / "dry-run" / "assets" / _asset_uid(block_uid) / f"{fingerprint}.wav"
        else:
            asset_file = speaker_dir / "assets" / _asset_uid(block_uid) / f"{fingerprint}.wav"
        legacy_cache_hit = (
            not dry_run
            and _is_valid_wav(out_file)
            and _read_meta_fingerprint(out_file) == fingerprint
        )
        if legacy_cache_hit and not asset_file.is_file():
            _atomic_write_bytes(asset_file, out_file.read_bytes(), validate_wav=True)
        cache_hit = not dry_run and _is_valid_wav(asset_file)
        if cache_hit:
            action = "cache"
        elif dry_run:
            action = "dry_run"
        else:
            action = "synthesize"
        plan.append((index, block_id, block_uid, text, out_file, asset_file, fingerprint, action))

    return plan


def _publish_local_item(
    planned_item: tuple[int, str, str, str, Path, Path, str, str],
    total_items: int,
    generated: list[str],
    skipped: list[str],
    progress_callback: Callable[[dict], None] | None,
) -> None:
    index, block_id, _block_uid, text, out_file, asset_file, fingerprint, action = planned_item
    _emit_item_progress(
        progress_callback,
        "item_started",
        block_id,
        index,
        total_items,
        index - 1,
        generated,
        skipped,
    )
    if action == "cache":
        _materialize_voiceover_alias(asset_file, out_file, fingerprint)
        skipped.append(str(out_file))
        event = "item_skipped"
    elif _is_valid_wav(out_file):
        # Dry-run placeholders are useful for a fresh project, but must not
        # overwrite any existing real positional WAV, regardless of UID.
        skipped.append(str(out_file))
        event = "item_skipped"
    else:
        _write_silence_wav(asset_file, duration_ms=max(900, int(len(text) * 55)))
        _materialize_voiceover_alias(asset_file, out_file, fingerprint)
        generated.append(str(out_file))
        event = "item_generated"
    _emit_item_progress(
        progress_callback,
        event,
        block_id,
        index,
        total_items,
        index,
        generated,
        skipped,
        attempts=1 if event == "item_generated" else None,
    )


def _synthesize_with_retry(
    provider: str,
    text: str,
    settings,
    voice: str,
    speed: float,
    cancel_check: Callable[[], bool] | None,
    usage_callback: Callable[[dict[str, Any]], None] | None,
) -> tuple[bytes | None, int, list[str], Exception | None]:
    attempts = 0
    retry_errors: list[str] = []
    while True:
        _raise_if_cancelled(cancel_check)
        attempts += 1
        try:
            # Gemini SDK clients are not assumed to be thread-safe. Give every
            # bounded task its own client; HTTP providers do not create one.
            worker_client, worker_types = _build_tts_client(provider, settings)
            wav_bytes = _synthesize_wav_bytes(
                provider,
                text,
                settings,
                voice,
                worker_client,
                worker_types,
                usage_callback,
            )
            _raise_if_cancelled(cancel_check)
            return _apply_tempo(wav_bytes, speed), attempts, retry_errors, None
        except TtsCancelledError:
            raise
        except Exception as exc:
            if attempts >= 4 or not _is_retryable_tts_error(provider, exc):
                return None, attempts, retry_errors, exc
            retry_errors.append(str(exc))
            time.sleep(min(60, 8 * attempts))


def _publish_synthesized_item(
    pending_item: tuple[int, str, str, str, Path, Path, str],
    future: Future[tuple[bytes | None, int, list[str], Exception | None]],
    total_items: int,
    generated: list[str],
    skipped: list[str],
    progress_callback: Callable[[dict], None] | None,
) -> None:
    index, block_id, _block_uid, _text, out_file, asset_file, fingerprint = pending_item
    _emit_item_progress(
        progress_callback,
        "item_started",
        block_id,
        index,
        total_items,
        index - 1,
        generated,
        skipped,
    )
    wav_bytes, attempts, retry_errors, final_error = future.result()
    for retry_attempt, error_text in enumerate(retry_errors, start=1):
        _emit_item_progress(
            progress_callback,
            "item_retrying",
            block_id,
            index,
            total_items,
            index - 1,
            generated,
            skipped,
            attempts=retry_attempt,
            error=error_text,
        )
    if final_error is not None or wav_bytes is None:
        if final_error is None:
            final_error = RuntimeError(f"TTS synthesis returned no audio for item {block_id}")
        _emit_item_progress(
            progress_callback,
            "item_failed",
            block_id,
            index,
            total_items,
            index - 1,
            generated,
            skipped,
            attempts=attempts,
            error=str(final_error),
        )
        raise final_error

    _atomic_write_bytes(asset_file, wav_bytes, validate_wav=True)
    _materialize_voiceover_alias(asset_file, out_file, fingerprint)
    generated.append(str(out_file))
    _emit_item_progress(
        progress_callback,
        "item_generated",
        block_id,
        index,
        total_items,
        index,
        generated,
        skipped,
        attempts=attempts,
    )


def synthesize_tts(
    prepared_path: Path,
    speaker_dir: Path,
    provider: str | None = None,
    voice_name: str | None = None,
    gender: str | None = None,
    dry_run: bool = False,
    progress_callback: Callable[[dict], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    speed: float = 1.0,
    usage_callback: Callable[[dict[str, Any]], None] | None = None,
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

    prepared_items = _prepare_tts_items(items)

    total_items = len(prepared_items)
    _emit_batch_progress(
        progress_callback,
        "started",
        total_items,
        0,
        generated,
        skipped,
    )
    plan = _plan_tts_items(
        prepared_items,
        speaker_dir,
        provider,
        voice,
        resolved_gender,
        speed,
        settings,
        dry_run,
        cancel_check,
    )

    pending = [item for item in plan if item[7] == "synthesize"]
    if pending:
        try:
            configured_workers = int(os.getenv("TTS_MAX_WORKERS", "3"))
        except ValueError:
            configured_workers = 3
        max_workers = max(1, min(4, configured_workers, len(pending)))
        pool = ThreadPoolExecutor(max_workers=max_workers)
        futures: dict[
            int,
            Future[tuple[bytes | None, int, list[str], Exception | None]],
        ] = {}
        try:
            futures = {
                index: pool.submit(
                    _synthesize_with_retry,
                    provider,
                    text,
                    settings,
                    voice,
                    speed,
                    cancel_check,
                    usage_callback,
                )
                for index, _, _, text, _, _, _, _ in pending
            }
            # Publish files and progress in scenario order. Network synthesis is
            # concurrent, while externally visible output remains deterministic.
            for planned_item in plan:
                _raise_if_cancelled(cancel_check)
                if planned_item[7] == "synthesize":
                    (
                        index,
                        block_id,
                        block_uid,
                        text,
                        out_file,
                        asset_file,
                        fingerprint,
                        _action,
                    ) = planned_item
                    _publish_synthesized_item(
                        (
                            index,
                            block_id,
                            block_uid,
                            text,
                            out_file,
                            asset_file,
                            fingerprint,
                        ),
                        futures[index],
                        total_items,
                        generated,
                        skipped,
                        progress_callback,
                    )
                else:
                    _publish_local_item(
                        planned_item,
                        total_items,
                        generated,
                        skipped,
                        progress_callback,
                    )
        except BaseException:
            for future in futures.values():
                future.cancel()
            # Provider calls already in progress cannot be forcefully stopped,
            # but queued requests must not delay cancellation or consume quota.
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            pool.shutdown(wait=True)
    else:
        for planned_item in plan:
            _raise_if_cancelled(cancel_check)
            _publish_local_item(
                planned_item,
                total_items,
                generated,
                skipped,
                progress_callback,
            )

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
        "scenario_revision_id": str(payload.get("scenario_revision_id") or ""),
        "voiceover_assets": [
            {
                "id": block_id,
                "block_uid": block_uid,
                "fingerprint": fingerprint,
                "asset": str(asset_file),
            }
            for (
                _index,
                block_id,
                block_uid,
                _text,
                _out_file,
                asset_file,
                fingerprint,
                _action,
            ) in plan
            if asset_file.is_file()
        ],
    }
    _emit_batch_progress(
        progress_callback,
        "completed",
        total_items,
        total_items,
        generated,
        skipped,
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
    usage_callback: Callable[[dict[str, Any]], None] | None = None,
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
        if _is_valid_wav(out_file):
            return {
                "speaker_dir": str(speaker_dir),
                "file": str(out_file),
                "provider": provider,
                "voice": voice,
                "gender": resolved_gender,
                "dry_run": True,
                "skipped": True,
                "text_chars": len(phrase),
                "generated_at": datetime.now(UTC).isoformat(),
            }
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
            wav_bytes = _synthesize_wav_bytes(
                provider, phrase, settings, voice, client, types, usage_callback
            )
            wav_bytes = _apply_tempo(wav_bytes, speed)
            _atomic_write_bytes(out_file, wav_bytes, validate_wav=True)
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
