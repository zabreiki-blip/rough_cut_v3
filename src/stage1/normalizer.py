from __future__ import annotations

from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict, deque
from hashlib import sha256
import json
import logging
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from atomic_io import atomic_write_json as _atomic_write_json
from atomic_io import atomic_write_text as _atomic_write_text

from .config import get_settings
from .io_utils import extract_paragraphs, read_json
from .normalization_memory import DEFAULT_MEMORY_PATH, NormalizationMemory, normalize_marker
from .normalization_models import (
    BlockType,
    DecisionSource,
    NormalizationStatus,
    NormalizedBlock,
    NormalizedScenario,
    SyncFragment,
    VALID_BLOCK_TYPES,
    VALID_DECISION_SOURCES,
)

logger = logging.getLogger(__name__)


LLMClassifier = Callable[[list[dict[str, Any]], str], list[dict[str, Any]]]
LLM_PRESETS = {"standard", "fast"}


def normalize_llm_preset(value: str | None) -> str:
    """Return a supported, request-local OpenRouter routing preset."""
    preset = str(value or "standard").strip().lower()
    if preset not in LLM_PRESETS:
        raise ValueError("llm_preset must be 'standard' or 'fast'")
    return preset


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
    "з к": BlockType.VOICEOVER.value,
    "з": BlockType.VOICEOVER.value,
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
    "видео",
    "лайф",
    "титр",
    "титры",
    "плашка",
    "графика",
    "карта",
    "фото",
    "архив",
    "отбивка",
    "шапка",
    "склейка",
    "ремарка",
    "техническое",
}
_TIMECODE_TOKEN = (
    r"(?:\[?\d{1,2}[:.]\d{2}(?:\s+|[:.])\d{2}\]?|"
    r"\[?\d{1,2}[:.]\d{2}\]?|\[?\d{1,2}-\d{2}\]?)"
)
_TIMECODE = re.compile(rf"({_TIMECODE_TOKEN})")
_SYNC_METADATA = re.compile(
    r"^([А-ЯЁA-Z][А-ЯЁа-яёA-Za-z\s\-]{1,60}),\s*"
    rf"(.{{2,160}}?)(?:,\s*|:\s*|\s+)({_TIMECODE_TOKEN})"
    r"[.\s–\-]*(.*)$",
    re.DOTALL,
)
_SYNC_LABEL_METADATA = re.compile(r"^([А-ЯЁA-Z][А-ЯЁа-яёA-Za-z\s\-]{1,60}),\s*(.{2,160})$")
_SYNC_NAME_ONLY_LABEL = re.compile(
    r"^[А-ЯЁA-Z][А-ЯЁа-яёA-Za-z\-]+(?:\s+[А-ЯЁA-Z][А-ЯЁа-яёA-Za-z\-]+)*$"
)
_SYNC_CONTINUATION_NOTE = re.compile(r"^\s*\((?:продолжение(?:\s+синхрона?)?)\)\s*", re.IGNORECASE)
_SYNC_FRAGMENT = re.compile(
    rf"(?P<start>{_TIMECODE_TOKEN})"
    rf"(?:\s*[-–—]\s*(?P<end>{_TIMECODE_TOKEN}))?"
    rf"[.\s]*(?P<text>.*?)(?=(?:{_TIMECODE_TOKEN})|$)",
    re.DOTALL,
)
_EDITORIAL_TIMECODE_PREFIX = re.compile(
    rf"^\s*\((?:[^)\n]{{1,240}}\)\s*:?\s*|[^:\n]{{1,240}}:\s*)"
    rf"{_TIMECODE_TOKEN}",
    re.DOTALL,
)
_VOICEOVER_ZK_PREFIX = re.compile(
    r"^\s*(?P<label>з\s*\.?\s*[кК]\.?)"
    r"(?:(?=\s)|(?<=\.)(?=[А-ЯЁA-Z«(])|\s*[:\-–—]\s*|$)"
    r"(?P<content>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_VOICEOVER_ZK_GLUED_PREFIX = re.compile(
    r"^\s*(?P<label>з\s*\.\s*[кК]\.?)"
    r"(?=[А-ЯЁA-Z«(])(?P<content>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_VOICEOVER_Z_PREFIX = re.compile(
    r"^\s*(?P<label>з\.?)"
    r"(?:\s+|\s*[:\-–—]\s*|$)"
    r"(?P<content>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_BARE_SPEAKER_PREFIX = re.compile(
    r"^\s*(?P<speaker>(?:[А-ЯЁ][а-яё]+\s+)?[А-ЯЁ]{3,})"
    r"(?:[.:\-–—]?\s+|[.:](?=[А-ЯЁ])|(?=[а-яё]))(?P<content>.+)$",
    re.DOTALL,
)
_STRUCTURAL_HEADING = re.compile(
    r"^(?:часть\b|глава\b|заставка\b|эпизод\b|\d+\s*\.\s*$)",
    re.IGNORECASE,
)
# Seed lexicon for an unmarked "video cue" paragraph: a shot-list remark
# describing what footage should be shown on screen (portraits, busts,
# archival photos, drone/copter passes, etc.) rather than spoken narration.
# This is a hand-picked starting list; it is deliberately kept as a plain
# module constant so it can later be promoted into a learnable
# NormalizationMemory rule instead of a hardcoded regex.
_VIDEO_CUE_KEYWORDS = (
    "планы",
    "план",
    "портреты",
    "портрет",
    "бюст",
    "камень",
    "интерьер",
    "экстерьер",
    "детали",
    "деталь",
    "крупный план",
    "панорама",
    "проход",
    "коптер",
    "дрон",
    "архив",
    "фотографии",
    "фотография",
    "фото",
    "съёмка",
    "съемка",
    "кадры",
    "кадр",
    "виды",
    "вид",
)
_VIDEO_CUE_KEYWORD_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(word) for word in sorted(_VIDEO_CUE_KEYWORDS, key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)


# _seed_video_cue_rules is idempotent at the database level (record_manual's
# upsert never regresses a rule a user already disabled and is a no-op once
# a keyword is already active), but generate_normalization() runs it once
# per *document*, and every call re-issues all 24 upserts even though the
# rows never change after the first run. Skip the redundant writes with a
# process-local "already seeded" cache keyed by the memory database's
# resolved path -- not a single process-wide flag -- so unrelated memory
# databases (e.g. per-project or per-test tmp DBs opened in the same
# process) each still get seeded on their own first use. Guarded by a lock
# because generate_normalization can run concurrently from worker threads.
_video_cue_seeded_db_paths: set[str] = set()
_video_cue_seed_lock = threading.Lock()


