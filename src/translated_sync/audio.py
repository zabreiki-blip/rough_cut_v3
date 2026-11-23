"""Validation and selected projection of translated voice audio."""

from __future__ import annotations

import hashlib
import math
import re


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def resolve_translation_audio(config: dict, query_text: str) -> dict:
    if not isinstance(config, dict):
        raise ValueError("translation audio must be an object")
    kind = config.get("active")
    variants = config.get("variants")
    if (
        kind not in ("tts", "recorded")
        or not isinstance(variants, dict)
        or not isinstance(variants.get(kind), dict)
    ):
        raise ValueError("translation audio selection is missing or invalid")
    variant = variants[kind]
    path = variant.get("path")
    digest, text_digest = variant.get("sha256"), variant.get("text_sha256")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("translation audio path is missing")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("translation audio sha256 is invalid")
    if text_digest != text_sha256(query_text):
        raise ValueError("translation audio text is stale")

    def number(source, key, default=None):
        try:
            value = float(source.get(key, default))
        except (ValueError, TypeError):
            raise ValueError(f"translation audio {key} is invalid") from None
        if not math.isfinite(value):
            raise ValueError(f"translation audio {key} is invalid")
        return value

    start, end, duration = (
        number(variant, key) for key in ("source_in_sec", "source_out_sec", "duration_sec")
    )
    if not 0 <= start < end <= duration:
        raise ValueError("translation audio source range is invalid")
    delay = number(config, "delay_sec", 0.8)
    fade = number(config, "fade_sec", 0.2)
    if delay < 0 or fade < 0:
        raise ValueError("translation audio delay or fade is invalid")
    duck_db = number(config, "duck_db", -18.0)
    gain_db = number(config, "gain_db", 0.0)
    if not -120 <= duck_db <= 0:
        raise ValueError("translation audio duck_db is out of range")
    if not -120 <= gain_db <= 24:
        raise ValueError("translation audio gain_db is out of range")
    return {
        "kind": kind,
        "path": path,
        "source_in_sec": start,
        "source_out_sec": end,
        "source_duration_sec": duration,
        "sha256": digest,
        "text_sha256": text_digest,
        "delay_sec": delay,
        "duck_db": duck_db,
        "gain_db": gain_db,
        "fade_sec": fade,
    }
