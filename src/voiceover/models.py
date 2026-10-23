from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Mapping

RANGE_STATUSES = ("needs_review", "confirmed", "missing")
ORIGINS = ("auto", "manual")
VOICE_SOURCES = ("recorded", "tts", "none")


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _text(value: Any, name: str, *, required: bool = True) -> str:
    if not isinstance(value, str) or (required and not value):
        raise ValueError(f"{name} must be a non-empty string" if required else f"{name} invalid")
    return value


def _int(value: Any, name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


@dataclass(frozen=True)
class RecordingSource:
    """One immutable recording file (or one part of a recording)."""

    id: str
    asset: str  # project-relative path of the montage WAV
    sha256: str
    duration_sec: float
    sample_rate: int
    channels: int
    part_index: int = 0
    part_name: str = ""
    original_asset: str = ""  # project-relative path of the untouched imported file

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RecordingSource":
        duration = _finite(data.get("duration_sec"), "duration_sec")
        if duration <= 0:
            raise ValueError("duration_sec must be positive")
        asset = _text(data.get("asset"), "asset")
        return cls(
            id=_text(data.get("id"), "id"),
            asset=asset,
            sha256=_text(data.get("sha256"), "sha256"),
            duration_sec=duration,
            sample_rate=_int(data.get("sample_rate"), "sample_rate", minimum=1),
            channels=_int(data.get("channels"), "channels", minimum=1),
            part_index=_int(data.get("part_index", 0), "part_index", minimum=0),
            part_name=_text(data.get("part_name", ""), "part_name", required=False),
            original_asset=_text(data.get("original_asset", asset), "original_asset"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "asset": self.asset,
            "sha256": self.sha256,
            "duration_sec": self.duration_sec,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "part_index": self.part_index,
            "part_name": self.part_name,
            "original_asset": self.original_asset,
        }


def _validated_bounds(
    data: Mapping[str, Any],
    duration_sec: float,
    *,
    default_speech: tuple[float, float] | None = None,
) -> tuple[float, float, float, float]:
    start = _finite(data.get("source_in_sec"), "source_in_sec")
    end = _finite(data.get("source_out_sec"), "source_out_sec")
    if not 0.0 <= start < end <= duration_sec:
        raise ValueError("expected 0 <= source_in_sec < source_out_sec <= duration")
    speech_in = data.get("speech_in_sec")
    speech_out = data.get("speech_out_sec")
    speech_in = start if speech_in is None else _finite(speech_in, "speech_in_sec")
    speech_out = end if speech_out is None else _finite(speech_out, "speech_out_sec")
    if not start <= speech_in < speech_out <= end:
        raise ValueError("speech range must lie inside the source range")
    return start, end, speech_in, speech_out


def _confidence(value: Any) -> float:
    number = _finite(0.0 if value is None else value, "confidence")
    if not 0.0 <= number <= 1.0:
        raise ValueError("confidence must be within 0..1")
    return number


@dataclass(frozen=True)
class Take:
    """One reading of a block in the recording (full or partial duplicate)."""

    source_in_sec: float
    source_out_sec: float
    speech_in_sec: float
    speech_out_sec: float
    confidence: float = 0.0
    partial: bool = False

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, duration_sec: float) -> "Take":
        start, end, speech_in, speech_out = _validated_bounds(data, duration_sec)
        partial = data.get("partial", False)
        if not isinstance(partial, bool):
            raise ValueError("partial must be boolean")
        return cls(start, end, speech_in, speech_out, _confidence(data.get("confidence")), partial)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_in_sec": self.source_in_sec,
            "source_out_sec": self.source_out_sec,
            "speech_in_sec": self.speech_in_sec,
            "speech_out_sec": self.speech_out_sec,
            "confidence": self.confidence,
            "partial": self.partial,
        }


@dataclass(frozen=True)
class VoiceoverRange:
    """Binding of a scenario block to a range of a recording."""

    block_uid: str
    text_sha256: str
    recording_id: str
    source_in_sec: float
    source_out_sec: float
    speech_in_sec: float
    speech_out_sec: float
    status: str = "needs_review"
    confidence: float = 0.0
    manual_range: bool = False
    takes: tuple[Take, ...] = field(default_factory=tuple)
    selected_take: int | None = None  # None: auto (last full take)
    origin: str = "auto"
    voice_source: str = "recorded"
    note: str = ""  # human-readable flag: "дубли: 3", "частичный дубль", ...
    display_text: str = ""  # original scenario text of the block (for the UI)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, duration_sec: float) -> "VoiceoverRange":
        status = data.get("status", "needs_review")
        if status not in RANGE_STATUSES:
            raise ValueError(f"unknown status: {status!r}")
        if status == "missing":
            # A block that was not found has no range: zero placeholder bounds.
            start = end = speech_in = speech_out = 0.0
        else:
            start, end, speech_in, speech_out = _validated_bounds(data, duration_sec)
        origin = data.get("origin", "auto")
        if origin not in ORIGINS:
            raise ValueError(f"unknown origin: {origin!r}")
        voice_source = data.get("voice_source")
        if voice_source is None:
            voice_source = "recorded" if status == "confirmed" else "none"
        if voice_source not in VOICE_SOURCES:
            raise ValueError(f"unknown voice_source: {voice_source!r}")
        raw_takes = data.get("takes") or []
        if not isinstance(raw_takes, (list, tuple)):
            raise ValueError("takes must be a list")
        takes = tuple(Take.from_dict(item, duration_sec=duration_sec) for item in raw_takes)
        if status == "missing" and takes:
            raise ValueError("a missing block cannot have takes")
        selected = data.get("selected_take")
        if selected is not None:
            selected = _int(selected, "selected_take", minimum=0)
            if selected >= len(takes):
                raise ValueError("selected_take is outside of takes")
        manual_range = data.get("manual_range", False)
        if not isinstance(manual_range, bool):
            raise ValueError("manual_range must be boolean")
        return cls(
            block_uid=_text(data.get("block_uid"), "block_uid"),
            text_sha256=_text(data.get("text_sha256", ""), "text_sha256", required=False),
            recording_id=_text(
                data.get("recording_id", ""), "recording_id", required=status != "missing"
            ),
            source_in_sec=start,
            source_out_sec=end,
            speech_in_sec=speech_in,
            speech_out_sec=speech_out,
            status=status,
            confidence=_confidence(data.get("confidence")),
            manual_range=manual_range,
            takes=takes,
            selected_take=selected,
            origin=origin,
            voice_source=voice_source,
            note=_text(data.get("note", ""), "note", required=False),
            display_text=_text(data.get("display_text", ""), "display_text", required=False),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_uid": self.block_uid,
            "text_sha256": self.text_sha256,
            "recording_id": self.recording_id,
            "source_in_sec": self.source_in_sec,
            "source_out_sec": self.source_out_sec,
            "speech_in_sec": self.speech_in_sec,
            "speech_out_sec": self.speech_out_sec,
            "status": self.status,
            "confidence": self.confidence,
            "manual_range": self.manual_range,
            "takes": [take.to_dict() for take in self.takes],
            "selected_take": self.selected_take,
            "origin": self.origin,
            "voice_source": self.voice_source,
            "note": self.note,
            "display_text": self.display_text,
        }
