from __future__ import annotations

from collections.abc import Mapping
import logging
from logging.config import dictConfig
import os
import re


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


_SECRET_PATTERNS = (
    re.compile(
        r"(?i)(\bauthorization\b[\s'\"]*(?:=|:)[\s'\"]*)"
        r"(?:bearer\s+)?[^\s,;\"'&}]+"
    ),
    # A Bearer credential can occur in a header dictionary, an HTTP error, or
    # an exception message. Keep the scheme visible for troubleshooting.
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/%=-]+"),
    # Covers both JSON/query-string style key=value and Python header dict
    # renderings such as ``'X-API-Token': 'secret'``.
    re.compile(
        r"(?i)(\b(?:x-api-token|x-connector-token|"
        r"(?:openrouter|gemini|macos_connector|rough_cut)[_-]?(?:api[_-]?)?"
        r"(?:key|token)|api[_-]?key)\b[\s'\"]*(?:=|:)[\s'\"]*)"
        r"[^\s,;\"'&}]+"
    ),
)


def redact_sensitive_text(value: object) -> str:
    """Remove credentials from text before it reaches a log sink."""

    text = str(value)
    text = _SECRET_PATTERNS[0].sub(r"\1[REDACTED]", text)
    text = _SECRET_PATTERNS[1].sub("Bearer [REDACTED]", text)
    return _SECRET_PATTERNS[2].sub(r"\1[REDACTED]", text)


class RedactingFormatter(logging.Formatter):
    """Formatter-level redaction also covers exception tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_sensitive_text(super().format(record))


def configure_logging(environ: Mapping[str, str] | None = None) -> str:
    """Configure application logging consistently for API, CLI, and connectors."""
    source = os.environ if environ is None else environ
    requested = str(source.get("LOG_LEVEL", "INFO")).strip().upper() or "INFO"
    level = requested if requested in logging.getLevelNamesMapping() else "INFO"
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {
                    "()": RedactingFormatter,
                    "format": LOG_FORMAT,
                }
            },
            "handlers": {
                "stderr": {
                    "class": "logging.StreamHandler",
                    "formatter": "standard",
                    "level": level,
                    "stream": "ext://sys.stderr",
                }
            },
            "root": {
                "handlers": ["stderr"],
                "level": level,
            },
        }
    )
    return level