def invalidate_video_cue_seed_cache(db_path: Path | str | None = None) -> None:
    """Forget that ``db_path`` (or, with no argument, every database) has
    already been seeded, so the next ``_seed_video_cue_rules`` call for it
    re-issues the upserts.

    Needed whenever something other than ``_seed_video_cue_rules`` itself
    can undo what seeding is responsible for -- most notably a user
    deleting a scope='video_cue' rule via the memory API: without
    invalidating the cache, the deleted rule would only come back after a
    process restart (when the in-memory "already seeded" set is naturally
    reset), making the API's behavior depend on process lifetime instead of
    being deterministic.
    """
    with _video_cue_seed_lock:
        if db_path is None:
            _video_cue_seeded_db_paths.clear()
            return
        _video_cue_seeded_db_paths.discard(str(Path(db_path).resolve()))


def _seed_video_cue_rules(memory: NormalizationMemory) -> None:
    """Seed the built-in video-cue lexicon into scope='video_cue' memory
    rules. Idempotent: ``record_manual``'s upsert never regresses a rule a
    user already disabled and is a no-op once a keyword is already active,
    so calling this once per normalization run never creates duplicates or
    overrides manual edits. Skipped entirely once this database has already
    been seeded in this process (see ``_video_cue_seeded_db_paths`` above).
    """
    cache_key = str(memory.path.resolve())
    with _video_cue_seed_lock:
        if cache_key in _video_cue_seeded_db_paths:
            return
        for keyword in _VIDEO_CUE_KEYWORDS:
            memory.record_manual(keyword, BlockType.TECHNICAL.value, scope="video_cue")
        _video_cue_seeded_db_paths.add(cache_key)


def _video_cue_keyword_pattern(memory: NormalizationMemory | None) -> re.Pattern[str]:
    """Build the effective video-cue keyword regex for a normalization run:
    the built-in seed plus any active learned keywords, minus anything a
    user disabled. Meant to be computed once per run (not per block) and
    passed down explicitly, since it requires a memory query.

    Falls back to the built-in-only pattern when memory is unavailable or
    the lookup fails, so normalization never breaks on a memory error.
    """
    if memory is None:
        return _VIDEO_CUE_KEYWORD_RE
    try:
        _seed_video_cue_rules(memory)
        rules = memory.list_rules(scope="video_cue")
    except Exception:
        logger.warning(
            "Failed to load learned video-cue keywords; using the built-in seed only",
            exc_info=True,
        )
        return _VIDEO_CUE_KEYWORD_RE
    active = {str(rule["marker"]).strip() for rule in rules if rule["status"] == "active"}
    disabled = {str(rule["marker"]).strip() for rule in rules if rule["status"] == "disabled"}
    keywords = ((set(_VIDEO_CUE_KEYWORDS) | active) - disabled) - {""}
    if keywords == set(_VIDEO_CUE_KEYWORDS):
        return _VIDEO_CUE_KEYWORD_RE
    return re.compile(
        r"\b(?:"
        + "|".join(re.escape(word) for word in sorted(keywords, key=len, reverse=True))
        + r")\b",
        re.IGNORECASE,
    )


# Coarse heuristic for a Russian personal (finite) verb form: past tense
# (-л/-ла/-ло/-ли, incl. reflexive -лся/-лась/-лись) and present/future
# 3rd-person and plural endings. It is intentionally approximate: a false
# positive here only keeps the safer voiceover classification.
_PERSONAL_VERB_RE = re.compile(
    r"\b[а-яё]{3,}(?:лся|лась|лись|ла|ло|ли|л|ете|ите|ешь|ишь|ют|ат|ят|ет|ит|ем|им)\b",
    re.IGNORECASE,
)
_VIDEO_CUE_MAX_WORDS = 12
# An unmarked, all-caps first paragraph of a scenario is almost always the
# film/scenario title (e.g. "БЕЛАРУСЬ. САРЬЯ. ХРАМ ВО ИМЯ ЛЮБВИ ИГНАТИЯ
# ЛОПАТИНСКОГО"), not spoken narration. It must never reach TTS or the
# interview search, so it belongs in "technical" like production credits.
# At least one Cyrillic/Latin letter is required so punctuation-only text
# does not match; the personal-verb check (shared with _looks_like_video_cue)
# keeps an all-caps opening line of narration from being misclassified.
_SCENARIO_TITLE_RE = re.compile(r"^[^а-яa-z]*[А-ЯЁA-Z][^а-яa-z]*$")
# A real scenario title is a short phrase (e.g. the 8-word/55-char example
# above); generous caps well above that keep long all-caps narration or
# a single very long run of caps (no spaces at all) from being mistaken
# for a title. Both bounds are needed: a single run-on "word" with no
# spaces would otherwise pass a word-count-only check.
_SCENARIO_TITLE_MAX_WORDS = 20
_SCENARIO_TITLE_MAX_CHARS = 120
_LLM_CONFIDENCE_THRESHOLD = 0.65
_LLM_CACHE_VERSION = 1
_LLM_CACHE_MAX_ENTRIES = 2000
_OPENROUTER_SYSTEM_PROMPT = "You normalize TV production scripts."
_OPENROUTER_INSTRUCTIONS = (
    "Classify every scenario paragraph without deleting or merging any item. "
    "Allowed types: voiceover, sync, host, technical, other, unknown. "
    'Return JSON {"items":[{"uid":...,"type":...,"confidence":0..1,'
    '"label_is_marker":true|false,'
    '"text_for_tts":...,"speaker":...,"role":...,'
    '"timecode_start":...,"timecode_end":...,"notes":...}]}. '
    "Use unknown when unsure. Do not rewrite original content. "
    "An item with continuation_of_type=voiceover is an unlabelled paragraph "
    "continuing the preceding voiceover unless its text clearly starts a new "
    "speaker or technical cue. "
)
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
    llm_preset: str = "standard",
) -> dict[str, Any]:
    llm_preset = normalize_llm_preset(llm_preset)
    with _path_lock(output_path):
        return _generate_normalization_unlocked(
            input_path,
            output_path,
            memory_path=memory_path,
            use_llm=use_llm,
            hint=hint,
            classifier=classifier,
            llm_preset=llm_preset,
        )


