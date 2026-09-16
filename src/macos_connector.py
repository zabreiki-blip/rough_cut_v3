from __future__ import annotations

import os
import hmac
from functools import lru_cache
import subprocess

from flask import Flask, Response, jsonify, request
from werkzeug.exceptions import RequestEntityTooLarge

from env_utils import env_int
from logging_config import configure_logging
from stage1.macos_tts_backend import list_macos_voices, run_say_and_convert


configure_logging()
app = Flask(__name__)

_env_file = os.getenv("MACOS_CONNECTOR_ENV_FILE", "").strip()
if _env_file:
    try:
        from dotenv import load_dotenv

        load_dotenv(_env_file, override=False)
    except ImportError:
        pass


def _env_int(name: str, default: int, min_value: int, max_value: int) -> int:
    return env_int(
        name,
        default,
        minimum=min_value,
        maximum=max_value,
    )


app.config["MAX_CONTENT_LENGTH"] = _env_int(
    "MACOS_CONNECTOR_MAX_REQUEST_BODY_BYTES", 256 * 1024, 64 * 1024, 4 * 1024 * 1024
)


def _to_int(value, default: int, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    parsed = max(min_value, parsed)
    parsed = min(max_value, parsed)
    return parsed


def _auth_ok() -> bool:
    token = os.getenv("MACOS_CONNECTOR_TOKEN", "").strip()
    if not token:
        return False
    auth = request.headers.get("Authorization", "").strip()
    if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip(), token):
        return True
    supplied = request.headers.get("X-Connector-Token", "").strip()
    return bool(supplied) and hmac.compare_digest(supplied, token)


@lru_cache(maxsize=1)
def _available_voices() -> frozenset[str]:
    return frozenset(
        list_macos_voices(
            timeout=15,
            run=subprocess.run,
            # The connector endpoint checks command availability before use;
            # inventory enumeration itself historically always invoked `say`.
            which=None,
            logger=app.logger,
        )
    )


def _voice_exists(voice: str) -> bool:
    return bool(voice) and voice in _available_voices()


def _run_say_and_convert(text: str, voice: str, rate: int, sample_rate: int) -> bytes:
    command_timeout = _env_int("MACOS_COMMAND_TIMEOUT", 900, 30, 7200)
    return run_say_and_convert(
        text,
        voice=voice,
        rate=rate,
        sample_rate=sample_rate,
        timeout=command_timeout,
        run=subprocess.run,
        command_names=("say", "afconvert"),
    )


@app.get("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "say_available": bool(shutil_which("say")),
            "afconvert_available": bool(shutil_which("afconvert")),
        }
    )


def shutil_which(cmd: str) -> str | None:
    import shutil

    return shutil.which(cmd)


@app.errorhandler(RequestEntityTooLarge)
def request_entity_too_large(_exc: RequestEntityTooLarge):
    return jsonify({"ok": False, "error": "request body is too large"}), 413


@app.post("/synthesize")
def synthesize():
    if not os.getenv("MACOS_CONNECTOR_TOKEN", "").strip():
        return jsonify({"ok": False, "error": "MACOS_CONNECTOR_TOKEN is not configured"}), 503
    if not _auth_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    if not shutil_which("say"):
        return jsonify({"ok": False, "error": "say command not found"}), 500
    if not shutil_which("afconvert"):
        return jsonify({"ok": False, "error": "afconvert command not found"}), 500

    payload = request.get_json(silent=True) or {}
    text = str(payload.get("text", "")).strip()
    if not text:
        return jsonify({"ok": False, "error": "text is required"}), 400
    if len(text) > 12000:
        return jsonify({"ok": False, "error": "text too long"}), 400

    requested_voice = str(payload.get("voice", "")).strip()
    rate = _to_int(
        payload.get("rate"),
        default=_env_int("MACOS_TTS_RATE", 175, 80, 420),
        min_value=80,
        max_value=420,
    )
    sample_rate = _to_int(
        payload.get("sample_rate"),
        default=_env_int("MACOS_TTS_SAMPLE_RATE", 24000, 8000, 48000),
        min_value=8000,
        max_value=48000,
    )

    voice = requested_voice if _voice_exists(requested_voice) else ""
    try:
        wav = _run_say_and_convert(text=text, voice=voice, rate=rate, sample_rate=sample_rate)
    except RuntimeError as exc:
        app.logger.exception("macOS synthesis failed", exc_info=exc)
        return jsonify({"ok": False, "error": "macOS synthesis failed"}), 500

    headers = {"X-Voice-Used": voice or "default"}
    return Response(wav, status=200, mimetype="audio/wav", headers=headers)


if __name__ == "__main__":
    if not os.getenv("MACOS_CONNECTOR_TOKEN", "").strip():
        raise SystemExit("MACOS_CONNECTOR_TOKEN is required")
    port = _env_int("MACOS_CONNECTOR_PORT", 8001, 1, 65535)
    host = os.getenv("MACOS_CONNECTOR_BIND", "127.0.0.1").strip() or "127.0.0.1"
    app.run(host=host, port=port)
