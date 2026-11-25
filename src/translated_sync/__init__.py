"""Alignment of translated Russian scripts to coarse Word transcript cues."""

from .transcript import TranscriptCue, parse_docx_transcript
from .matching import match_translated_quote

__all__ = ["TranscriptCue", "parse_docx_transcript", "match_translated_quote"]
