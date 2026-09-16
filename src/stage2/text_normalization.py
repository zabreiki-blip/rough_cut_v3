"""Shared character-level normalization for Russian Stage 2 text."""

from __future__ import annotations

import unicodedata


def fold_russian_text(value: str) -> str:
    """Fold Unicode, case, and ``ё`` without applying domain-specific stemming."""

    return unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
