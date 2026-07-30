"""Data contracts for Stage 2 transcript-to-sequence alignment.

The alignment artifacts deliberately live outside the normalized scenario.  A
Whisper rerun or a manual correction can therefore replace ``sync_matches.json``
without mutating the canonical Stage 1/2 scenario files.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from math import isfinite
from typing import Any, Literal

MatchStatus = Literal["auto_matched", "confirmed", "needs_review", "not_found"]
TimecodeMode = Literal["relative", "display"]


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if isfinite(result) else default


def _boolean(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


@dataclass(frozen=True)
class SyncSource:
    """A Premiere sequence and its exported, sample-aligned guide audio."""

    id: str
    speaker: str
    premiere_sequence: str
    # A sequence may contain several interviewees, or even every speaker in
    # the project. ``speaker`` remains as a legacy single-name alias.
    speakers: tuple[str, ...] = ()
    all_speakers: bool = False
    sequence_id: str = ""
    sequence_start_tc: str = "00:00:00:00"
    fps: float = 25.0
    guide_audio: str = ""
    # Position in the Premiere sequence corresponding to guide-audio second 0.
    guide_offset_sec: float = 0.0
    duration_sec: float = 0.0
    project_path: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("sync source id must not be empty")
        if not self.premiere_sequence.strip():
            raise ValueError("premiere_sequence must not be empty")
        if self.fps <= 0 or not isfinite(self.fps):
            raise ValueError("fps must be a positive finite number")
        if self.guide_offset_sec < 0 or not isfinite(self.guide_offset_sec):
            raise ValueError("guide_offset_sec must be a non-negative finite number")
        if self.duration_sec < 0 or not isfinite(self.duration_sec):
            raise ValueError("duration_sec must be a non-negative finite number")
        primary = self.speaker.strip()
        names: list[str] = []
        for value in self.speakers:
            name = str(value or "").strip()
            if (
                name
                and name.casefold() != primary.casefold()
                and name.casefold() not in {item.casefold() for item in names}
            ):
                names.append(name)
        if not primary and names:
            primary = names.pop(0)
            object.__setattr__(self, "speaker", primary)
        object.__setattr__(self, "speakers", tuple(names))

    @property
    def speaker_names(self) -> tuple[str, ...]:
        return (() if not self.speaker.strip() else (self.speaker.strip(),)) + self.speakers

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SyncSource":
        raw_speakers = value.get("speakers")
        if isinstance(raw_speakers, str):
            speakers = tuple(
                item.strip()
                for item in raw_speakers.replace(";", ",").replace("\n", ",").split(",")
                if item.strip()
            )
        elif isinstance(raw_speakers, (list, tuple)):
            speakers = tuple(str(item or "").strip() for item in raw_speakers if str(item or "").strip())
        else:
            speakers = ()
        scope = str(value.get("speaker_scope") or "").strip().lower()
        return cls(
            id=str(value.get("id") or value.get("source_id") or "").strip(),
            speaker=str(value.get("speaker") or "").strip(),
            premiere_sequence=str(
                value.get("premiere_sequence") or value.get("sequence_name") or value.get("name") or ""
            ).strip(),
            speakers=speakers,
            all_speakers=_boolean(value.get("all_speakers", False)) or scope == "all",
            sequence_id=str(value.get("sequence_id") or ""),
            sequence_start_tc=str(value.get("sequence_start_tc") or "00:00:00:00"),
            fps=_number(value.get("fps"), 25.0),
            guide_audio=str(value.get("guide_audio") or value.get("audio_path") or ""),
            guide_offset_sec=_number(value.get("guide_offset_sec"), 0.0),
            duration_sec=_number(value.get("duration_sec"), 0.0),
            project_path=str(value.get("project_path") or ""),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True)
class WordTimestamp:
    word: str
    start: float
    end: float
    confidence: float | None = None

    def __post_init__(self) -> None:
        if not self.word.strip():
            raise ValueError("word must not be empty")
        if not isfinite(self.start) or not isfinite(self.end):
            raise ValueError("word timestamps must be finite")
        if self.start < 0 or self.end < self.start:
            raise ValueError("invalid word timestamp range")
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("word confidence must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WordTimestamp":
        confidence = value.get("confidence", value.get("probability", value.get("score")))
        return cls(
            word=str(value.get("word") or value.get("text") or value.get("token") or "").strip(),
            start=_number(value.get("start")),
            end=_number(value.get("end", value.get("start"))),
            confidence=None if confidence is None else max(0.0, min(1.0, _number(confidence))),
        )


@dataclass
class WordIndex:
    """Provider-neutral word timestamps generated from one guide audio file."""

    source_id: str
    words: list[WordTimestamp] = field(default_factory=list)
    language: str = "ru"
    model: str = ""
    generated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source_id.strip():
            raise ValueError("word index source_id must not be empty")
        self.words.sort(key=lambda item: (item.start, item.end))

    @property
    def duration_sec(self) -> float:
        return self.words[-1].end if self.words else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "language": self.language,
            "model": self.model,
            "generated_at": self.generated_at,
            "metadata": self.metadata,
            "words": [item.to_dict() for item in self.words],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WordIndex":
        return cls(
            source_id=str(value.get("source_id") or "").strip(),
            words=[WordTimestamp.from_dict(item) for item in value.get("words", [])],
            language=str(value.get("language") or "ru"),
            model=str(value.get("model") or ""),
            generated_at=str(value.get("generated_at") or ""),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True)
class SyncAlternative:
    source_id: str
    source_in_sec: float
    source_out_sec: float
    confidence: float
    matched_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SyncAlternative":
        return cls(
            source_id=str(value.get("source_id") or ""),
            source_in_sec=_number(value.get("source_in_sec")),
            source_out_sec=_number(value.get("source_out_sec")),
            confidence=_number(value.get("confidence")),
            matched_text=str(value.get("matched_text") or ""),
        )


@dataclass
class SyncMatch:
    segment_id: str
    query_text: str
    speaker: str = ""
    approximate_timecode: str = ""
    source_id: str = ""
    source_in_sec: float | None = None
    source_out_sec: float | None = None
    confidence: float = 0.0
    status: MatchStatus = "not_found"
    matched_text: str = ""
    strategy: str = ""
    alternatives: list[SyncAlternative] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.segment_id.strip():
            raise ValueError("segment_id must not be empty")
        if self.status not in {"auto_matched", "confirmed", "needs_review", "not_found"}:
            raise ValueError(f"invalid match status: {self.status}")
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        if (self.source_in_sec is None) != (self.source_out_sec is None):
            raise ValueError("source_in_sec and source_out_sec must be set together")
        if self.source_in_sec is not None and self.source_out_sec is not None:
            if self.source_in_sec < 0 or self.source_out_sec < self.source_in_sec:
                raise ValueError("invalid source range")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["alternatives"] = [item.to_dict() for item in self.alternatives]
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SyncMatch":
        return cls(
            segment_id=str(value.get("segment_id") or "").strip(),
            query_text=str(value.get("query_text") or ""),
            speaker=str(value.get("speaker") or ""),
            approximate_timecode=str(value.get("approximate_timecode") or ""),
            source_id=str(value.get("source_id") or ""),
            source_in_sec=(
                None if value.get("source_in_sec") is None else _number(value.get("source_in_sec"))
            ),
            source_out_sec=(
                None if value.get("source_out_sec") is None else _number(value.get("source_out_sec"))
            ),
            confidence=_number(value.get("confidence")),
            status=str(value.get("status") or "not_found"),  # type: ignore[arg-type]
            matched_text=str(value.get("matched_text") or ""),
            strategy=str(value.get("strategy") or ""),
            alternatives=[
                SyncAlternative.from_dict(item) for item in value.get("alternatives", [])
            ],
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True)
class AlignmentRequest:
    segment_id: str
    text: str
    speaker: str = ""
    approximate_timecode: str | float | None = None
    source_ids: tuple[str, ...] = ()
    timecode_mode: TimecodeMode = "relative"

    def __post_init__(self) -> None:
        if not self.segment_id.strip():
            raise ValueError("segment_id must not be empty")
        if not self.text.strip():
            raise ValueError("alignment text must not be empty")
        if self.timecode_mode not in {"relative", "display"}:
            raise ValueError(f"invalid timecode_mode: {self.timecode_mode}")
