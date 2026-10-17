"""Общие фикстуры тестов."""

from __future__ import annotations

import pytest

# Локальный ASR-бэкенд, который тесты считают «установленным». Реальный ASR не
# запускается: transcribe_audio в тестах мокается, важно лишь, чтобы
# resolve_backend() не зависел от пакетов окружения (на macOS стоит mlx-whisper,
# в CI-линуксе из dev-lock нет ни одного бэкенда).
_FAKE_INSTALLED_MODULES = frozenset({"faster_whisper"})


@pytest.fixture(autouse=True)
def hermetic_whisper_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Делает определение локальных Whisper-бэкендов независимым от окружения.

    Подменяется только проверка наличия модулей, так что настоящая логика
    available_backends()/resolve_backend() выполняется. Тесты про «бэкенд не
    установлен» явно патчат ``available_backends`` и перекрывают эту фикстуру.
    """
    from stage2 import whisper_runner

    monkeypatch.setattr(
        whisper_runner,
        "_module_available",
        lambda module: module in _FAKE_INSTALLED_MODULES,
    )
