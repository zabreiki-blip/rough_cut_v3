from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class BlockType(str, Enum):
    VOICEOVER = "voiceover"
    SYNC = "sync"
    HOST = "host"
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
    memory_rule_id: int | None = None
    suggested_type: str = ""
    suggested_label: str = ""
    speaker: str = ""
    role: str = ""
    timecode_start: str = ""
    timecode_end: str = ""
    notes: str = ""
    proposed_type: str = ""
    proposed_label: str = ""
    proposed_label_confirmed: bool = False
    proposed_source: str = ""
    proposed_memory_rule_id: int | None = None
    continuation_of_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "NormalizedBlock":
        fields = cls.__dataclass_fields__
        return cls(**{key: value[key] for key in fields if key in value})


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
