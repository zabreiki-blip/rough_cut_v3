from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class NarrationBlock:
    id: str
    text_original: str
    text_for_tts: str
    pause_ms: int = 300
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

