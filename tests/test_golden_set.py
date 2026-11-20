"""The public golden set must always pass the validator (evals/README.md)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dev_set_is_valid() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/validate_golden.py"),
            str(ROOT / "evals/golden.dev.jsonl"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
