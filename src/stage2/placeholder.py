from __future__ import annotations

from functools import lru_cache
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_BG_COLOR = (10, 10, 10)
_ACCENT_COLOR = (220, 80, 60)
_TEXT_COLOR = (230, 230, 230)
_SUBTEXT_COLOR = (160, 160, 160)


@lru_cache(maxsize=16)
def _get_font(size: int) -> ImageFont.ImageFont:
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
    draw.text((120, height - 110), f"[ЗАГЛУШКА · {seg_id} · 10 сек]",
              font=font_id, fill=(80, 80, 80))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out_path), "PNG")
    return out_path
