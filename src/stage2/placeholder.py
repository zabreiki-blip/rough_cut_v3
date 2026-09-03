from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import re
import textwrap
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_BG_COLOR = (10, 10, 10)
_ACCENT_COLOR = (220, 80, 60)
_TEXT_COLOR = (230, 230, 230)
_SUBTEXT_COLOR = (160, 160, 160)
_ASSET_KEY_VERSION = 1


def _asset_text(value: str) -> str:
    """Return a platform-independent representation for placeholder identity."""
    return unicodedata.normalize("NFC", str(value)).replace("\r\n", "\n").replace("\r", "\n")


def placeholder_asset_key(
    block_uid: str,
    *,
    text: str,
    speaker: str,
    segment_type: str,
    role: str = "",
    timecode: str = "",
    footer_label: str = "",
    width: int = 1920,
    height: int = 1080,
) -> str:
    """Return a deterministic, version-safe key for a placeholder image.

    Positional segment ids (for example ``snh1``) deliberately do not take part
    in the identity.  A block therefore keeps its asset when it moves, while a
    visible content change produces a new asset without overwriting the old one.
    """
    uid = _asset_text(block_uid).strip()
    kind = _asset_text(segment_type).strip().lower()
    if not uid:
        raise ValueError("placeholder block_uid is required")
    if not kind:
        raise ValueError("placeholder segment_type is required")

    payload = {
        "version": _ASSET_KEY_VERSION,
        "block_uid": uid,
        "segment_type": kind,
        "speaker": _asset_text(speaker),
        "role": _asset_text(role),
        "timecode": _asset_text(timecode),
        "text": _asset_text(text),
        "footer_label": _asset_text(footer_label),
        "width": int(width),
        "height": int(height),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:20]
    safe_uid = re.sub(r"[^A-Za-z0-9_-]+", "-", uid).strip("-_")[:48] or "block"
    return f"placeholder-v{_ASSET_KEY_VERSION}-{safe_uid}-{digest}"


def placeholder_asset_path(
    directory: Path,
    block_uid: str,
    *,
    text: str,
    speaker: str,
    segment_type: str,
    role: str = "",
    timecode: str = "",
    footer_label: str = "",
    width: int = 1920,
    height: int = 1080,
) -> Path:
    """Return the content-addressed PNG path for a scenario block."""
    key = placeholder_asset_key(
        block_uid,
        text=text,
        speaker=speaker,
        segment_type=segment_type,
        role=role,
        timecode=timecode,
        footer_label=footer_label,
        width=width,
        height=height,
    )
    return Path(directory) / f"{key}.png"


@lru_cache(maxsize=16)
def _get_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ):
        try:
            return ImageFont.truetype(name, size)
        except (IOError, OSError):
            continue
    return ImageFont.load_default()


def generate_placeholder(
    seg_id: str,
    speaker: str,
    role: str,
    timecode: str,
    text: str,
    out_path: Path,
    width: int = 1920,
    height: int = 1080,
    footer_label: str = "",
) -> Path:
    """Generate a black PNG placeholder card for an SNH segment."""
    img = Image.new("RGB", (width, height), _BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Accent bar on left
    draw.rectangle([(60, 60), (80, height - 60)], fill=_ACCENT_COLOR)

    # Speaker name
    font_speaker = _get_font(96)
    draw.text((120, 120), speaker.upper(), font=font_speaker, fill=_TEXT_COLOR)

    # Role + timecode
    font_role = _get_font(56)
    draw.text((122, 250), f"{role}  ·  {timecode}", font=font_role, fill=_SUBTEXT_COLOR)

    # Divider line
    draw.line([(120, 340), (width - 120, 340)], fill=(50, 50, 50), width=2)

    # Quote text (wrapped)
    font_text = _get_font(52)
    wrapped = textwrap.fill(text[:400], width=50)
    draw.text((120, 380), wrapped, font=font_text, fill=_SUBTEXT_COLOR)

    # Bottom label
    font_id = _get_font(40)
    footer = footer_label or f"ЗАГЛУШКА · {seg_id} · 10 сек"
    draw.text((120, height - 110), f"[{footer}]",
              font=font_id, fill=(80, 80, 80))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out_path), "PNG")
    return out_path


def generate_versioned_placeholder(
    block_uid: str,
    speaker: str,
    role: str,
    timecode: str,
    text: str,
    output_dir: Path,
    *,
    segment_type: str,
    width: int = 1920,
    height: int = 1080,
    footer_label: str = "",
) -> Path:
    """Generate a placeholder without using a positional id as its filename."""
    out_path = placeholder_asset_path(
        output_dir,
        block_uid,
        text=text,
        speaker=speaker,
        segment_type=segment_type,
        role=role,
        timecode=timecode,
        footer_label=footer_label,
        width=width,
        height=height,
    )
    if out_path.is_file():
        return out_path
    return generate_placeholder(
        seg_id=block_uid,
        speaker=speaker,
        role=role,
        timecode=timecode,
        text=text,
        out_path=out_path,
        width=width,
        height=height,
        footer_label=footer_label,
    )
