"""The public golden set must always pass the validator (evals/README.md)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def validate(*paths: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/validate_golden.py"), *map(str, paths)],
        capture_output=True,
        text=True,
    )


def test_every_public_golden_set_is_valid() -> None:
    files = sorted((ROOT / "evals").glob("golden.*.jsonl"))
    assert ROOT / "evals/golden.dev.jsonl" in files
    result = validate(*files)
    assert result.returncode == 0, result.stdout + result.stderr


ITEM = {
    "id": "multi-spouse-901-en", "question": "And what applies to my spouse?",
    "original_language": "en", "type": "lookup", "as_of": "2026-11-24",
    "expected": [["sfs:2005:716/5kap/3§"]], "reference_answer": "r",
    "expects_follow_up": False, "expects_not_settled": False, "expects_out_of_scope": False,
    "review": "draft",
}  # fmt: skip


def test_turns_are_earlier_questions_and_are_checked_like_the_question(tmp_path) -> None:
    import json

    from permitnav.evals.golden import load_golden

    good = dict(ITEM, turns=["My work permit was granted in February 2026. What salary do I need?"])
    path = tmp_path / "golden.jsonl"
    path.write_text(json.dumps(good) + "\n", encoding="utf-8")
    assert validate(path).returncode == 0
    (item,) = load_golden(path)
    assert item.turns == tuple(good["turns"]) and item.question == ITEM["question"]
    for bad in ([], [""], "one question", ["Call me on 070-123 45 67"]):
        path.write_text(json.dumps(dict(ITEM, turns=bad)) + "\n", encoding="utf-8")
        result = validate(path)
        assert result.returncode != 0 and "turns" in result.stdout + result.stderr, bad
