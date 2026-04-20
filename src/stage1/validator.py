from __future__ import annotations

from datetime import UTC, datetime
import math
import os
from pathlib import Path
import re
import struct
import wave

from .config import get_settings
from .io_utils import read_json, write_json


def _env_int(name: str, default: int, min_value: int = 0, max_value: int = 10_000_000) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        value = default
    value = max(min_value, value)
    value = min(max_value, value)
    return value


def _read_pcm_stats(
    wf: wave.Wave_read,
    sample_width: int,
    n_channels: int,
) -> tuple[int, int]:
    """Return (rms, peak) for the first channel."""
    if sample_width not in {1, 2, 4}:
        return 0, 0

    chunk_frames = 8192
    rms_acc = 0.0
    sample_count = 0
    peak = 0

    while True:
        raw = wf.readframes(chunk_frames)
        if not raw:
            break

        if sample_width == 1:
            fmt = f"<{len(raw)}B"
            unpacked = struct.unpack(fmt, raw)
            samples = [x - 128 for x in unpacked]
        elif sample_width == 2:
            fmt = f"<{len(raw) // 2}h"
            samples = struct.unpack(fmt, raw)
        else:
            fmt = f"<{len(raw) // 4}i"
            samples = struct.unpack(fmt, raw)

        if n_channels > 1:
            samples = samples[::n_channels]
        if not samples:
            continue

        for sample in samples:
            value = int(sample)
            abs_value = abs(value)
            if abs_value > peak:
                peak = abs_value
            rms_acc += float(value) * float(value)
        sample_count += len(samples)

    if sample_count == 0:
        return 0, peak
    rms = int(math.sqrt(rms_acc / float(sample_count)))
    return rms, peak


def _audio_metrics_wav(path: Path) -> tuple[int, int, int]:
    """Return (duration_ms, rms, peak)."""
    try:
        with wave.open(str(path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate <= 0:
                return 0, 0, 0
            duration_ms = int((frames / float(rate)) * 1000)
            sample_width = wf.getsampwidth()
            n_channels = wf.getnchannels()
            rms, peak = _read_pcm_stats(
                wf,
                sample_width=sample_width,
                n_channels=n_channels,
            )
            return duration_ms, rms, peak
    except Exception:
        return 0, 0, 0


def validate_stage1(
    prepared_path: Path,
    speaker_dir: Path,
    manifest_path: Path,
    project_name: str | None = None,
) -> dict:
    payload = read_json(prepared_path)
    items = payload.get("items", [])
    if not isinstance(items, list) or not items:
        raise ValueError("Prepared scenario has no items")

    errors: list[str] = []
    warnings: list[str] = []
    min_duration_ms = _env_int("VALIDATE_MIN_WAV_DURATION_MS", 350, min_value=0, max_value=60_000)
    silence_rms_threshold = _env_int(
        "VALIDATE_SILENCE_RMS_THRESHOLD",
        120,
        min_value=0,
        max_value=32_767,
    )

    ids: list[str] = []
    for item in items:
        block_id = str(item.get("id", "")).strip()
        if not re.match(r"^zk\d+$", block_id):
            errors.append(f"Invalid id format: {block_id}")
            continue
        ids.append(block_id)

    dupes = sorted({x for x in ids if ids.count(x) > 1})
    for dup in dupes:
        errors.append(f"Duplicate id: {dup}")

    expected = [f"zk{i}" for i in range(1, len(ids) + 1)]
    if ids != expected:
        errors.append("IDs are not a continuous sequence zk1..zkN in order")

    manifest_items: list[dict] = []
    for item in items:
        block_id = str(item.get("id", "")).strip()
        text_for_tts = str(item.get("text_for_tts", "")).strip()
        file_path = speaker_dir / f"{block_id}.wav"
        if not file_path.exists():
            errors.append(f"Missing audio file: {file_path}")
            duration_ms = 0
        else:
            duration_ms, rms, _peak = _audio_metrics_wav(file_path)
            if duration_ms == 0:
                warnings.append(f"Could not read WAV duration: {file_path}")
            else:
                if min_duration_ms > 0 and duration_ms < min_duration_ms:
                    warnings.append(
                        f"WAV is too short ({duration_ms} ms < {min_duration_ms} ms): {file_path}"
                    )
                if silence_rms_threshold > 0 and rms <= silence_rms_threshold:
                    warnings.append(
                        f"WAV looks silent (rms={rms} <= {silence_rms_threshold}): {file_path}"
                    )
        manifest_items.append(
            {
                "id": block_id,
                "file": str(file_path),
                "duration_ms": duration_ms,
                "text_for_tts": text_for_tts,
            }
        )

    settings = get_settings()
    manifest_path.unlink(missing_ok=True)  # remove stale manifest before conditional write
    if not errors:
        manifest = {
            "project": project_name or settings.project_name,
            "generated_at": datetime.now(UTC).isoformat(),
            "items": manifest_items,
        }
        write_json(manifest_path, manifest)

    return {
        "ok": len(errors) == 0,
        "prepared_path": str(prepared_path),
        "speaker_dir": str(speaker_dir),
        "manifest_path": str(manifest_path),
        "items_count": len(items),
        "errors": errors,
        "warnings": warnings,
        "validation_config": {
            "min_wav_duration_ms": min_duration_ms,
            "silence_rms_threshold": silence_rms_threshold,
        },
    }
