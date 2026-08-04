from __future__ import annotations

from collections.abc import Mapping
import os


def env_int(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Read an integer setting, falling back on invalid input and clamping its range."""
    source = os.environ if environ is None else environ
    raw = str(source.get(name, default)).strip()
    try:
        value = int(raw)
    except ValueError:
        value = default
    return min(maximum, max(minimum, value))


def env_int_strict(
    name: str,
    default: int,
    *,
    minimum: int,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Read an integer setting whose explicit invalid values are configuration errors."""
    source = os.environ if environ is None else environ
    raw = source.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise ValueError(
            f"{name} must be an integer greater than or equal to {minimum}"
        ) from exc
    if value < minimum:
        raise ValueError(
            f"{name} must be an integer greater than or equal to {minimum}"
        )
    return value
