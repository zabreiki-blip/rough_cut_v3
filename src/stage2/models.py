from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

SegmentType = Literal["zk", "snh", "tech"]


@dataclass
class TimelineSegment:
    id: str
    type: SegmentType
    text: str
    duration_sec: float
    start_sec: float = 0.0
    # SNH-specific
    speaker: str = ""
    role: str = ""
    timecode: str = ""
    # ZK-specific
    wav_path: str = ""
    # placeholder PNG (generated for SNH)
    placeholder_path: str = ""

    @property
    def end_sec(self) -> float:
        return self.start_sec + self.duration_sec

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["end_sec"] = self.end_sec
        return d


@dataclass
class FullTimeline:
    project: str
    segments: list[TimelineSegment] = field(default_factory=list)
    fps: int = 25
    width: int = 1920
    height: int = 1080

    @property
    def total_duration_sec(self) -> float:
        if not self.segments:
            return 0.0
        last = self.segments[-1]
        return last.start_sec + last.duration_sec

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "fps": self.fps,
            "width": self.width,
            "height": self.height,
            "total_duration_sec": self.total_duration_sec,
            "segments": [s.to_dict() for s in self.segments],
        }
