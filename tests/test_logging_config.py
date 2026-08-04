from __future__ import annotations

import logging

from logging_config import LOG_FORMAT, configure_logging


def test_logging_configuration_is_idempotent_and_has_stable_format() -> None:
    assert configure_logging({"LOG_LEVEL": "DEBUG"}) == "DEBUG"
    assert configure_logging({"LOG_LEVEL": "DEBUG"}) == "DEBUG"

    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert len(root.handlers) == 1
    assert root.handlers[0].formatter is not None
    assert root.handlers[0].formatter._fmt == LOG_FORMAT


def test_invalid_log_level_falls_back_to_info() -> None:
    assert configure_logging({"LOG_LEVEL": "verbose"}) == "INFO"
    assert logging.getLogger().level == logging.INFO