def _generate_normalization_unlocked(
    input_path: Path,
    output_path: Path,
    *,
    memory_path: Path | None = None,
    use_llm: bool = True,
    hint: str = "",
    classifier: LLMClassifier | None = None,
    llm_preset: str = "standard",
) -> dict[str, Any]:
    """Create and persist a complete, ordered normalization draft."""
    input_path = Path(input_path)
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")
    output_path = Path(output_path)
    previous: NormalizedScenario | None = None
    if output_path.is_file():
        try:
            previous = NormalizedScenario.from_dict(read_json(output_path))
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            previous = None
    logger.info("Normalization started source=%s llm=%s", input_path, use_llm)
    source_hash = _file_hash(input_path)
    if (
        previous is not None
        and previous.scenario_revision_id
        and previous.source_hash != source_hash
    ):
        raise SourceChangedError(
            "Active scenario revision cannot be replaced by another source; "
            "import it as a new scenario revision"
        )
    paragraphs = _extract_paragraphs(input_path)
    now = _utcnow()
    blocks: list[NormalizedBlock] = []

    with NormalizationMemory(memory_path or DEFAULT_MEMORY_PATH) as memory:
        # Computed once per run (not per block): building it requires a
        # memory query, and the seed keyword list rarely changes mid-run.
        video_cue_re = _video_cue_keyword_pattern(memory)
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
            _classify_by_rules(block, memory, video_cue_re)
            if block.type != BlockType.UNKNOWN.value and block.original_label:
                block.label_confirmed = True
                if content:
                    block.normalized_text = content
                elif block.type == BlockType.VOICEOVER.value:
                    # A marker-only paragraph (very common as ``З.к.`` on its
                    # own line) is a production cue for the following block,
                    # not text that should ever reach TTS.
                    _apply_type(
                        block,
                        BlockType.TECHNICAL.value,
                        DecisionSource.BUILTIN_RULE.value,
                        1.0,
                    )
                    block.notes = "section_marker_for=voiceover"
                    block.text_for_tts = ""
            _extract_block_metadata(block)
            if block.type == BlockType.VOICEOVER.value:
                block.text_for_tts = block.normalized_text
            blocks.append(block)

        _annotate_continuation_context(blocks, video_cue_re)
        unresolved = [block for block in blocks if block.type == BlockType.UNKNOWN.value]
        if unresolved and use_llm:
            if classifier is not None:
                llm = classifier
            elif llm_preset == "standard":
                # Keep the legacy call shape: integrations and tests may patch
                # this classifier with a two-argument callable.
                llm = _openrouter_classifier
            else:
                llm = lambda batch, author_hint: _openrouter_classifier(
                    batch, author_hint, llm_preset=llm_preset
                )
            batches = _llm_batches(unresolved)
            payloads = [[_llm_input(block) for block in batch] for batch in batches]
            cache_path = _llm_cache_path(Path(output_path))
            cache = _load_llm_cache(cache_path)
            rules_signature = _llm_rules_signature(memory)
            cache_keys = [
                _llm_cache_fingerprint(
                    payload,
                    hint=hint,
                    classifier=classifier,
                    llm_preset=llm_preset,
                    rules_signature=rules_signature,
                )
                for payload in payloads
            ]
            batch_results: list[tuple[list[dict[str, Any]] | None, Exception | None]] = [
                (None, None) for _ in batches
            ]
            pending: list[int] = []
            for index, (payload, cache_key) in enumerate(zip(payloads, cache_keys)):
                cached = _llm_cached_results(cache, cache_key, payload)
                if cached is None:
                    pending.append(index)
                else:
                    batch_results[index] = (cached, None)

            # A large documentary script can contain hundreds of unresolved
            # paragraphs. OpenRouter calls are independent, so bounded
            # concurrency keeps the HTTP request practical without flooding the
            # provider. Injected classifiers remain sequential for deterministic
            # tests and custom integrations.
            if classifier is None and len(pending) > 1:
                with ThreadPoolExecutor(max_workers=min(4, len(pending))) as pool:
                    futures = {pool.submit(llm, payloads[index], hint): index for index in pending}
                    for future in as_completed(futures):
                        index = futures[future]
                        try:
                            results = future.result()
                            batch_results[index] = (results, None)
                            _cache_llm_results(
                                cache_path,
                                cache,
                                cache_keys[index],
                                payloads[index],
                                results,
                            )
                        except Exception as exc:
                            logger.exception("LLM batch %d classification failed", index)
                            batch_results[index] = (None, exc)
            else:
                for index in pending:
                    try:
                        results = llm(payloads[index], hint)
                        batch_results[index] = (results, None)
                        _cache_llm_results(
                            cache_path,
                            cache,
                            cache_keys[index],
                            payloads[index],
                            results,
                        )
                    except Exception as exc:
                        logger.exception("LLM batch %d classification failed", index)
                        batch_results[index] = (None, exc)

            for batch, (batch_result, error) in zip(batches, batch_results):
                if error is None:
                    _apply_llm_results(batch, batch_result or [])
                    continue
                message = f"LLM classification failed: {type(error).__name__}: {error}"
                for block in batch:
                    block.warnings.append(message)

    _snapshot_proposals(blocks)
    scenario = NormalizedScenario(
        source=str(input_path),
        source_hash=source_hash,
        blocks=blocks,
        hint=hint.strip(),
        created_at=now,
        updated_at=now,
    )
    if previous is not None and previous.source_hash == source_hash:
        scenario.revision = previous.revision + 1
        scenario.scenario_revision_id = previous.scenario_revision_id
        scenario.parent_revision_id = previous.parent_revision_id
        previous_by_text: dict[str, deque[NormalizedBlock]] = defaultdict(deque)
        for block in previous.blocks:
            previous_by_text[block.original_text].append(block)
        for block in scenario.blocks:
            candidates = previous_by_text.get(block.original_text)
            if not candidates:
                continue
            old = candidates.popleft()
            block.uid = old.uid
            block.versioning = dict(old.versioning)
    scenario_payload = scenario.to_dict()
    _atomic_write_json(Path(output_path), scenario_payload)
    logger.info("Normalization draft written source=%s blocks=%d", input_path, len(blocks))
    return scenario_payload


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
        return _save_normalization_unlocked(path, payload, expected_revision=expected_revision)


