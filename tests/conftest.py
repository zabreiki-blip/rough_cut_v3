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


@pytest.fixture(autouse=True)
def _no_local_hole_repair_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Локальный ремонт дыр облачного ASR по умолчанию включён и запускал бы
    настоящие ffmpeg/VAD/Whisper на аудио тестов. Тесты, которые его проверяют,
    включают его явно (SYNC_CLOUD_ASR_LOCAL_REPAIR=1) и мокают движок."""
    monkeypatch.setenv("SYNC_CLOUD_ASR_LOCAL_REPAIR", "0")


@pytest.fixture(autouse=True)
def _no_cloud_hole_vad_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """VAD-проверка дыр перед облачным повтором (SYNC_CLOUD_ASR_HOLE_VAD) по
    умолчанию включена и запускала бы настоящие ffmpeg/VAD на аудио тестов.
    Тесты, которые её проверяют, включают её явно и мокают VAD."""
    monkeypatch.setenv("SYNC_CLOUD_ASR_HOLE_VAD", "0")
