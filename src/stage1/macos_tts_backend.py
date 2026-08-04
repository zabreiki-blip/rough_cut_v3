from __future__ import annotations

import logging
from pathlib import Path
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable


LOCAL_VOICE_FALLBACKS = ("Milena", "Yuri", "Samantha", "Alex")


def list_macos_voices(
    *,
    timeout: int = 15,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    which: Callable[[str], str | None] | None = shutil.which,
    logger: logging.Logger | None = None,
) -> tuple[str, ...]:
    """Return the names reported by ``say -v ?`` in their system order."""
    if which is not None and which("say") is None:
        return ()
    try:
        proc = run(
            ["say", "-v", "?"],
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )
    except Exception as exc:
        if logger is not None:
            logger.warning("Could not enumerate macOS voices: %s", exc)
        return ()

    voices: list[str] = []
    for line in proc.stdout.splitlines():
        name = line.strip().split(maxsplit=1)[0] if line.strip() else ""
        if name:
            voices.append(name)
    return tuple(voices)


def resolve_macos_voice(
    requested_voice: str,
    voices: Iterable[str],
    *,
    fallbacks: tuple[str, ...] = LOCAL_VOICE_FALLBACKS,
) -> str:
    """Apply the local-TTS voice policy to an already enumerated inventory."""
    available = tuple(voices)
    if not available:
        return requested_voice or fallbacks[-1]
    if requested_voice in available:
        return requested_voice
    for fallback in fallbacks:
        if fallback in available:
            return fallback
    return available[0]


def run_say_and_convert(
    text: str,
    *,
    voice: str,
    rate: int,
    sample_rate: int,
    timeout: int,
    retry_with_default_voice: bool = False,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    command_names: tuple[str, str] = ("macOS 'say'", "macOS 'afconvert'"),
) -> bytes:
    """Synthesize UTF-8 text to AIFF with ``say`` and convert it to WAV."""
    with tempfile.TemporaryDirectory() as temp_dir:
        src_text = Path(temp_dir) / "tts.txt"
        src_aiff = Path(temp_dir) / "tts.aiff"
        out_wav = Path(temp_dir) / "tts.wav"
        src_text.write_text(text, encoding="utf-8")

        def say_command(selected_voice: str) -> list[str]:
            command = [
                "say",
                "-r",
                str(rate),
                "-o",
                str(src_aiff),
                "-f",
                str(src_text),
            ]
            if selected_voice:
                command[1:1] = ["-v", selected_voice]
            return command

        selected_error: subprocess.CalledProcessError | None = None
        try:
            run(
                say_command(voice),
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"{command_names[0]} timed out after {timeout} seconds") from exc
        except subprocess.CalledProcessError as exc:
            selected_error = exc
            if not retry_with_default_voice or not voice:
                details = _command_error_details(exc, "unknown say error")
                raise RuntimeError(f"{command_names[0]} failed: {details}") from exc

        if selected_error is not None:
            try:
                run(
                    say_command(""),
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"{command_names[0]} timed out after {timeout} seconds") from exc
            except subprocess.CalledProcessError as exc:
                details = _command_error_details(exc, "")
                if not details:
                    details = _command_error_details(selected_error, "unknown say error")
                raise RuntimeError(f"{command_names[0]} failed: {details}") from exc

        afconvert_command = [
            "afconvert",
            "-f",
            "WAVE",
            "-d",
            f"LEI16@{sample_rate}",
            str(src_aiff),
            str(out_wav),
        ]
        try:
            run(
                afconvert_command,
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"{command_names[1]} timed out after {timeout} seconds") from exc
        except subprocess.CalledProcessError as exc:
            details = _command_error_details(exc, "unknown afconvert error")
            raise RuntimeError(f"{command_names[1]} failed: {details}") from exc
        return out_wav.read_bytes()


def _command_error_details(exc: subprocess.CalledProcessError, default: str) -> str:
    return (exc.stderr or exc.stdout or "").strip() or default
