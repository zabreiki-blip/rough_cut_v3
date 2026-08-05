"""Fail-closed mapping from Stage 2 sequence sources to rendered viewings."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
import unicodedata

from stage2.models import FullTimeline


@dataclass(frozen=True)
class SourceMappingResult:
    """Mapping result suitable for passing to ``build_nested_xml``."""

    catalog: dict[str, object]
    unmapped_source_ids: tuple[str, ...]
    details: dict[str, dict[str, Any]]

    def to_dict(self) -> dict[str, object]:
        return {
            "catalog": self.catalog,
            "unmapped_source_ids": list(self.unmapped_source_ids),
            "details": self.details,
        }


_DATE_PREFIX = re.compile(r"^(?:\d{6}|\d{8})(?:\s+|$)")
_GENERIC_TOKENS = {
    "copy",
    "export",
    "final",
    "proxy",
    "review",
    "render",
    "version",
    "viewing",
    "копия",
    "просмотр",
    "просмотровка",
    "рендер",
    "финал",
}
_SOURCE_KIND_TOKENS = {
    "dop",
    "interview",
    "intervyu",
    "rvk",
    "sinh",
    "sinhr",
    "sinhron",
    "snh",
    "sync",
    "интервью",
    "синх",
    "синхрон",
    "снх",
}
_IDENTITY_ALIASES = {
    "ulianovsk": "ulyanovsk",
    "ulinovsk": "ulyanovsk",
    "ulynovsk": "ulyanovsk",
}
_TRANSLITERATION = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l",
    "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s",
    "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e",
    "ю": "yu", "я": "ya",
})
_MATCHED_STATUSES = {"", "auto_matched", "confirmed"}


def _field(value: object, *names: str, default: object = "") -> object:
    if isinstance(value, Mapping):
        for name in names:
            if name in value and value[name] is not None:
                return value[name]
        return default
    for name in names:
        result = getattr(value, name, None)
        if result is not None:
            return result
    return default


def _items(value: object, *, id_names: tuple[str, ...]) -> list[tuple[str, object]]:
    raw = value.get("sources", value) if isinstance(value, Mapping) else value
    if isinstance(raw, Mapping):
        return [(str(key).strip(), item) for key, item in raw.items()]
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        result: list[tuple[str, object]] = []
        for item in raw:
            source_id = str(_field(item, *id_names)).strip()
            result.append((source_id, item))
        return result
    raise ValueError("sources must be a mapping or list")


def _normalize(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("ё", "е")
    text = Path(text).stem
    text = text.replace("_", " ")
    return " ".join(re.findall(r"[\w]+", text, flags=re.UNICODE))


def _canonical(value: object) -> str:
    normalized = _normalize(value)
    normalized = _DATE_PREFIX.sub("", normalized)
    tokens = [
        token
        for token in normalized.split()
        if token not in _GENERIC_TOKENS and not re.fullmatch(r"v?\d+", token)
    ]
    return " ".join(tokens)


def normalize_source_identity(value: object) -> str:
    tokens = []
    for token in _canonical(value).split():
        transliterated = token.translate(_TRANSLITERATION)
        if transliterated in _SOURCE_KIND_TOKENS:
            continue
        tokens.append(_IDENTITY_ALIASES.get(transliterated, transliterated))
    return " ".join(tokens)


def _media_name(media: object) -> str:
    explicit = str(_field(media, "name")).strip()
    if explicit:
        return explicit
    path = str(_field(media, "path", "media_path", "container_path", "relative_path")).strip()
    return Path(path).name


def _media_duration(media: object) -> float:
    try:
        return float(_field(media, "duration", "duration_sec", default=0.0))
    except (TypeError, ValueError):
        return 0.0


def _source_aliases(source: object, timeline_aliases: set[str]) -> tuple[set[str], set[str]]:
    sequence_values = {
        str(_field(source, "premiere_sequence", "sequence_name", "proxy_sequence_name")).strip(),
        *timeline_aliases,
    }
    speaker_values = {str(_field(source, "speaker")).strip()}
    raw_speakers = _field(source, "speakers", default=())
    if isinstance(raw_speakers, str):
        speaker_values.update(re.split(r"[,;\n]+", raw_speakers))
    elif isinstance(raw_speakers, Sequence):
        speaker_values.update(str(item) for item in raw_speakers)

    normalized_sequences = {_normalize(item) for item in sequence_values if _normalize(item)}
    normalized_speakers: set[str] = set()
    for item in speaker_values:
        full = _normalize(item)
        if not full:
            continue
        normalized_speakers.add(full)
        words = full.split()
        if len(words) > 1 and len(words[-1]) >= 4:
            normalized_speakers.add(words[-1])
    return normalized_sequences, normalized_speakers


def _score_media(
    media_name: str,
    sequence_aliases: set[str],
    speaker_aliases: set[str],
) -> tuple[int, str]:
    media_normalized = _normalize(media_name)
    media_canonical = _canonical(media_name)
    canonical_sequences = {_canonical(alias) for alias in sequence_aliases}
    canonical_speakers = {_canonical(alias) for alias in speaker_aliases}
    media_identity = normalize_source_identity(media_name)
    sequence_identities = {normalize_source_identity(alias) for alias in sequence_aliases}
    speaker_identities = {normalize_source_identity(alias) for alias in speaker_aliases}

    if media_normalized in sequence_aliases:
        return 400, "exact_sequence_name"
    if media_canonical and media_canonical in canonical_sequences:
        return 300, "normalized_sequence_name"
    if media_identity and media_identity in sequence_identities:
        return 280, "normalized_source_identity"
    if media_normalized in speaker_aliases:
        return 200, "exact_speaker_alias"
    if media_canonical and media_canonical in canonical_speakers:
        return 150, "normalized_speaker_alias"
    if media_identity and media_identity in speaker_identities:
        return 140, "normalized_speaker_identity"
    # A decorated filename such as ``BRIK просмотровка Курская`` is accepted
    # only when one complete alias remains a token. This is deliberately not a
    # fuzzy or substring comparison (e.g. ``Ира`` must not match ``Ирбе``).
    media_tokens = set(media_canonical.split())
    token_aliases = {
        alias
        for alias in canonical_sequences | canonical_speakers
        if alias and " " not in alias and len(alias) >= 4
    }
    if media_tokens & token_aliases:
        return 100, "name_token_alias"
    return 0, "no_name_match"


def _coerce_timeline(value: FullTimeline | Mapping[str, Any]) -> FullTimeline:
    return value if isinstance(value, FullTimeline) else FullTimeline.from_dict(dict(value))


def map_catalog_to_timeline(
    timeline: FullTimeline | Mapping[str, Any],
    sync_sources: object,
    catalog: object,
) -> SourceMappingResult:
    """Map Stage 2 source IDs to catalog media using names, never duration alone.

    Ties at the best name score are reported as ambiguous.  A uniquely named
    media file is still rejected when a selected timeline range extends beyond
    its duration (with one timeline-frame tolerance).
    """

    full_timeline = _coerce_timeline(timeline)
    source_items = _items(sync_sources, id_names=("id", "source_id"))
    media_items = _items(catalog, id_names=("source_id", "id"))
    if any(not source_id for source_id, _ in source_items):
        raise ValueError("sync source id must not be empty")
    if any(not media_id for media_id, _ in media_items):
        raise ValueError("catalog media source_id must not be empty")
    if len({item[0] for item in source_items}) != len(source_items):
        raise ValueError("duplicate sync source id")
    if len({item[0] for item in media_items}) != len(media_items):
        raise ValueError("duplicate catalog media source_id")

    timeline_aliases: dict[str, set[str]] = {}
    max_source_out: dict[str, float] = {}
    for segment in full_timeline.segments:
        if not segment.source_id:
            continue
        aliases = timeline_aliases.setdefault(segment.source_id, set())
        aliases.update(value for value in (segment.sequence_name, segment.speaker) if value)
        if (
            segment.type in {"snh", "host"}
            and segment.match_status in _MATCHED_STATUSES
            and segment.source_out_sec > segment.source_in_sec
        ):
            max_source_out[segment.source_id] = max(
                max_source_out.get(segment.source_id, 0.0),
                segment.source_out_sec,
            )

    keyed: dict[str, object] = {}
    details: dict[str, dict[str, Any]] = {}
    tolerance = 1.0 / full_timeline.fps
    for source_id, source in source_items:
        sequence_aliases, speaker_aliases = _source_aliases(
            source, timeline_aliases.get(source_id, set())
        )
        candidates: list[tuple[int, str, str, object]] = []
        for media_id, media in media_items:
            score, method = _score_media(
                _media_name(media), sequence_aliases, speaker_aliases
            )
            if score:
                candidates.append((score, media_id, method, media))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        best_score = candidates[0][0] if candidates else 0
        best = [candidate for candidate in candidates if candidate[0] == best_score]
        base_detail: dict[str, Any] = {
            "sequence_aliases": sorted(sequence_aliases),
            "speaker_aliases": sorted(speaker_aliases),
            "max_source_out_sec": max_source_out.get(source_id, 0.0),
            "candidates": [
                {
                    "media_source_id": media_id,
                    "name": _media_name(media),
                    "score": score,
                    "method": method,
                }
                for score, media_id, method, media in candidates
            ],
        }
        if not best:
            details[source_id] = {**base_detail, "status": "unmapped", "reason": "no_name_match"}
            continue
        if len(best) != 1:
            details[source_id] = {
                **base_detail,
                "status": "unmapped",
                "reason": "ambiguous_name_match",
            }
            continue

        score, media_id, method, media = best[0]
        duration = _media_duration(media)
        required_out = max_source_out.get(source_id, 0.0)
        if duration <= 0:
            details[source_id] = {
                **base_detail,
                "status": "unmapped",
                "reason": "invalid_media_duration",
                "media_source_id": media_id,
                "media_duration_sec": duration,
            }
            continue
        if required_out > duration + tolerance:
            details[source_id] = {
                **base_detail,
                "status": "unmapped",
                "reason": "media_too_short",
                "media_source_id": media_id,
                "media_duration_sec": duration,
            }
            continue
        keyed[source_id] = media
        details[source_id] = {
            **base_detail,
            "status": "mapped",
            "reason": method,
            "media_source_id": media_id,
            "media_name": _media_name(media),
            "media_duration_sec": duration,
            "score": score,
        }

    unmapped = tuple(source_id for source_id, _ in source_items if source_id not in keyed)
    return SourceMappingResult(
        catalog=keyed,
        unmapped_source_ids=unmapped,
        details=details,
    )


__all__ = ["SourceMappingResult", "map_catalog_to_timeline"]
