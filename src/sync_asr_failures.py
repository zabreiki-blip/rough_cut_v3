"""Separate "ASR failed" from "no recording" in sync results.

The matcher only knows which sources have an index; a source whose ASR failed
simply has none, so its segments end up as ``no_eligible_source`` -- the same
reason as "the speaker has no recording yet". This post-processing step runs
after matching and relabels such segments as ``asr_failed`` when a source whose
ASR failed could plausibly have served the speaker.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any

from stage2.sync_matcher import _KINSHIP_WORDS, _SETTING_WORDS, speaker_name_tokens

ASR_FAILED_REASON = "asr_failed"
_NO_SOURCE_REASON = "no_eligible_source"


def _speaker_query_tokens(speaker: str) -> list[str]:
    # Same as SyncMatcher._eligible_sources: role descriptions after a comma are
    # not part of a name; a leading service label falls through to the next part.
    return next(
        (tokens for part in speaker.split(",") if (tokens := speaker_name_tokens(part))),
        [],
    )


def _alias_matches(tokens: list[str], speaker_tokens: list[str]) -> bool:
    """Surname-stem match of a source name against the speaker (as in the matcher)."""
    if not tokens:
        return False
    if tokens == speaker_tokens:
        return True
    surname = speaker_tokens[-1]
    if tokens[-1] == surname:
        # Different given names with a shared surname are different people.
        return len(speaker_tokens) == 1 or len(tokens) == 1 or tokens[0] == speaker_tokens[0]
    return (
        tokens[0] == surname
        and len(tokens) == 2
        and (tokens[1] in _SETTING_WORDS or surname in _KINSHIP_WORDS)
    )


def _could_serve(source: Any, speaker: str) -> bool:
    speaker_tokens = _speaker_query_tokens(speaker)
    if not speaker_tokens or getattr(source, "all_speakers", False):
        return True
    names = [str(name or "") for name in getattr(source, "speaker_names", ())] or [
        str(getattr(source, "speaker", "") or ""),
        *(str(name or "") for name in getattr(source, "speakers", ()) or ()),
    ]
    for name in names:
        if _alias_matches(speaker_name_tokens(name), speaker_tokens):
            return True
        # A registered short alias (given name only) of a full-name speaker.
        if len(speaker_tokens) > 1 and speaker_name_tokens(name) == speaker_tokens[:1]:
            return True
    return False


def mark_asr_failed_matches(
    matches: Iterable[Any],
    sources: Sequence[Any],
    failed_source_ids: Iterable[str],
    normalize_text: Callable[[str], Sequence[str]] | None = None,
) -> list[str]:
    """Relabel ``no_eligible_source`` matches a failed source could have served.

    Mutates ``match.metadata`` in place and returns the changed segment ids.
    Speaker/source names are compared with ``speaker_name_tokens`` using the
    same rules as ``SyncMatcher._eligible_sources``; ``normalize_text`` is kept
    only for call compatibility.
    """
    failed = set(failed_source_ids)
    failed_sources = [source for source in sources if source.id in failed]
    changed: list[str] = []
    if not failed_sources:
        return changed
    for match in matches:
        if match.status != "not_found" or match.metadata.get("unmatched_reason") != _NO_SOURCE_REASON:
            continue
        culprits = sorted(
            source.id
            for source in failed_sources
            if _could_serve(source, str(match.speaker or ""))
        )
        if not culprits:
            continue
        match.metadata["unmatched_reason"] = ASR_FAILED_REASON
        match.metadata["asr_failed_source_ids"] = culprits
        changed.append(match.segment_id)
    return changed
