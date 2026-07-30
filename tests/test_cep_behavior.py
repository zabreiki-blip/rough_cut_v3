from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest


def test_cep_behavior_in_node_runtime() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for CEP behavior tests")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, str(root / "tests" / "cep_behavior_test.cjs")],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
