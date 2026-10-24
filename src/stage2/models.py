from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, Literal

SegmentType = Literal["zk", "snh", "host", "life", "tech"]
TIMELINE_SCHEMA_VERSION = 1
# 1: neighbouring same-source fragments no longer duplicate padding (see ``trim``).
# Timelines built before the policy existed load as 0.
EDIT_POLICY_VERSION = 1


@dataclass
class TimelineSegment:
    id: str
    type: SegmentType
    text: str
    duration_sec: float
    start_sec: float = 0.0
    # Stable canonical normalization block identifier.
    block_uid: str = ""
    # Parent normalization block when one speaker block expands into fragments.
    parent_block_uid: str = ""
    # Scenario-version change status (for example ``added``).
    revision_status: str = ""
    # SNH-specific
    speaker: str = ""
    role: str = ""
    timecode: str = ""
    timecode_end: str = ""
    # Registered synchronized Premiere sequence and aligned source range.
    source_id: str = ""
    sequence_name: str = ""
    sequence_id: str = ""
    source_in_sec: float = 0.0
    source_out_sec: float = 0.0
    # Unpadded ASR word boundaries.  ``source_*`` is the range sent to
    # Premiere; keeping both makes a clipped edit diagnosable later.
    speech_in_sec: float = 0.0
    speech_out_sec: float = 0.0
    match_status: str = ""
    match_confidence: float = 0.0
    # Match diagnostics (empty for timelines built before they were recorded).
    unmatched_reason: str = ""
    match_strategy: str = ""
    # Range was set or kept by the editor (``manual_updated_at`` on the match).
    manual_range: bool = False
    # ``block_uid`` of the fragments merged into this segment.
    merged_from: list[str] = field(default_factory=list)
    # ZK-specific
    wav_path: str = ""
    # Voiceover origin of a ZK block: ``tts`` (own zkN.wav, in=0), ``recorded`` (a range of a
    # shared recording: ``wav_path`` is the montage WAV, ``source_id`` the recording id,
    # ``source_*``/``speech_*`` the range) or ``none`` (published map: no recording, no TTS).
    voiceover_kind: str = "tts"
    audio_source_duration_sec: float = 0.0
    audio_source_sha256: str = ""
    # ``recorded_unavailable``: the block is published as recorded but its audio/range is gone
    # (it falls back to TTS in a draft; a complete build is blocked).
    voiceover_problem: str = ""
    # placeholder PNG (generated for SNH)
    placeholder_path: str = ""
    # ``"№ 3"`` for a host stand-up stub (``ВЕДУЩИЙ № 3`` without text): the segment is a
    # placeholder card until a take is assigned by hand.  Empty for every other segment.
    host_standup: str = ""
    # Text that is actually expected in speech recognition. The canonical
    # scenario text stays intact in ``text``; older timelines fall back to a
    # deterministic derivation in final_review.
    spoken_text: str = ""
    # Padding de-duplication with a neighbouring same-source fragment:
    # ``{with_segment_id, rule, trimmed_head_sec, trimmed_tail_sec, reverse_jump,
    # speech_overlap, overlap_kept_manual}``; ``None`` when nothing was compared.
    trim: dict[str, Any] | None = None

    @property
    def end_sec(self) -> float:
        return self.start_sec + self.duration_sec

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["end_sec"] = self.end_sec
        return d

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TimelineSegment":
        if not isinstance(value, dict):
            raise TypeError("timeline segment must be an object")
        init_fields = {item.name for item in fields(cls) if item.init}
        required = {"id", "type", "text", "duration_sec"}
        missing = sorted(key for key in required if key not in value)
        if missing:
            raise ValueError(f"timeline segment is missing: {', '.join(missing)}")
        kwargs = {key: value[key] for key in init_fields if key in value}
        if kwargs["type"] not in {"zk", "snh", "host", "life", "tech"}:
            raise ValueError(f"invalid timeline segment type: {kwargs['type']!r}")
        for key in (
            "duration_sec",
            "start_sec",
            "source_in_sec",
            "source_out_sec",
            "speech_in_sec",
            "speech_out_sec",
            "match_confidence",
            "audio_source_duration_sec",
        ):
            if key in kwargs:
                try:
                    kwargs[key] = float(kwargs[key])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"timeline segment {key} must be numeric") from exc
        return cls(**kwargs)


@dataclass
class FullTimeline:
    project: str
    segments: list[TimelineSegment] = field(default_factory=list)
    fps: int = 25
    width: int = 1920
    height: int = 1080
    schema_version: int = TIMELINE_SCHEMA_VERSION
    edit_policy_version: int = EDIT_POLICY_VERSION
    # Fingerprint of the published recorded-voiceover map the timeline was built from
    # (empty: built without a map).  A different map makes the timeline stale.
    recorded_voiceover_sha256: str = ""

    @property
    def total_duration_sec(self) -> float:
        if not self.segments:
            return 0.0
        last = self.segments[-1]
        return last.start_sec + last.duration_sec

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "edit_policy_version": self.edit_policy_version,
            "recorded_voiceover_sha256": self.recorded_voiceover_sha256,
            "project": self.project,
            "fps": self.fps,
            "width": self.width,
            "height": self.height,
            "total_duration_sec": self.total_duration_sec,
            "segments": [s.to_dict() for s in self.segments],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FullTimeline":
        if not isinstance(value, dict):
            raise TypeError("timeline must be an object")
        version = int(value.get("schema_version", TIMELINE_SCHEMA_VERSION))
        if version < 1 or version > TIMELINE_SCHEMA_VERSION:
            raise ValueError(f"unsupported timeline schema_version: {version}")
        try:
            policy = int(value.get("edit_policy_version", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("timeline edit_policy_version must be an integer") from exc
        raw_segments = value.get("segments")
        if not isinstance(raw_segments, list):
            raise ValueError("timeline segments must be a list")
        project = str(value.get("project") or "").strip()
        if not project:
            raise ValueError("timeline project is required")
        try:
            fps = int(value.get("fps", 25))
            width = int(value.get("width", 1920))
            height = int(value.get("height", 1080))
        except (TypeError, ValueError) as exc:
            raise ValueError("timeline fps/width/height must be integers") from exc
        if fps < 1 or width < 1 or height < 1:
            raise ValueError("timeline fps/width/height must be positive")
        return cls(
            project=project,
            segments=[TimelineSegment.from_dict(item) for item in raw_segments],
            fps=fps,
            width=width,
            height=height,
            schema_version=version,
            edit_policy_version=policy,
            recorded_voiceover_sha256=str(value.get("recorded_voiceover_sha256") or ""),
        )
