"""Label-only ``Синх <Имя>`` headers must not produce search units for alignment."""

from __future__ import annotations

import api


def _label_only_block() -> dict:
    return {
        "uid": "label-block",
        "type": "sync",
        "speaker": "Смольникова",
        "original_text": "Синх Смольникова",
        "label_only": True,
    }


def _quoted_block() -> dict:
    return {
        "uid": "quote-block",
        "type": "sync",
        "speaker": "Смольникова",
        "original_text": "Смольникова: Это цитата для поиска в транскрипте",
    }


def test_label_only_sync_header_produces_no_unit() -> None:
    units = api._sync_alignment_units([_label_only_block()])

    assert units == []


def test_quoted_block_after_label_only_header_still_produces_unit() -> None:
    units = api._sync_alignment_units([_label_only_block(), _quoted_block()])

    assert len(units) >= 1
    assert all(unit["parent_block_uid"] == "quote-block" for unit in units)
    assert not any(unit["parent_block_uid"] == "label-block" for unit in units)