def _save_normalization_unlocked(
    path: Path,
    payload: dict[str, Any],
    *,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Save user edits as a new draft revision, preserving source identity."""
    path = Path(path)
    current = NormalizedScenario.from_dict(read_json(path)) if path.exists() else None
    if (
        current is not None
        and expected_revision is not None
        and current.revision != expected_revision
    ):
        raise RevisionConflictError(
            f"Expected revision {expected_revision}, current revision is {current.revision}"
        )
    incoming = NormalizedScenario.from_dict(payload)
    if current is not None:
        if incoming.source_hash != current.source_hash or incoming.source != current.source:
            raise SourceChangedError(
                "Source and source_hash cannot be changed while editing a draft"
            )
        previous = {block.uid: block for block in current.blocks}
        for block in incoming.blocks:
            old = previous.get(block.uid)
            if old is not None:
                block.proposed_type = old.proposed_type
                block.proposed_label = old.proposed_label
                block.proposed_label_confirmed = old.proposed_label_confirmed
                block.proposed_source = old.proposed_source
                block.proposed_memory_rule_id = old.proposed_memory_rule_id
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
                block.type != old.type or block.original_label != old.original_label
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
    learning_decisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    with _path_lock(normalized_path):
        return _confirm_normalization_unlocked(
            normalized_path,
            prepared_path,
            raw_path,
            memory_path=memory_path,
            expected_revision=expected_revision,
            learning_decisions=learning_decisions,
        )


def _confirm_normalization_unlocked(
    normalized_path: Path,
    prepared_path: Path,
    raw_path: Path,
    *,
    memory_path: Path | None = None,
    expected_revision: int | None = None,
    learning_decisions: list[dict[str, Any]] | None = None,
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

    resolved_memory_path = memory_path or DEFAULT_MEMORY_PATH
    with NormalizationMemory(resolved_memory_path) as memory:
        learning_candidates = _infer_learning_candidates(scenario, memory)
    decisions = _validate_learning_decisions(learning_candidates, learning_decisions)

    scenario.status = NormalizationStatus.CONFIRMED.value
    scenario.revision += 1
    scenario.updated_at = _utcnow()
    scenario.confirmed_at = scenario.updated_at
    prepared = _prepared_projection(scenario)
    raw_text = _raw_projection(scenario)
    _snapshot_proposals(scenario.blocks)
    _atomic_write_json(Path(prepared_path), prepared)
    _atomic_write_text(Path(raw_path), raw_text)
    result = scenario.to_dict()
    _atomic_write_json(normalized_path, result)
    logger.info(
        "Normalization confirmed source=%s revision=%s blocks=%d",
        scenario.source,
        scenario.revision,
        len(scenario.blocks),
    )
    try:
        _learn_from_confirmation(scenario, resolved_memory_path, learning_candidates, decisions)
    except Exception as exc:
        # Publishing the confirmed scenario is authoritative. A memory failure
        # must not roll it back or make downstream projections inconsistent.
        result.setdefault("warnings", []).append(
            f"Normalization memory was not updated: {type(exc).__name__}: {exc}"
        )
    return result


def preview_learning_candidates(
    normalized_path: Path,
    *,
    memory_path: Path | None = None,
    expected_revision: int | None = None,
) -> list[dict[str, Any]]:
    with _path_lock(normalized_path):
        scenario = NormalizedScenario.from_dict(read_json(Path(normalized_path)))
        if expected_revision is not None and scenario.revision != expected_revision:
            raise RevisionConflictError(
                f"Expected revision {expected_revision}, current revision is {scenario.revision}"
            )
        with NormalizationMemory(memory_path or DEFAULT_MEMORY_PATH) as memory:
            return _infer_learning_candidates(scenario, memory)


def validate_for_confirmation(scenario: NormalizedScenario | dict[str, Any]) -> list[str]:
    value = (
        scenario
        if isinstance(scenario, NormalizedScenario)
        else NormalizedScenario.from_dict(scenario)
    )
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
    source_hash = (
        _file_hash(source) if source.exists() else sha256(raw_text.encode("utf-8")).hexdigest()
    )
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
                item = (
                    prepared_items[voiceover_index] if voiceover_index < len(prepared_items) else {}
                )
                voiceover_index += 1
                original = str(item.get("text_original") or segment.text).strip()
                blocks.append(
                    NormalizedBlock(
                        uid=f"legacy-{order}-{uuid4().hex[:8]}",
                        order=order,
                        original_text=original,
                        normalized_text=original,
                        text_for_tts=str(item.get("text_for_tts") or original).strip(),
                        original_label=f"ЗК{voiceover_index}",
                        label_confirmed=True,
                        type=BlockType.VOICEOVER.value,
                        source=DecisionSource.MANUAL.value,
                        confidence=1.0,
                        notes="imported_from_legacy",
                    )
                )
            elif segment.type == "snh":
                blocks.append(
                    NormalizedBlock(
                        uid=f"legacy-{order}-{uuid4().hex[:8]}",
                        order=order,
                        original_text=segment.text,
                        normalized_text=segment.text,
                        original_label="СНХ",
                        label_confirmed=True,
                        type=BlockType.SYNC.value,
                        source=DecisionSource.MANUAL.value,
                        confidence=1.0,
                        speaker=segment.speaker,
                        role=segment.role,
                        timecode_start=segment.timecode,
                        notes="imported_from_legacy",
                    )
                )
            else:
                label, content = _split_label(segment.text)
                marker_type = _BUILTIN_MARKERS.get(normalize_marker(label))
                block_type = marker_type or BlockType.TECHNICAL.value
                blocks.append(
                    NormalizedBlock(
                        uid=f"legacy-{order}-{uuid4().hex[:8]}",
                        order=order,
                        original_text=segment.text,
                        normalized_text=content if marker_type and content else segment.text,
                        original_label=label,
                        label_confirmed=bool(label),
                        type=block_type,
                        source=DecisionSource.MANUAL.value,
                        confidence=1.0,
                        speaker=label if block_type == BlockType.HOST.value else "",
                        notes="imported_from_legacy",
                    )
                )
        for item in prepared_items[voiceover_index:]:
            order = len(blocks) + 1
            original = str(item.get("text_original", "")).strip()
            blocks.append(
                NormalizedBlock(
                    uid=f"legacy-{order}-{uuid4().hex[:8]}",
                    order=order,
                    original_text=original,
                    normalized_text=original,
                    text_for_tts=str(item.get("text_for_tts", original)).strip(),
                    original_label=f"ЗК{voiceover_index + 1}",
                    label_confirmed=True,
                    type=BlockType.VOICEOVER.value,
                    source=DecisionSource.MANUAL.value,
                    confidence=1.0,
                    notes="imported_from_legacy_missing_in_raw",
                )
            )
            voiceover_index += 1
    else:
        for item in prepared_items:
            order = len(blocks) + 1
            original = str(item.get("text_original", "")).strip()
            blocks.append(
                NormalizedBlock(
                    uid=f"legacy-{order}-{uuid4().hex[:8]}",
                    order=order,
                    original_text=original,
                    normalized_text=original,
                    text_for_tts=str(item.get("text_for_tts", original)).strip(),
                    original_label=f"ЗК{order}",
                    label_confirmed=True,
                    type=BlockType.VOICEOVER.value,
                    source=DecisionSource.MANUAL.value,
                    confidence=1.0,
                    notes="imported_from_legacy",
                )
            )
    now = _utcnow()
    _snapshot_proposals(blocks)
    scenario = NormalizedScenario(
        source=str(source),
        source_hash=source_hash,
        blocks=blocks,
        status=NormalizationStatus.DRAFT.value,
        created_at=now,
        updated_at=now,
        warnings=["Imported legacy data requires confirmation"],
    )
    result = scenario.to_dict()
    _atomic_write_json(Path(output_path), result)
    return result


def _extract_paragraphs(input_path: Path) -> list[str]:
    return extract_paragraphs(input_path)


def _snapshot_proposals(blocks: list[NormalizedBlock]) -> None:
    for block in blocks:
        block.proposed_type = block.type
        block.proposed_label = block.original_label
        block.proposed_label_confirmed = block.label_confirmed
        block.proposed_source = block.source
        block.proposed_memory_rule_id = block.memory_rule_id


def _annotate_continuation_context(
    blocks: list[NormalizedBlock], video_cue_re: re.Pattern[str] = _VIDEO_CUE_KEYWORD_RE
) -> None:
    previous: NormalizedBlock | None = None
    previous_sync: NormalizedBlock | None = None
    for block in blocks:
        if (
            previous is not None
            and block.type == BlockType.UNKNOWN.value
            and not block.original_label
            and previous.type == BlockType.SYNC.value
            and _looks_like_sync_continuation(block.original_text)
        ):
            block.continuation_of_type = BlockType.SYNC.value
            if not block.suggested_type:
                block.suggested_type = BlockType.SYNC.value
            _apply_type(
                block,
                BlockType.SYNC.value,
                DecisionSource.BUILTIN_RULE.value,
                0.9,
            )
            block.speaker = previous.speaker
            block.role = previous.role
        elif (
            previous is not None
            and block.type == BlockType.UNKNOWN.value
            and not block.original_label
            and (
                previous.type == BlockType.VOICEOVER.value
                or previous.notes == "section_marker_for=voiceover"
            )
            # A footage/shot-list remark must not inherit voiceover just
            # because it follows one without its own marker (see
            # _looks_like_video_cue).
            and not _looks_like_video_cue(block.original_text, video_cue_re)
        ):
            block.continuation_of_type = BlockType.VOICEOVER.value
            if not block.suggested_type:
                block.suggested_type = BlockType.VOICEOVER.value
            # A plain paragraph after an explicitly recognized voiceover marker
            # is part of the same voiceover section. Classify this locally so a
            # paragraph boundary does not make the result depend on the LLM.
            _apply_type(
                block,
                BlockType.VOICEOVER.value,
                DecisionSource.BUILTIN_RULE.value,
                0.9,
            )
        # A scenario editor often marks the next quote only as "СНХ" (or
        # leaves a bare timecode) after naming the interviewee once. Treat it
        # as another independently searchable edit from that interview, not as
        # a chronological continuation of the previous source range.
        if block.type == BlockType.SYNC.value:
            if block.speaker:
                previous_sync = block
            elif previous_sync is not None:
                block.continuation_of_type = BlockType.SYNC.value
                block.speaker = previous_sync.speaker
                block.role = previous_sync.role
        previous = block


def _looks_like_sync_continuation(text: str) -> bool:
    return bool(re.match(r"^(?:\.{3}|…|[a-zа-яё]|И(?:\s|$))", text.lstrip()))


def _looks_like_video_cue(text: str, video_cue_re: re.Pattern[str] = _VIDEO_CUE_KEYWORD_RE) -> bool:
    """Detect an unmarked "video cue": a shot-list remark describing footage
    to show on screen (e.g. "Мраморный бюст Марии"), not spoken narration.

    Paired with :func:`_looks_like_sync_continuation`: a short nominal
    phrase naming what to show, containing no personal (finite) verb form.
    The verb/length check is the primary signal; the keyword lexicon only
    narrows it to paragraphs that plausibly describe footage at all.
    ``video_cue_re`` defaults to the built-in seed pattern but callers with
    a :class:`NormalizationMemory` should pass the pattern produced by
    :func:`_video_cue_keyword_pattern`, computed once per normalization run.
    """
    stripped = text.strip()
    if not stripped or not video_cue_re.search(stripped):
        return False
    if _PERSONAL_VERB_RE.search(stripped):
        return False
    if len(stripped.split()) > _VIDEO_CUE_MAX_WORDS:
        return False
    return True


def _looks_like_scenario_title(text: str) -> bool:
    """Detect an unmarked scenario/film title paragraph: fully upper-case
    text (dots, dashes, spaces and quotes allowed) with no personal (finite)
    verb form. Intended to be applied only to the first block of a scenario;
    callers are responsible for that position check.
    """
    stripped = text.strip()
    if not stripped or not _SCENARIO_TITLE_RE.match(stripped):
        return False
    if len(stripped) > _SCENARIO_TITLE_MAX_CHARS:
        return False
    if len(stripped.split()) > _SCENARIO_TITLE_MAX_WORDS:
        return False
    return not _PERSONAL_VERB_RE.search(stripped)


def _starts_with_unambiguous_timecode(text: str) -> bool:
    """Recognize a bare timecode without mistaking a prose-leading date for one."""
    stripped = text.lstrip()
    parenthesized = re.match(rf"^\(\s*({_TIMECODE_TOKEN})\s*\)", stripped)
    if parenthesized:
        return True

    match = _TIMECODE.match(stripped)
    if not match:
        return False
    token = match.group(1)
    inner = token.strip("[]")
    if token.startswith("[") or ":" in inner or inner.count(".") >= 2 or "-" in inner:
        return True

    # Two-component dot notation (for example 23.05) is ambiguous with a date.
    # Treat it as a timecode only when an explicit range removes that ambiguity.
    remainder = stripped[match.end() :]
    return bool(re.match(rf"^\s*[-–—]\s*{_TIMECODE_TOKEN}", remainder))


def _split_label(text: str) -> tuple[str, str]:
    # Generic labels are deliberately limited: prose containing an ordinary colon
    # should not accidentally become a learned marker.
    if _TIMECODE.match(text.strip()):
        return "", text.strip()
    # Real editorial drafts commonly spell the voice-over marker as ``З.к.``,
    # ``З.К`` or glue it directly to the first word (``З.КВ 1915 году``).
    # Marker normalization alone cannot recover that punctuation, so split it
    # before the generic label machinery.
    voiceover = (
        _VOICEOVER_ZK_GLUED_PREFIX.match(text)
        or _VOICEOVER_ZK_PREFIX.match(text)
        or _VOICEOVER_Z_PREFIX.match(text)
    )
    if voiceover:
        return voiceover.group("label").strip(), voiceover.group("content").strip()
    # Built-in structural markers take precedence over generic dash-separated
    # labels. Documentary headings such as "ЗК Стратегия Лили — ..." otherwise
    # look like one long, unknown label instead of a known voiceover marker.
    known = sorted(set(_BUILTIN_MARKERS) | _TECH_MARKERS, key=len, reverse=True)
    for marker in known:
        match = re.match(
            rf"^\s*({re.escape(marker)})(?:\s*[-.]?\s*\d+[A-Za-zА-Яа-я]?)?"
            rf"(?:\s*[.:\-–—]\s*|\s+|$)(.*)$",
            text,
            flags=re.I | re.DOTALL,
        )
        if match and normalize_marker(match.group(1)) == marker:
            return match.group(1).strip(), match.group(2).strip()
    # A formal speaker credit can be much longer than an ordinary marker. It
    # is safe to accept only when its content starts with a structural timecode
    # (optionally after a "Продолжение синхрона" note).
    speaker = re.match(r"^\s*([^:\n]{2,240}?)\s*:\s*(.+)$", text, flags=re.DOTALL)
    if speaker:
        candidate = speaker.group(1).strip()
        content = speaker.group(2).strip()
        sync_content = _SYNC_CONTINUATION_NOTE.sub("", content)
        if (
            _SYNC_LABEL_METADATA.match(candidate) or _SYNC_NAME_ONLY_LABEL.match(candidate)
        ) and _TIMECODE.match(sync_content):
            return candidate, content
    # Some scripts put a timecode directly after the speaker credit, without a
    # colon: "Бартон 00:37 — 01:07 ...". Validate the prefix as a name or a
    # name-and-role credit before treating it as a label.
    timecoded_speaker = re.match(
        rf"^\s*(.{{2,240}}?)[.\s]*({_TIMECODE_TOKEN}.*)$", text, flags=re.DOTALL
    )
    if timecoded_speaker:
        candidate = timecoded_speaker.group(1).strip()
        if _SYNC_LABEL_METADATA.match(candidate) or _SYNC_NAME_ONLY_LABEL.match(candidate):
            return candidate, timecoded_speaker.group(2).strip()
    generic = re.match(r"^\s*([^:\n]{1,60}?)\s*:\s*(.+)$", text, flags=re.DOTALL)
    if generic:
        candidate = generic.group(1).strip()
        if not candidate.startswith("(") and len(candidate.split()) <= 3:
            return candidate, generic.group(2).strip()
    bare_speaker = _BARE_SPEAKER_PREFIX.match(text)
    if bare_speaker:
        content = bare_speaker.group("content").strip()
        # Avoid turning short all-caps production headings into interviewees.
        if len(content) >= 40 and re.search(r"[а-яё]", content):
            return bare_speaker.group("speaker"), content
    normalized_text = normalize_marker(text)
    if normalized_text in set(_BUILTIN_MARKERS) | _TECH_MARKERS:
        return text.strip(), ""
    return "", text.strip()


def _classify_by_rules(
    block: NormalizedBlock,
    memory: NormalizationMemory,
    video_cue_re: re.Pattern[str] = _VIDEO_CUE_KEYWORD_RE,
) -> None:
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
            _apply_type(
                block, rule["target_type"], DecisionSource.MEMORY.value, float(rule["confidence"])
            )
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
    _label, content = _split_label(block.original_text)
    sync_content = _SYNC_CONTINUATION_NOTE.sub("", content)
    if (
        _SYNC_LABEL_METADATA.match(block.original_label)
        or _SYNC_NAME_ONLY_LABEL.match(block.original_label)
    ) and _TIMECODE.match(sync_content):
        _apply_type(block, BlockType.SYNC.value, DecisionSource.BUILTIN_RULE.value, 1.0)
    elif _starts_with_unambiguous_timecode(stripped) or re.match(r"^\s*\((?:…|\.\.\.)\)", stripped):
        _apply_type(block, BlockType.SYNC.value, DecisionSource.BUILTIN_RULE.value, 0.9)
    elif _EDITORIAL_TIMECODE_PREFIX.match(stripped):
        _apply_type(block, BlockType.SYNC.value, DecisionSource.BUILTIN_RULE.value, 0.9)
    elif _STRUCTURAL_HEADING.match(stripped):
        _apply_type(block, BlockType.TECHNICAL.value, DecisionSource.BUILTIN_RULE.value, 1.0)
    elif _BARE_SPEAKER_PREFIX.match(stripped) and block.original_label and len(content) >= 40:
        _apply_type(block, BlockType.SYNC.value, DecisionSource.BUILTIN_RULE.value, 0.8)
    elif (
        re.match(r"^(?:планы?|портреты)\b", stripped, flags=re.IGNORECASE)
        or not block.original_label
    ) and _looks_like_video_cue(stripped, video_cue_re):
        _apply_type(block, BlockType.TECHNICAL.value, DecisionSource.BUILTIN_RULE.value, 1.0)
    elif block.order == 1 and not block.original_label and _looks_like_scenario_title(stripped):
        _apply_type(block, BlockType.TECHNICAL.value, DecisionSource.BUILTIN_RULE.value, 0.9)
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
        block.timecode_start = _canonicalize_timecode(timecodes[0])
        if len(timecodes) > 1 and block.type != BlockType.SYNC.value:
            block.timecode_end = _canonicalize_timecode(timecodes[1])


def _extract_block_metadata(block: NormalizedBlock) -> None:
    if block.type == BlockType.HOST.value and not block.speaker:
        block.speaker = block.original_label or "ВЕДУЩИЙ"
    if block.type != BlockType.SYNC.value:
        return
    fragment_source = _SYNC_CONTINUATION_NOTE.sub("", block.normalized_text)
    has_sync_metadata = False
    match = _SYNC_METADATA.match(block.normalized_text)
    if match:
        block.speaker = match.group(1).strip()
        block.role = match.group(2).strip()
        fragment_source = block.normalized_text[match.start(3) :].strip()
        has_sync_metadata = True
    else:
        label_match = _SYNC_LABEL_METADATA.match(block.original_label)
        name_only_match = _SYNC_NAME_ONLY_LABEL.match(block.original_label)
        if label_match and _TIMECODE.match(fragment_source):
            block.speaker = label_match.group(1).strip()
            block.role = label_match.group(2).strip()
            has_sync_metadata = True
        elif name_only_match and _TIMECODE.match(fragment_source):
            block.speaker = block.original_label.strip()
            has_sync_metadata = True
        elif (
            block.original_label
            and normalize_marker(block.original_label) not in _BUILTIN_MARKERS
            and _BARE_SPEAKER_PREFIX.match(block.original_text)
        ):
            block.speaker = block.original_label.strip()

    # A bare timecoded paragraph is an older supported sync form whose
    # normalized_text intentionally retains the leading timecode. Structured
    # fragments apply only when the block also has explicit speaker metadata.
    if not has_sync_metadata:
        return
    fragments = _extract_sync_fragments(fragment_source)
    if not fragments:
        return
    block.sync_fragments = fragments
    block.timecode_start = fragments[0].timecode_start
    block.timecode_end = fragments[0].timecode_end
    block.normalized_text = " ".join(fragment.text for fragment in fragments if fragment.text)


def _extract_sync_fragments(text: str) -> list[SyncFragment]:
    fragments: list[SyncFragment] = []
    for match in _SYNC_FRAGMENT.finditer(text):
        # The punctuation belongs to the quote; only structural whitespace and
        # separators around the next timecode are discarded.
        fragment_text = match.group("text").strip(" \t\r\n–—-")
        if not fragment_text:
            continue
        fragments.append(
            SyncFragment(
                text=fragment_text,
                timecode_start=_canonicalize_timecode(match.group("start")),
                timecode_end=_canonicalize_timecode(match.group("end") or ""),
            )
        )
    return fragments


def _canonicalize_timecode(value: str) -> str:
    value = value.strip().strip("[]")
    if re.fullmatch(r"\d{1,2}-\d{2}", value):
        return value.replace("-", ":")
    if re.fullmatch(r"\d{1,2}[.:]\d{2}\s+\d{2}", value):
        parts = re.split(r"[.:\s]+", value)
        return ":".join(parts)
    return value


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
        try:
            confidence = float(item.get("confidence", 0.6))
        except (TypeError, ValueError):
            block.warnings.append(f"LLM returned invalid confidence {item.get('confidence')!r}")
            continue
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


def _llm_cache_path(output_path: Path) -> Path:
    parent = Path(output_path).parent
    work_dir = parent if parent.name == "work" else parent / "work"
    return work_dir / "normalization_llm_cache.json"


def _load_llm_cache(path: Path) -> dict[str, Any]:
    try:
        payload = read_json(path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"version": _LLM_CACHE_VERSION, "entries": {}}
    if (
        not isinstance(payload, dict)
        or payload.get("version") != _LLM_CACHE_VERSION
        or not isinstance(payload.get("entries"), dict)
    ):
        return {"version": _LLM_CACHE_VERSION, "entries": {}}
    return payload


def _classifier_identity(
    classifier: LLMClassifier | None, *, llm_preset: str = "standard"
) -> dict[str, Any]:
    if classifier is None:
        settings = get_settings()
        return {
            "kind": "openrouter",
            "model": settings.openrouter_model,
            "llm_preset": llm_preset,
            "provider": {"sort": "throughput"} if llm_preset == "fast" else {},
            "base_url": settings.openrouter_base_url,
            "temperature": 0.1,
            "response_format": "json_object",
            "system_prompt": _OPENROUTER_SYSTEM_PROMPT,
            "instructions": _OPENROUTER_INSTRUCTIONS,
        }
    target = classifier
    code = getattr(target, "__code__", None)
    if code is None:
        target = getattr(classifier, "__call__", classifier)
        code = getattr(target, "__code__", None)
    code_hash = ""
    if code is not None:
        code_hash = sha256(
            json.dumps(
                _stable_code_identity(code),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    return {
        "kind": "custom",
        "module": getattr(classifier, "__module__", type(classifier).__module__),
        "qualname": getattr(classifier, "__qualname__", type(classifier).__qualname__),
        "code_hash": code_hash,
    }


def _stable_code_identity(code: Any) -> dict[str, Any]:
    def stable_constant(value: Any) -> Any:
        if hasattr(value, "co_code") and hasattr(value, "co_consts"):
            return _stable_code_identity(value)
        if isinstance(value, tuple):
            return [stable_constant(item) for item in value]
        if isinstance(value, frozenset):
            items = [stable_constant(item) for item in value]
            return {
                "frozenset": sorted(
                    items,
                    key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
                )
            }
        if isinstance(value, (str, int, float, bool, type(None))):
            return value
        if isinstance(value, bytes):
            return {"bytes": value.hex()}
        return {"type": type(value).__qualname__, "value": str(value)}

    return {
        "code": bytes(code.co_code).hex(),
        "constants": [stable_constant(value) for value in code.co_consts],
        "names": list(code.co_names),
        "varnames": list(code.co_varnames),
        "freevars": list(code.co_freevars),
        "cellvars": list(code.co_cellvars),
        "argcount": code.co_argcount,
        "kwonlyargcount": code.co_kwonlyargcount,
    }


def _llm_rules_signature(memory: NormalizationMemory) -> str:
    # ``list_rules()`` with no scope filter returns every alias scope (both
    # 'marker' and 'video_cue'), so learned/disabled video-cue keywords are
    # already part of this signature. The scope is included explicitly below
    # so a marker-scope rule and a video_cue-scope rule that happen to share
    # the same normalized marker/type/status/source are not indistinguishable
    # in the hashed representation.
    memory_rules = [
        {
            "scope": str(rule.get("scope") or ""),
            "normalized_marker": str(rule.get("normalized_marker") or ""),
            "target_type": str(rule.get("target_type") or ""),
            "status": str(rule.get("status") or ""),
            "source": str(rule.get("source") or ""),
        }
        for rule in memory.list_rules()
    ]
    memory_rules.sort(
        key=lambda item: (
            item["scope"],
            item["normalized_marker"],
            item["target_type"],
            item["status"],
            item["source"],
        )
    )
    rules = {
        "builtin_markers": sorted(_BUILTIN_MARKERS.items()),
        "technical_markers": sorted(_TECH_MARKERS),
        "sync_metadata": _SYNC_METADATA.pattern,
        "sync_label_metadata": _SYNC_LABEL_METADATA.pattern,
        "sync_name_only": _SYNC_NAME_ONLY_LABEL.pattern,
        "editorial_timecode_prefix": _EDITORIAL_TIMECODE_PREFIX.pattern,
        "confidence_threshold": _LLM_CONFIDENCE_THRESHOLD,
        "memory_rules": memory_rules,
    }
    return sha256(json.dumps(rules, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _stable_llm_input(payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in item.items() if key != "uid"} for item in payload]


def _llm_cache_fingerprint(
    payload: list[dict[str, Any]],
    *,
    hint: str,
    classifier: LLMClassifier | None,
    rules_signature: str,
    llm_preset: str = "standard",
) -> str:
    material = {
        "version": _LLM_CACHE_VERSION,
        "input": _stable_llm_input(payload),
        "hint": hint.strip(),
        "classifier": _classifier_identity(classifier, llm_preset=llm_preset),
        "rules_signature": rules_signature,
    }
    return sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _cacheable_llm_results(
    payload: list[dict[str, Any]], results: Any
) -> list[dict[str, Any]] | None:
    if not isinstance(results, list):
        return None
    requested_uids = [str(item.get("uid") or "") for item in payload]
    by_uid: dict[str, dict[str, Any]] = {}
    for result in results:
        if not isinstance(result, dict):
            return None
        uid = str(result.get("uid") or "")
        if not uid or uid in by_uid:
            return None
        by_uid[uid] = result
    if set(by_uid) != set(requested_uids):
        return None
    decisions: list[dict[str, Any]] = []
    for uid in requested_uids:
        decision = dict(by_uid[uid])
        decision.pop("uid", None)
        if str(decision.get("type") or "") not in VALID_BLOCK_TYPES:
            return None
        try:
            float(decision.get("confidence", 0.6))
            json.dumps(decision, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return None
        decisions.append(decision)
    return decisions


def _llm_cached_results(
    cache: dict[str, Any], cache_key: str, payload: list[dict[str, Any]]
) -> list[dict[str, Any]] | None:
    entry = cache.get("entries", {}).get(cache_key)
    if not isinstance(entry, dict) or not isinstance(entry.get("results"), list):
        return None
    decisions = entry["results"]
    if len(decisions) != len(payload) or not all(isinstance(item, dict) for item in decisions):
        return None
    restored: list[dict[str, Any]] = []
    for request, decision in zip(payload, decisions):
        item = dict(decision)
        item["uid"] = str(request.get("uid") or "")
        restored.append(item)
    if _cacheable_llm_results(payload, restored) is None:
        return None
    return restored


def _cache_llm_results(
    path: Path,
    cache: dict[str, Any],
    cache_key: str,
    payload: list[dict[str, Any]],
    results: Any,
) -> bool:
    decisions = _cacheable_llm_results(payload, results)
    if decisions is None:
        return False
    entries = cache.setdefault("entries", {})
    entries[cache_key] = {"created_at": _utcnow(), "results": decisions}
    if len(entries) > _LLM_CACHE_MAX_ENTRIES:
        oldest = sorted(
            entries,
            key=lambda key: str(entries[key].get("created_at") or ""),
        )[: len(entries) - _LLM_CACHE_MAX_ENTRIES]
        for key in oldest:
            entries.pop(key, None)
    cache["version"] = _LLM_CACHE_VERSION
    try:
        _atomic_write_json(path, cache)
    except (OSError, TypeError, ValueError):
        entries.pop(cache_key, None)
        return False
    return True


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
        "continuation_of_type": block.continuation_of_type,
    }


def _openrouter_classifier(
    blocks: list[dict[str, Any]], hint: str, *, llm_preset: str = "standard"
) -> list[dict[str, Any]]:
    llm_preset = normalize_llm_preset(llm_preset)
    settings = get_settings()
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("requests is not installed") from exc
    prompt = _OPENROUTER_INSTRUCTIONS + (
        f"Author hint: {hint or '(none)'}. Items: {json.dumps(blocks, ensure_ascii=False)}"
    )
    json_body: dict[str, Any] = {
        "model": settings.openrouter_model,
        "messages": [
            {"role": "system", "content": _OPENROUTER_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    if llm_preset == "fast":
        json_body["provider"] = {"sort": "throughput"}
    request_kwargs = {
        "headers": {
            "Authorization": f"Bearer {settings.openrouter_api_key}",
            "Content-Type": "application/json",
        },
        "json": json_body,
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
            time.sleep(2**attempt)
            continue
        if response.status_code not in {408, 429, 500, 502, 503, 504} or attempt == 2:
            break
        time.sleep(2**attempt)
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
        content = content[start : end + 1]
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
        if block.source not in VALID_DECISION_SOURCES:
            errors.append(f"Block {expected_order} has invalid source '{block.source}'")
    if errors:
        raise NormalizationValidationError(errors)


def _prepared_projection(scenario: NormalizedScenario) -> dict[str, Any]:
    voiceovers = [block for block in scenario.blocks if block.type == BlockType.VOICEOVER.value]
    return {
        "source": scenario.source,
        "used_llm": any(block.source == DecisionSource.LLM.value for block in scenario.blocks),
        "manual_markers_used": any(
            block.source == DecisionSource.MANUAL.value for block in scenario.blocks
        ),
        "extraction_method": "normalization",
        "normalization_revision": scenario.revision,
        "scenario_revision_id": scenario.scenario_revision_id,
        "parent_revision_id": scenario.parent_revision_id,
        "items": [
            {
                "id": f"zk{index}",
                "block_uid": block.uid,
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
        BlockType.VOICEOVER.value: "ЗК",
        BlockType.SYNC.value: "СНХ",
        BlockType.HOST.value: "ВЕДУЩИЙ",
        BlockType.TECHNICAL.value: "ТЕХ",
        BlockType.OTHER.value: "ПРОЧЕЕ",
        BlockType.UNKNOWN.value: "НЕИЗВЕСТНО",
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


def _path_lock(path: Path) -> threading.RLock:
    key = str(Path(path).resolve())
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


def _infer_learning_candidates(
    scenario: NormalizedScenario, memory: NormalizationMemory
) -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    rules = memory.list_rules()
    for block in scenario.blocks:
        from_type = block.proposed_type
        if (
            not from_type
            or block.type == from_type
            or block.type in {BlockType.UNKNOWN.value, BlockType.OTHER.value}
            or not block.original_label
            or not block.label_confirmed
        ):
            continue
        source_label, content = _split_label(block.original_text)
        normalized = normalize_marker(block.original_label)
        if not content or normalize_marker(source_label) != normalized:
            continue
        if memory.is_suppressed(block.original_label):
            continue
        matching = [r for r in rules if r["normalized_marker"] == normalized]
        if any(r["target_type"] == block.type and r["status"] == "active" for r in matching):
            continue
        candidate_id = sha256(
            f"correction-v1\n{normalized}\n{from_type}\n{block.type}".encode("utf-8")
        ).hexdigest()[:20]
        item = candidates.setdefault(
            candidate_id,
            {
                "id": candidate_id,
                "marker": block.original_label,
                "normalized_marker": normalized,
                "from_type": from_type,
                "target_type": block.type,
                "reason": "manual_type_change",
                "block_uids": [],
                "orders": [],
                "snippets": [],
                "status": "new",
                "confirmations": 0,
                "required_confirmations": 2,
            },
        )
        item["block_uids"].append(block.uid)
        item["orders"].append(block.order)
        item["snippets"].append(content[:160])
        existing = next(
            (r for r in matching if r["target_type"] == block.type and r["source"] == "correction"),
            None,
        )
        if existing:
            item["status"] = existing["status"]
            item["confirmations"] = int(existing["confirmations"])
    return list(candidates.values())


def _validate_learning_decisions(
    candidates: list[dict[str, Any]], decisions: list[dict[str, Any]] | None
) -> dict[str, str]:
    valid_ids = {item["id"] for item in candidates}
    result = {item["id"]: "this_time" for item in candidates}
    seen: set[str] = set()
    for decision in decisions or []:
        candidate_id = str(decision.get("candidate_id") or decision.get("id") or "")
        action = str(decision.get("action") or "")
        if candidate_id in seen or candidate_id not in valid_ids:
            raise ValueError("Invalid or duplicate learning candidate decision")
        if action not in {"remember", "this_time", "never"}:
            raise ValueError("Learning action must be remember, this_time or never")
        seen.add(candidate_id)
        result[candidate_id] = action
    return result


def _learn_from_confirmation(
    scenario: NormalizedScenario,
    memory_path: Path,
    candidates: list[dict[str, Any]],
    decisions: dict[str, str],
) -> None:
    with NormalizationMemory(memory_path) as memory:
        for candidate in candidates:
            action = decisions.get(candidate["id"], "this_time")
            if action == "remember":
                memory.record_correction_confirmation(
                    candidate["marker"], candidate["target_type"], scenario.source_hash
                )
            elif action == "never":
                memory.suppress_marker(candidate["marker"])
        for block in scenario.blocks:
            if (
                not block.original_label
                or not block.label_confirmed
                or block.type
                in {
                    BlockType.UNKNOWN.value,
                    BlockType.OTHER.value,
                }
            ):
                continue
            if block.source == DecisionSource.MANUAL.value:
                if block.memory_rule_id:
                    try:
                        memory.record_correction(block.memory_rule_id)
                    except KeyError:
                        pass
            elif block.source == DecisionSource.LLM.value:
                memory.record_llm_confirmation(
                    block.original_label, block.type, scenario.source_hash
                )


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
