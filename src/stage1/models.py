from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class NarrationBlock:
    id: str
    text_original: str
    text_for_tts: str
    pause_ms: int = 300
    notes: str = ""
    # Internal source ranges used while producing the legacy raw projection.
    # They deliberately stay out of scenario_prepared.json.
    source_spans: list[tuple[int, int]] = field(
        default_factory=list,
        repr=False,
        compare=False,
    )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("source_spans", None)
        return payload
