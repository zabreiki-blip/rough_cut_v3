from __future__ import annotations

import gc
import hashlib
from pathlib import Path
import threading

import api
from stage4 import versioning


def test_source_hash_cache_is_lru_bounded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(api, "_SOURCE_HASH_CACHE_MAX", 3)
    api._SOURCE_HASH_CACHE.clear()
    files = []
    for index in range(5):
        path = tmp_path / f"s{index}.docx"
        path.write_bytes(f"data-{index}".encode())
        files.append(path)

    def check(path: Path) -> bool:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return api._normalization_source_is_current(
            {"source_sha256": digest, "source_path": str(path)}, tmp_path
        )

    for path in files[:3]:
        assert check(path)
    assert check(files[0])  # touch: s0 becomes most recent
    assert check(files[3])  # evicts s1 (least recently used)

    assert len(api._SOURCE_HASH_CACHE) == 3
    assert str(files[1]) not in api._SOURCE_HASH_CACHE
    assert str(files[0]) in api._SOURCE_HASH_CACHE
    api._SOURCE_HASH_CACHE.clear()


def test_transition_lock_is_shared_while_held_and_collected_when_idle() -> None:
    name = "weak-lock-project"
    lock = api._project_transition_lock(name)
    assert api._project_transition_lock(name) is lock  # same while referenced
    del lock
    gc.collect()
    assert name not in api._PROJECT_TRANSITION_LOCKS


def test_transition_lock_excludes_waiter_across_threads() -> None:
    name = "weak-lock-exclusion"
    entered = threading.Event()
    release = threading.Event()
    order: list[str] = []

    def holder() -> None:
        with api._project_transition_lock(name):
            order.append("holder-in")
            entered.set()
            release.wait(5)
            order.append("holder-out")

    thread = threading.Thread(target=holder)
    thread.start()
    entered.wait(5)
    gc.collect()  # holder is mid-block: its lock must survive collection

    def waiter() -> None:
        with api._project_transition_lock(name):
            order.append("waiter")

    second = threading.Thread(target=waiter)
    second.start()
    release.set()
    thread.join(5)
    second.join(5)
    assert order == ["holder-in", "holder-out", "waiter"]


def test_stage4_project_lock_is_held_during_block_and_collected_after(tmp_path: Path) -> None:
    paths = {"root": tmp_path}
    key = str(tmp_path.resolve())
    with versioning._project_lock(paths):
        gc.collect()
        assert key in versioning._PROJECT_LOCKS
        with versioning._project_lock(paths):  # reentrant, same lock
            pass
    gc.collect()
    assert key not in versioning._PROJECT_LOCKS
