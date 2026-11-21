"""The behaviour eval: does the answer have the kind the golden item expects?

Each item expects one kind (answered, follow_up, not_settled or out_of_scope) from its
expects_* flags. This needs no judge: the answerer's `kind` either matches or it doesn't.
Whether the content is right is the LLM judge's job (phase 2, slice 7).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from permitnav.answering.answerer import KINDS, Answer, Answerer
from permitnav.evals.golden import GoldenItem
from permitnav.llm.openrouter import LLMError


@dataclass(frozen=True)
class AnswerResult:
    item: GoldenItem
    answer: Answer

    @property
    def kind(self) -> str:
        return self.answer.kind

    @property
    def correct(self) -> bool:
        return self.kind == self.item.expected_kind

    @property
    def cost_usd(self) -> float | None:
        """None when the provider did not report the cost; 0 when no model was called."""
        completion = self.answer.completion
        return completion.cost_usd if completion else 0.0


@dataclass
class KindRow:
    expected: str
    n: int = 0
    correct: int = 0
    got: Counter[str] = field(default_factory=Counter)  # the kinds given instead


def run_answers(
    items: list[GoldenItem],
    answerer: Answerer,
    progress: Callable[[int, int], None] | None = None,
) -> list[AnswerResult]:
    """A model failure on one item is recorded as a `failed` answer, so the run keeps the
    answers already paid for."""
    results = []
    for done, item in enumerate(items, start=1):
        try:
            answer = answerer.answer(item.question, as_of=item.as_of)
        except LLMError as error:
            answer = Answer(kind="failed", text=f"model call failed: {error}")
        results.append(AnswerResult(item, answer))
        if progress:
            progress(done, len(items))
    return results


def kind_table(results: list[AnswerResult]) -> list[KindRow]:
    """One row per expected kind, in KINDS order, for the kinds the items expect."""
    rows = {kind: KindRow(kind) for kind in KINDS}  # "failed" counts in the expected row
    for result in results:
        row = rows[result.item.expected_kind]
        row.n += 1
        if result.correct:
            row.correct += 1
        else:
            row.got[result.kind] += 1
    return [row for row in rows.values() if row.n]
