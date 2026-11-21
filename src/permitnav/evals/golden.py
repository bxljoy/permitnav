"""Loading golden-set items (format: evals/README.md). Validation stays in
scripts/validate_golden.py; this module only reads what it needs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class GoldenItem:
    id: str
    question: str
    original_language: str
    translation_of: str | None
    type: str
    as_of: date
    expected: tuple[tuple[str, ...], ...]  # required groups of acceptable alternatives (D16)
    review: str
    authority: str | None
    # What the answer should be: answered | follow_up | not_settled | out_of_scope, from the
    # item's expects_* flags (they never combine).
    expected_kind: str = "answered"

    @property
    def is_translation(self) -> bool:
        return self.translation_of is not None


def load_golden(path: Path) -> list[GoldenItem]:
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        items.append(
            GoldenItem(
                id=raw["id"],
                question=raw["question"],
                original_language=raw["original_language"],
                translation_of=raw.get("translation_of"),
                type=raw["type"],
                as_of=date.fromisoformat(raw["as_of"]),
                expected=tuple(tuple(group) for group in raw["expected"]),
                review=raw["review"],
                authority=raw.get("authority"),
                expected_kind=_expected_kind(raw),
            )
        )
    return items


def _expected_kind(raw: dict) -> str:
    flags = [
        f
        for f in ("expects_follow_up", "expects_not_settled", "expects_out_of_scope")
        if raw.get(f)
    ]
    if len(flags) > 1:
        raise ValueError(f"{raw.get('id')}: at most one expects_* flag may be true, got {flags}")
    if raw.get("expects_out_of_scope"):
        return "out_of_scope"
    if raw.get("expects_follow_up"):
        return "follow_up"
    if raw.get("expects_not_settled"):
        return "not_settled"
    return "answered"
