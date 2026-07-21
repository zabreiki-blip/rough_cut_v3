from __future__ import annotations

from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from .config import get_settings
from .io_utils import extract_text, read_json
from .normalization_memory import DEFAULT_MEMORY_PATH, NormalizationMemory, normalize_marker
from .normalization_models import (
    BlockType,
    DecisionSource,
    NormalizationStatus,
    NormalizedBlock,
    NormalizedScenario,
    VALID_BLOCK_TYPES,
)


LLMClassifier = Callable[[list[dict[str, Any]], str], list[dict[str, Any]]]


class NormalizationError(RuntimeError):
    pass


class RevisionConflictError(NormalizationError):
    pass


class NormalizationValidationError(NormalizationError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


class SourceChangedError(NormalizationError):
    pass


_BUILTIN_MARKERS = {
    "vo": BlockType.VOICEOVER.value,
    "narrator": BlockType.VOICEOVER.value,
    "зк": BlockType.VOICEOVER.value,
    "закадр": BlockType.VOICEOVER.value,
    "закадровый текст": BlockType.VOICEOVER.value,
    "диктор": BlockType.VOICEOVER.value,
    "голос диктора": BlockType.VOICEOVER.value,
    "снх": BlockType.SYNC.value,
    "синхр": BlockType.SYNC.value,
    "синхрон": BlockType.SYNC.value,
    "интервью": BlockType.SYNC.value,
    "ведущий": BlockType.HOST.value,
    "ведущая": BlockType.HOST.value,
}
_TECH_MARKERS = {
    "видео", "лайф", "титр", "плашка", "графика", "карта", "фото",
    "архив", "отбивка", "шапка", "склейка", "ремарка", "техническое",
}
_TIMECODE = re.compile(r"\[?(\d{1,2}[:.]\d{2}(?:[:.]\d{2})?)\]?")
_SYNC_METADATA = re.compile(
    r"^([А-ЯЁA-Z][А-ЯЁа-яёA-Za-z\s\-]{1,60}),\s*"
    r"([^,]{2,80}),\s*(\d{1,2}[:.]\d{2}(?:[:.]\d{2})?)"
    r"[.\s–\-]*(.*)$",
    re.DOTALL,
)
_LLM_CONFIDENCE_THRESHOLD = 0.65
_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def generate_normalization(
    input_path: Path,
    output_path: Path,
    *,
    memory_path: Path | None = None,
    use_llm: bool = True,
    hint: str = "",
    classifier: LLMClassifier | None = None,
) -> dict[str, Any]:
    with _path_lock(output_path):
        return _generate_normalization_unlocked(
            input_path,
            output_path,
            memory_path=memory_path,
            use_llm=use_llm,
            hint=hint,
            classifier=classifier,
        )


def _generate_normalization_unlocked(
    input_path: Path,
    output_path: Path,
    *,
    memory_path: Path | None = None,
    use_llm: bool = True,
    hint: str = "",
    classifier: LLMClassifier | None = None,
) -> dict[str, Any]:
    """Create and persist a complete, ordered normalization draft."""
    input_path = Path(input_path)
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")
    source_hash = _file_hash(input_path)
    paragraphs = _extract_paragraphs(input_path)
    now = _utcnow()
    blocks: list[NormalizedBlock] = []
    unresolved: list[NormalizedBlock] = []

    with NormalizationMemory(memory_path or DEFAULT_MEMORY_PATH) as memory:
        for order, original in enumerate(paragraphs, start=1):
            label, content = _split_label(original)
            block = NormalizedBlock(
                uid=f"block-{order}-{uuid4().hex[:8]}",
                order=order,
                original_text=original,
                normalized_text=original,
                text_for_tts="",
                original_label=label,
            )
            _classify_by_rules(block, memory)
            if block.type != BlockType.UNKNOWN.value and block.original_label:
                block.label_confirmed = True
                block.normalized_text = content or original
            _extract_block_metadata(block)
            if block.type == BlockType.VOICEOVER.value:
                block.text_for_tts = block.normalized_text
            if block.type == BlockType.UNKNOWN.value:
                unresolved.append(block)
            blocks.append(block)

        if unresolved and use_llm:
            llm = classifier or _openrouter_classifier
            batches = _llm_batches(unresolved)
            payloads = [[_llm_input(block) for block in batch] for batch in batches]

            # A large documentary script can contain hundreds of unresolved
            # paragraphs. OpenRouter calls are independent, so bounded
            # concurrency keeps the HTTP request practical without flooding the
            # provider. Injected classifiers remain sequential for deterministic
            # tests and custom integrations.
            if classifier is None and len(batches) > 1:
                with ThreadPoolExecutor(max_workers=min(4, len(batches))) as pool:
                    futures = [pool.submit(llm, payload, hint) for payload in payloads]
                    batch_results = []
                    for future in futures:
                        try:
                            batch_results.append((future.result(), None))
                        except Exception as exc:
                            batch_results.append((None, exc))
            else:
                batch_results = []
                for payload in payloads:
                    try:
                        batch_results.append((llm(payload, hint), None))
                    except Exception as exc:
                        batch_results.append((None, exc))

            for batch, (results, error) in zip(batches, batch_results):
                if error is None:
                    _apply_llm_results(batch, results or [])
                    continue
                message = f"LLM classification failed: {type(error).__name__}: {error}"
                for block in batch:
                    block.warnings.append(message)

    scenario = NormalizedScenario(
        source=str(input_path),
        source_hash=source_hash,
        blocks=blocks,
        hint=hint.strip(),
        created_at=now,
        updated_at=now,
    )
    payload = scenario.to_dict()
    _atomic_write_json(Path(output_path), payload)
    return payload


def load_normalization(path: Path) -> dict[str, Any]:
    return NormalizedScenario.from_dict(read_json(Path(path))).to_dict()


def reset_normalization(
    path: Path,
    *,
    expected_revision: int | None = None,
    source_root: Path | None = None,
) -> dict[str, Any]:
    """Clear all normalization blocks while preserving source identity and revision history."""
    with _path_lock(path):
        path = Path(path)
        scenario = NormalizedScenario.from_dict(read_json(path))
        if expected_revision is not None and scenario.revision != expected_revision:
            raise RevisionConflictError(
                f"Expected revision {expected_revision}, current revision is {scenario.revision}"
            )
        source_path = Path(scenario.source).resolve()
        if source_root is not None and not source_path.is_relative_to(Path(source_root).resolve()):
            raise SourceChangedError("Normalization source is outside the project")
        if not source_path.exists() or _file_hash(source_path) != scenario.source_hash:
            raise SourceChangedError("Source scenario changed after normalization")

        scenario.blocks = []
        scenario.status = NormalizationStatus.DRAFT.value
        scenario.revision += 1
        scenario.hint = ""
        scenario.warnings = []
        scenario.updated_at = _utcnow()
        scenario.confirmed_at = ""
        result = scenario.to_dict()
        _atomic_write_json(path, result)
        return result


def save_normalization(
    path: Path,
    payload: dict[str, Any],
    *,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    with _path_lock(path):
        return _save_normalization_unlocked(
            path, payload, expected_revision=expected_revision
        )


def _save_normalization_unlocked(
    path: Path,
    payload: dict[str, Any],
    *,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Save user edits as a new draft revision, preserving source identity."""
    path = Path(path)
    current = NormalizedScenario.from_dict(read_json(path)) if path.exists() else None
    if current is not None and expected_revision is not None and current.revision != expected_revision:
        raise RevisionConflictError(
            f"Expected revision {expected_revision}, current revision is {current.revision}"
        )
    incoming = NormalizedScenario.from_dict(payload)
    if current is not None:
        if incoming.source_hash != current.source_hash or incoming.source != current.source:
            raise SourceChangedError("Source and source_hash cannot be changed while editing a draft")
        previous = {block.uid: block for block in current.blocks}
        for block in incoming.blocks:
            old = previous.get(block.uid)
            if old is not None and block.original_label and not old.label_confirmed:
                _label, content = _split_label(old.original_text)
                if block.original_label != old.original_label:
                    block.label_confirmed = True
                elif content and (
                    block.normalized_text == content or block.text_for_tts == content
                ):
                    block.label_confirmed = True
                    block.normalized_text = content
            if old is not None and (
                block.type != old.type
                or block.original_label != old.original_label
                or block.normalized_text != old.normalized_text
                or block.text_for_tts != old.text_for_tts
            ):
                block.source = DecisionSource.MANUAL.value
                block.confidence = 1.0
            if old is not None:
                block.original_text = old.original_text
            block.warnings = list(dict.fromkeys(block.warnings))
        incoming.created_at = current.created_at
        incoming.revision = current.revision + 1
    else:
        incoming.revision = max(1, incoming.revision)
        incoming.created_at = incoming.created_at or _utcnow()
    incoming.status = NormalizationStatus.DRAFT.value
    incoming.confirmed_at = ""
    incoming.updated_at = _utcnow()
    _validate_structure(incoming)
    result = incoming.to_dict()
    _atomic_write_json(path, result)
    return result


def confirm_normalization(
    normalized_path: Path,
    prepared_path: Path,
    raw_path: Path,
    *,
    memory_path: Path | None = None,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    with _path_lock(normalized_path):
        return _confirm_normalization_unlocked(
            normalized_path,
            prepared_path,
            raw_path,
            memory_path=memory_path,
            expected_revision=expected_revision,
        )


def _confirm_normalization_unlocked(
    normalized_path: Path,
    prepared_path: Path,
    raw_path: Path,
    *,
    memory_path: Path | None = None,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Validate, learn confirmed aliases, and atomically publish projections."""
    normalized_path = Path(normalized_path)
    scenario = NormalizedScenario.from_dict(read_json(normalized_path))
    if expected_revision is not None and scenario.revision != expected_revision:
        raise RevisionConflictError(
            f"Expected revision {expected_revision}, current revision is {scenario.revision}"
        )
    source_path = Path(scenario.source)
    if not source_path.exists() or _file_hash(source_path) != scenario.source_hash:
        raise SourceChangedError("Source scenario changed after normalization")
    errors = validate_for_confirmation(scenario)
    if errors:
        raise NormalizationValidationError(errors)

    scenario.status = NormalizationStatus.CONFIRMED.value
    scenario.revision += 1
    scenario.updated_at = _utcnow()
    scenario.confirmed_at = scenario.updated_at
    prepared = _prepared_projection(scenario)
    raw_text = _raw_projection(scenario)
    _atomic_write_json(Path(prepared_path), prepared)
    _atomic_write_text(Path(raw_path), raw_text)
    result = scenario.to_dict()
    _atomic_write_json(normalized_path, result)
    try:
        _learn_from_confirmation(scenario, memory_path or DEFAULT_MEMORY_PATH)
    except Exception as exc:
        # Publishing the confirmed scenario is authoritative. A memory failure
        # must not roll it back or make downstream projections inconsistent.
        result.setdefault("warnings", []).append(
            f"Normalization memory was not updated: {type(exc).__name__}: {exc}"
        )
    return result


def validate_for_confirmation(scenario: NormalizedScenario | dict[str, Any]) -> list[str]:
    value = scenario if isinstance(scenario, NormalizedScenario) else NormalizedScenario.from_dict(scenario)
    errors: list[str] = []
    try:
        _validate_structure(value)
    except NormalizationValidationError as exc:
        errors.extend(exc.errors)
    for block in value.blocks:
        if block.type == BlockType.UNKNOWN.value:
            errors.append(f"Block {block.order} has unknown type")
        if block.type == BlockType.VOICEOVER.value and not block.text_for_tts.strip():
            errors.append(f"Voiceover block {block.order} has empty TTS text")
    return list(dict.fromkeys(errors))


def is_normalization_confirmed(path: Path, *, source_path: Path | None = None) -> bool:
    try:
        scenario = NormalizedScenario.from_dict(read_json(Path(path)))
    except (OSError, ValueError, TypeError, KeyError):
        return False
    if scenario.status != NormalizationStatus.CONFIRMED.value:
        return False
    actual_source = Path(source_path) if source_path else Path(scenario.source)
    return actual_source.exists() and _file_hash(actual_source) == scenario.source_hash


def import_legacy_normalization(
    prepared_path: Path,
    raw_path: Path,
    output_path: Path,
    *,
    source_path: Path | None = None,
) -> dict[str, Any]:
    """Import legacy prepared/raw files as an explicitly unconfirmed draft."""
    prepared = read_json(Path(prepared_path))
    raw_text = Path(raw_path).read_text(encoding="utf-8") if Path(raw_path).exists() else ""
    source = Path(source_path) if source_path else Path(str(prepared.get("source", raw_path)))
    if not source.exists() and raw_text:
        source = Path(output_path).parent / "scenario_legacy_source.txt"
        _atomic_write_text(source, raw_text)
    source_hash = _file_hash(source) if source.exists() else sha256(raw_text.encode("utf-8")).hexdigest()
    blocks: list[NormalizedBlock] = []
    prepared_items = list(prepared.get("items", []))
    if raw_text.strip():
        from stage2.timeline_parser import parse_timeline

        legacy_timeline = parse_timeline(
            raw_text,
            Path("/__normalization_import_no_audio__"),
            Path("/__normalization_import_no_placeholders__"),
            "legacy-import",
        )
        voiceover_index = 0
        for segment in legacy_timeline.segments:
            order = len(blocks) + 1
            if segment.type == "zk":
                item = prepared_items[voiceover_index] if voiceover_index < len(prepared_items) else {}
                voiceover_index += 1
                original = str(item.get("text_original") or segment.text).strip()
                blocks.append(NormalizedBlock(
                    uid=f"legacy-{order}-{uuid4().hex[:8]}", order=order,
                    original_text=original, normalized_text=original,
                    text_for_tts=str(item.get("text_for_tts") or original).strip(),
                    original_label=f"ЗК{voiceover_index}", label_confirmed=True,
                    type=BlockType.VOICEOVER.value,
                    source=DecisionSource.MANUAL.value, confidence=1.0,
                    notes="imported_from_legacy",
                ))
            elif segment.type == "snh":
                blocks.append(NormalizedBlock(
                    uid=f"legacy-{order}-{uuid4().hex[:8]}", order=order,
                    original_text=segment.text, normalized_text=segment.text,
                    original_label="СНХ", label_confirmed=True, type=BlockType.SYNC.value,
                    source=DecisionSource.MANUAL.value, confidence=1.0,
                    speaker=segment.speaker, role=segment.role,
                    timecode_start=segment.timecode, notes="imported_from_legacy",
                ))
            else:
                label, content = _split_label(segment.text)
                marker_type = _BUILTIN_MARKERS.get(normalize_marker(label))
                block_type = marker_type or BlockType.TECHNICAL.value
                blocks.append(NormalizedBlock(
                    uid=f"legacy-{order}-{uuid4().hex[:8]}", order=order,
                    original_text=segment.text,
                    normalized_text=content if marker_type and content else segment.text,
                    original_label=label, label_confirmed=bool(label), type=block_type,
                    source=DecisionSource.MANUAL.value, confidence=1.0,
                    speaker=label if block_type == BlockType.HOST.value else "",
                    notes="imported_from_legacy",
                ))
        for item in prepared_items[voiceover_index:]:
            order = len(blocks) + 1
            original = str(item.get("text_original", "")).strip()
            blocks.append(NormalizedBlock(
                uid=f"legacy-{order}-{uuid4().hex[:8]}", order=order,
                original_text=original, normalized_text=original,
                text_for_tts=str(item.get("text_for_tts", original)).strip(),
                original_label=f"ЗК{voiceover_index + 1}", label_confirmed=True,
                type=BlockType.VOICEOVER.value,
                source=DecisionSource.MANUAL.value, confidence=1.0,
                notes="imported_from_legacy_missing_in_raw",
            ))
            voiceover_index += 1
    else:
        for item in prepared_items:
            order = len(blocks) + 1
            original = str(item.get("text_original", "")).strip()
            blocks.append(NormalizedBlock(
                uid=f"legacy-{order}-{uuid4().hex[:8]}", order=order,
                original_text=original, normalized_text=original,
                text_for_tts=str(item.get("text_for_tts", original)).strip(),
                original_label=f"ЗК{order}", label_confirmed=True,
                type=BlockType.VOICEOVER.value,
                source=DecisionSource.MANUAL.value, confidence=1.0,
                notes="imported_from_legacy",
            ))
    now = _utcnow()
    scenario = NormalizedScenario(
        source=str(source), source_hash=source_hash, blocks=blocks,
        status=NormalizationStatus.DRAFT.value, created_at=now, updated_at=now,
        warnings=["Imported legacy data requires confirmation"],
    )
    result = scenario.to_dict()
    _atomic_write_json(Path(output_path), result)
    return result


def _extract_paragraphs(input_path: Path) -> list[str]:
    text = extract_text(input_path)
    return [line.strip() for line in text.splitlines() if line.strip()]


def _split_label(text: str) -> tuple[str, str]:
    # Generic labels are deliberately limited: prose containing an ordinary colon
    # should not accidentally become a learned marker.
    if _TIMECODE.match(text.strip()):
        return "", text.strip()
    generic = re.match(r"^\s*([^:–—\n]{1,60}?)\s*[:–—]\s*(.+)$", text)
    if generic:
        candidate = generic.group(1).strip()
        if len(candidate.split()) <= 6:
            return candidate, generic.group(2).strip()
    normalized_text = normalize_marker(text)
    known = sorted(set(_BUILTIN_MARKERS) | _TECH_MARKERS, key=len, reverse=True)
    for marker in known:
        match = re.match(
            rf"^\s*({re.escape(marker)})(?:\s*[-.]?\s*\d+[A-Za-zА-Яа-я]?)?\s*[.:\-–—]?\s*(.*)$",
            text,
            flags=re.I,
        )
        if match and normalize_marker(match.group(1)) == marker:
            return match.group(1).strip(), match.group(2).strip()
    if normalized_text in set(_BUILTIN_MARKERS) | _TECH_MARKERS:
        return text.strip(), ""
    return "", text.strip()


def _classify_by_rules(block: NormalizedBlock, memory: NormalizationMemory) -> None:
    marker = normalize_marker(block.original_label)
    if marker:
        exact = memory.resolve_exact(marker)
        if exact and exact.get("conflict"):
            block.warnings.append(f"Conflicting learned rules for '{block.original_label}'")
            return
        if exact:
            rule = exact["rule"]
            if rule["source"] == "manual":
                _apply_type(block, rule["target_type"], DecisionSource.MEMORY.value, 1.0)
                block.memory_rule_id = int(rule["id"])
                return
        if marker in _BUILTIN_MARKERS:
            _apply_type(block, _BUILTIN_MARKERS[marker], DecisionSource.BUILTIN_RULE.value, 1.0)
            return
        if marker in _TECH_MARKERS:
            _apply_type(block, BlockType.TECHNICAL.value, DecisionSource.BUILTIN_RULE.value, 1.0)
            return
        if exact:
            rule = exact["rule"]
            _apply_type(block, rule["target_type"], DecisionSource.MEMORY.value, float(rule["confidence"]))
            block.memory_rule_id = int(rule["id"])
            return
        suggestion = memory.suggest(marker)
        if suggestion:
            block.suggested_type = str(suggestion["rule"]["target_type"])
            block.suggested_label = str(suggestion["rule"]["marker"])
            block.warnings.append(
                f"Possible learned alias '{block.suggested_label}' ({suggestion['score']:.0%})"
            )
    stripped = block.original_text.strip()
    if re.match(r"^(?:\[?\d{1,2}:\d{2}(?::\d{2})?\]?|\(…\)|\(\.\.\.\))", stripped):
        _apply_type(block, BlockType.SYNC.value, DecisionSource.BUILTIN_RULE.value, 0.9)
    elif stripped.startswith("(") and stripped.endswith(")"):
        _apply_type(block, BlockType.TECHNICAL.value, DecisionSource.BUILTIN_RULE.value, 0.8)


def _apply_type(block: NormalizedBlock, target_type: str, source: str, confidence: float) -> None:
    block.type = target_type if target_type in VALID_BLOCK_TYPES else BlockType.UNKNOWN.value
    block.source = source
    block.confidence = max(0.0, min(1.0, confidence))
    if block.type == BlockType.VOICEOVER.value and not block.text_for_tts:
        block.text_for_tts = block.normalized_text
    timecodes = _TIMECODE.findall(block.original_text)
    if timecodes:
        block.timecode_start = timecodes[0]
        if len(timecodes) > 1:
            block.timecode_end = timecodes[1]


def _extract_block_metadata(block: NormalizedBlock) -> None:
    if block.type == BlockType.HOST.value and not block.speaker:
        block.speaker = block.original_label or "ВЕДУЩИЙ"
    if block.type != BlockType.SYNC.value:
        return
    match = _SYNC_METADATA.match(block.normalized_text)
    if not match:
        return
    block.speaker = match.group(1).strip()
    block.role = match.group(2).strip()
    block.timecode_start = match.group(3).strip()
    remaining = match.group(4).strip()
    if remaining:
        block.normalized_text = remaining


def _apply_llm_results(blocks: list[NormalizedBlock], results: list[dict[str, Any]]) -> None:
    by_uid = {str(item.get("uid", "")): item for item in results}
    for block in blocks:
        item = by_uid.get(block.uid)
        if not item:
            block.warnings.append("LLM did not return a classification for this block")
            continue
        target_type = str(item.get("type", BlockType.UNKNOWN.value))
        if target_type not in VALID_BLOCK_TYPES:
            block.warnings.append(f"LLM returned invalid type '{target_type}'")
            continue
        confidence = float(item.get("confidence", 0.6))
        if confidence < _LLM_CONFIDENCE_THRESHOLD:
            block.source = DecisionSource.LLM.value
            block.confidence = max(0.0, min(1.0, confidence))
            block.warnings.append(
                f"LLM confidence {confidence:.0%} is below the confirmation threshold"
            )
            continue
        _apply_type(block, target_type, DecisionSource.LLM.value, confidence)
        if block.original_label:
            if item.get("label_is_marker"):
                block.label_confirmed = True
                _label, content = _split_label(block.original_text)
                block.normalized_text = content or block.original_text
            else:
                block.original_label = ""
        for key in ("speaker", "role", "timecode_start", "timecode_end", "notes"):
            if item.get(key):
                setattr(block, key, str(item[key]).strip())
        if block.type == BlockType.VOICEOVER.value:
            block.text_for_tts = str(item.get("text_for_tts", "")).strip() or block.normalized_text
        _extract_block_metadata(block)


def _llm_batches(
    blocks: list[NormalizedBlock], max_chars: int = 12_000
) -> list[list[NormalizedBlock]]:
    batches: list[list[NormalizedBlock]] = []
    current: list[NormalizedBlock] = []
    current_size = 0
    for block in blocks:
        block_size = len(json.dumps(_llm_input(block), ensure_ascii=False))
        if current and current_size + block_size > max_chars:
            batches.append(current)
            current = []
            current_size = 0
        current.append(block)
        current_size += block_size
    if current:
        batches.append(current)
    return batches


def _llm_input(block: NormalizedBlock) -> dict[str, Any]:
    """Return only classification context, avoiding duplicated draft fields."""
    return {
        "uid": block.uid,
        "original_text": block.original_text,
        "original_label": block.original_label,
        "suggested_type": block.suggested_type,
        "suggested_label": block.suggested_label,
    }


def _openrouter_classifier(blocks: list[dict[str, Any]], hint: str) -> list[dict[str, Any]]:
    settings = get_settings()
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("requests is not installed") from exc
    prompt = (
        "Classify every scenario paragraph without deleting or merging any item. "
        "Allowed types: voiceover, sync, host, technical, other, unknown. "
        "Return JSON {\"items\":[{\"uid\":...,\"type\":...,\"confidence\":0..1,"
        "\"label_is_marker\":true|false,"
        "\"text_for_tts\":...,\"speaker\":...,\"role\":...,"
        "\"timecode_start\":...,\"timecode_end\":...,\"notes\":...}]}. "
        "Use unknown when unsure. Do not rewrite original content. "
        f"Author hint: {hint or '(none)'}. Items: {json.dumps(blocks, ensure_ascii=False)}"
    )
    request_kwargs = {
        "headers": {"Authorization": f"Bearer {settings.openrouter_api_key}", "Content-Type": "application/json"},
        "json": {
            "model": settings.openrouter_model,
            "messages": [{"role": "system", "content": "You normalize TV production scripts."},
                         {"role": "user", "content": prompt}],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        },
        "timeout": 120,
    }
    response = None
    for attempt in range(3):
        try:
            response = requests.post(
                f"{settings.openrouter_base_url}/chat/completions", **request_kwargs
            )
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
            continue
        if response.status_code not in {408, 429, 500, 502, 503, 504} or attempt == 2:
            break
        time.sleep(2 ** attempt)
    assert response is not None
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    content = content.strip()
    if not content.startswith("{"):
        start, end = content.find("{"), content.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("OpenRouter response does not contain a JSON object")
        content = content[start:end + 1]
    parsed = json.loads(content)
    items = parsed.get("items", [])
    if not isinstance(items, list):
        raise ValueError("OpenRouter response does not contain an items array")
    return items


def _validate_structure(scenario: NormalizedScenario) -> None:
    errors: list[str] = []
    if not scenario.source or not scenario.source_hash:
        errors.append("Scenario source identity is missing")
    seen: set[str] = set()
    for expected_order, block in enumerate(scenario.blocks, start=1):
        if not block.uid or block.uid in seen:
            errors.append(f"Block {expected_order} has missing or duplicate uid")
        seen.add(block.uid)
        if block.order != expected_order:
            errors.append("Block order must be consecutive and match list order")
        if not block.original_text.strip():
            errors.append(f"Block {expected_order} has empty original text")
        if not block.normalized_text.strip():
            errors.append(f"Block {expected_order} has empty normalized text")
        if block.type not in VALID_BLOCK_TYPES:
            errors.append(f"Block {expected_order} has invalid type '{block.type}'")
    if errors:
        raise NormalizationValidationError(errors)


def _prepared_projection(scenario: NormalizedScenario) -> dict[str, Any]:
    voiceovers = [block for block in scenario.blocks if block.type == BlockType.VOICEOVER.value]
    return {
        "source": scenario.source,
        "used_llm": any(block.source == DecisionSource.LLM.value for block in scenario.blocks),
        "manual_markers_used": any(block.source == DecisionSource.MANUAL.value for block in scenario.blocks),
        "extraction_method": "normalization",
        "normalization_revision": scenario.revision,
        "items": [
            {
                "id": f"zk{index}",
                "text_original": block.normalized_text,
                "text_for_tts": block.text_for_tts,
                "pause_ms": 300,
                "notes": block.notes,
            }
            for index, block in enumerate(voiceovers, start=1)
        ],
    }


def _raw_projection(scenario: NormalizedScenario) -> str:
    counters: dict[str, int] = {}
    prefixes = {
        BlockType.VOICEOVER.value: "ЗК", BlockType.SYNC.value: "СНХ",
        BlockType.HOST.value: "ВЕДУЩИЙ", BlockType.TECHNICAL.value: "ТЕХ",
        BlockType.OTHER.value: "ПРОЧЕЕ", BlockType.UNKNOWN.value: "НЕИЗВЕСТНО",
    }
    lines: list[str] = []
    for block in scenario.blocks:
        counters[block.type] = counters.get(block.type, 0) + 1
        prefix = prefixes[block.type]
        lines.append(f"{prefix}{counters[block.type]}: {block.normalized_text}")
    return "\n".join(lines) + "\n"


def _file_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _path_lock(path: Path) -> threading.RLock:
    key = str(Path(path).resolve())
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


def _learn_from_confirmation(scenario: NormalizedScenario, memory_path: Path) -> None:
    with NormalizationMemory(memory_path) as memory:
        for block in scenario.blocks:
            if not block.original_label or not block.label_confirmed or block.type in {
                BlockType.UNKNOWN.value,
                BlockType.OTHER.value,
            }:
                continue
            if block.source == DecisionSource.MANUAL.value:
                if block.memory_rule_id:
                    try:
                        memory.record_correction(block.memory_rule_id)
                    except KeyError:
                        pass
                memory.record_manual(block.original_label, block.type)
            elif block.source == DecisionSource.LLM.value:
                memory.record_llm_confirmation(
                    block.original_label, block.type, scenario.source_hash
                )


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
