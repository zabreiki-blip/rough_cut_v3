from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile

from flask import Flask, Response, jsonify, request


app = Flask(__name__)


def _env_int(name: str, default: int, min_value: int, max_value: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        value = default
    value = max(min_value, value)
    value = min(max_value, value)
    return value


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
        return True
    auth = request.headers.get("Authorization", "").strip()
    if auth == f"Bearer {token}":
        return True
    return request.headers.get("X-Connector-Token", "").strip() == token


def _voice_exists(voice: str) -> bool:
    if not voice:
        return False
    try:
        proc = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, check=True)
    except Exception:
        return False
    for line in proc.stdout.splitlines():
        if line.strip().startswith(f"{voice} "):
            return True
    return False


def _run_say_and_convert(text: str, voice: str, rate: int, sample_rate: int) -> bytes:
    with tempfile.TemporaryDirectory() as td:
        src_aiff = Path(td) / "tts.aiff"
        out_wav = Path(td) / "tts.wav"

        say_cmd = ["say", "-r", str(rate), "-o", str(src_aiff), text]
        if voice:
            say_cmd[1:1] = ["-v", voice]
        try:
            subprocess.run(say_cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            details = (exc.stderr or exc.stdout or "").strip() or "unknown say error"
            raise RuntimeError(f"say failed: {details}") from exc

        af_cmd = [
            "afconvert",
            "-f",
            "WAVE",
            "-d",
            f"LEI16@{sample_rate}",
            str(src_aiff),
            str(out_wav),
        ]
        try:
            subprocess.run(af_cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            details = (exc.stderr or exc.stdout or "").strip() or "unknown afconvert error"
            raise RuntimeError(f"afconvert failed: {details}") from exc
        return out_wav.read_bytes()


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


@app.post("/synthesize")
def synthesize():
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
        return jsonify({"ok": False, "error": str(exc)}), 500

    headers = {"X-Voice-Used": voice or "default"}
    return Response(wav, status=200, mimetype="audio/wav", headers=headers)


if __name__ == "__main__":
    port = _env_int("MACOS_CONNECTOR_PORT", 8001, 1, 65535)
    app.run(host="0.0.0.0", port=port)
