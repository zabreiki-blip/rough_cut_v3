from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class BlockType(str, Enum):
    VOICEOVER = "voiceover"
    SYNC = "sync"
    HOST = "host"
    LIFE = "life"
    TECHNICAL = "technical"
    OTHER = "other"
    UNKNOWN = "unknown"


class DecisionSource(str, Enum):
    BUILTIN_RULE = "builtin_rule"
    MEMORY = "memory"
    LLM = "llm"
    MANUAL = "manual"
    UNKNOWN = "unknown"


class NormalizationStatus(str, Enum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"


@dataclass
class SyncFragment:
    text: str
    timecode_start: str
    timecode_end: str = ""

    def to_dict(self) -> dict[str, str]:
        payload = {
            "text": self.text,
            "timecode_start": self.timecode_start,
        }
        if self.timecode_end:
            payload["timecode_end"] = self.timecode_end
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SyncFragment":
        return cls(
            text=str(value.get("text", "")),
            timecode_start=str(value.get("timecode_start", "")),
            timecode_end=str(value.get("timecode_end", "")),
        )


@dataclass
class NormalizedBlock:
    uid: str
    order: int
    original_text: str
    normalized_text: str
    type: str = BlockType.UNKNOWN.value
    text_for_tts: str = ""
    original_label: str = ""
    label_confirmed: bool = False
    source: str = DecisionSource.UNKNOWN.value
    confidence: float = 0.0
    warnings: list[str] = field(default_factory=list)
    # Exact warning strings explicitly cleared by a reviewer. Kept separately
    # so a same-source re-run does not recreate already resolved warnings.
    reviewed_warnings: list[str] = field(default_factory=list)
    # Tracks reviewer edits to any editable block field without changing the
    # classification source used by the learning pipeline.
    manually_edited: bool = False
    # Server-maintained field-level overrides for same-source regeneration.
    manual_fields: list[str] = field(default_factory=list)
    memory_rule_id: int | None = None
    suggested_type: str = ""
    suggested_label: str = ""
    speaker: str = ""
    role: str = ""
    source_description: str = ""
    timecode_start: str = ""
    timecode_end: str = ""
    sync_fragments: list[SyncFragment] = field(default_factory=list)
    notes: str = ""
    proposed_type: str = ""
    proposed_label: str = ""
    proposed_label_confirmed: bool = False
    proposed_source: str = ""
    proposed_memory_rule_id: int | None = None
    continuation_of_type: str = ""
    absorbed_by_uid: str = ""
    # UID of the block that opened the LIFE/SYNC/HOST scene this block belongs
    # to (dialogue replies, ``+`` splices, continuations).  Empty for the
    # opening block itself and for blocks outside a scene.  Optional: older
    # JSON without the key reads with the default.
    scene_uid: str = ""
    # Stage 4 attaches the block's relationship to the previous scenario
    # revision here.  It must survive normalization model round-trips so the
    # timeline exporter can highlight changed clips in Premiere.
    versioning: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["sync_fragments"] = [fragment.to_dict() for fragment in self.sync_fragments]
        if not self.versioning:
            payload.pop("versioning", None)
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "NormalizedBlock":
        fields = cls.__dataclass_fields__
        kwargs = {
            key: value[key]
            for key in fields
            if key in value and key not in {"sync_fragments", "versioning"}
        }
        kwargs["sync_fragments"] = [
            SyncFragment.from_dict(item)
            for item in (value.get("sync_fragments") or [])
            if isinstance(item, dict)
        ]
        versioning = value.get("versioning")
        kwargs["versioning"] = dict(versioning) if isinstance(versioning, dict) else {}
        return cls(**kwargs)


@dataclass
class NormalizedScenario:
    source: str
    source_hash: str
    blocks: list[NormalizedBlock]
    status: str = NormalizationStatus.DRAFT.value
    revision: int = 1
    schema_version: int = 1
    hint: str = ""
    created_at: str = ""
    updated_at: str = ""
    confirmed_at: str = ""
    warnings: list[str] = field(default_factory=list)
    scenario_revision_id: str = ""
    parent_revision_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["blocks"] = [block.to_dict() for block in self.blocks]
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "NormalizedScenario":
        fields = cls.__dataclass_fields__
        kwargs = {key: value[key] for key in fields if key in value and key != "blocks"}
        kwargs["blocks"] = [NormalizedBlock.from_dict(item) for item in value.get("blocks", [])]
        return cls(**kwargs)


VALID_BLOCK_TYPES = {item.value for item in BlockType}
VALID_DECISION_SOURCES = {item.value for item in DecisionSource}
