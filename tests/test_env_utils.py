from __future__ import annotations

import pytest

from env_utils import env_int, env_int_strict


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 10), ("bad", 10), ("-2", 1), ("7", 7), ("99", 20)],
)
def test_env_int_defaults_and_clamps(raw: str | None, expected: int) -> None:
    environ = {} if raw is None else {"SETTING": raw}
    assert (
        env_int(
            "SETTING",
            10,
            minimum=1,
            maximum=20,
            environ=environ,
        )
        == expected
    )


def test_env_int_strict_distinguishes_missing_and_invalid_values() -> None:
    assert env_int_strict("SETTING", 5, minimum=1, environ={}) == 5
    assert env_int_strict("SETTING", 5, minimum=1, environ={"SETTING": "2"}) == 2
    for raw in ("bad", "0"):
        with pytest.raises(ValueError, match="SETTING must be an integer"):
            env_int_strict("SETTING", 5, minimum=1, environ={"SETTING": raw})
