"""The reranker port (phase 2, slice 6): scores (question, text) pairs together.

An embedding compares a question and a text that were encoded separately; a reranker (a
cross-encoder) reads both at once, so it can see that "the employer must check the permit"
answers "what does an employer have to do". Scores are comparable for one question across
texts, not across questions.
"""

from __future__ import annotations

from typing import Protocol


class Reranker(Protocol):
    model_id: str

    def score(self, question: str, texts: list[str]) -> list[float]:
        """One relevance score per text, higher is better."""
        ...
