"""A deterministic stand-in for tests: the share of the question's words a text contains."""

from __future__ import annotations

import re


class FakeReranker:
    model_id = "fake-reranker"

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []  # (question, number of texts)

    def score(self, question: str, texts: list[str]) -> list[float]:
        self.calls.append((question, len(texts)))
        words = set(re.findall(r"\w+", question.lower()))
        return [
            len(words & set(re.findall(r"\w+", text.lower()))) / (len(words) or 1) for text in texts
        ]
