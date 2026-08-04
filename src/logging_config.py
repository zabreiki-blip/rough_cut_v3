from __future__ import annotations

from collections.abc import Mapping
import logging
from logging.config import dictConfig
import os


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


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
