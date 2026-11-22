"""The LLM smoke slice (phase 3): a few golden questions the full pipeline must still get
right after a change, run in CI on merge to main and nightly, with the real answer model.

Checks per question, none needing a judge: the expected answer kind; for an answer, at least
one citation; a reply in the question's language. The questions were chosen as ones the
current configuration got right in every dev run, so a failure is a signal, not noise. Even
those flip now and then (1 in 36 answers in three runs), so a failing question is asked once
more and fails only if it fails again, and one failing question is still allowed
(`max_failures`). A real regression fails consistently and is still caught.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from permitnav.evals.answers import AnswerResult
from permitnav.retrieval.translate import language_of


@dataclass(frozen=True)
class SmokeSlice:
    items: list[str]
    max_failures: int

    @classmethod
    def load(cls, path: Path) -> SmokeSlice:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(items=list(data["items"]), max_failures=int(data["max_failures"]))


def problems(result: AnswerResult) -> list[str]:
    """What is wrong with one answer; empty when it passes."""
    found = []
    expected = result.item.expected_kind
    if result.kind != expected:
        found.append(f"kind {result.kind}, expected {expected}")
    elif expected == "answered" and not result.answer.citations:
        found.append("answered without a citation")
    if result.answer.completion is not None:  # a reply the model wrote
        language = language_of(result.answer.text)
        if language is not None and language != result.item.original_language:
            found.append(f"replied in {language} to a {result.item.original_language} question")
    return found
