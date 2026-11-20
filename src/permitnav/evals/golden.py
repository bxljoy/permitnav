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
            )
        )
    return items
