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
from permitnav.evals.judge import CalibrationItem, Verdict
from permitnav.llm.port import LLMError
from permitnav.retrieval.translate import language_of


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
        """All model calls for the answer, retries included. None when the provider did not
        report a cost; 0 when no model was called."""
        search = self.answer.search
        calls = [
            *([search.completion] if search and search.completion else []),
            *self.answer.earlier,
            *([self.answer.completion] if self.answer.completion else []),
        ]
        if any(c.cost_usd is None for c in calls):
            return None
        return sum((c.cost_usd or 0.0 for c in calls), 0.0)


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


def judgeable(result: AnswerResult) -> CalibrationItem | None:
    """The answer in the shape the judge reads, or None when there is nothing to judge (a
    decline, or a failed answer)."""
    if result.kind not in ("answered", "follow_up", "not_settled"):
        return None
    return CalibrationItem(
        id=result.item.id,
        question=result.item.question,
        language=result.item.original_language,
        expected_kind=result.item.expected_kind,
        reference_answer=result.item.reference_answer,
        turns=list(result.item.turns),
        kind=result.kind,
        answer=result.answer.text,
        sources=[
            {
                "marker": s.marker,
                "unit_id": s.unit_id,
                "role": s.role,
                "heading": s.heading,
                "dates": s.dates,
                "text": s.text,
            }
            for s in result.answer.sources
        ],
    )


JUDGED_KINDS = ("answered", "follow_up", "not_settled")  # items that should get an answer


@dataclass
class JudgedRow:
    expected: str
    items: int = 0  # every item of this expected kind: the denominator for "correct"
    n: int = 0  # answers the judge read
    correct: int = 0
    partly: int = 0
    incorrect: int = 0
    faithful: int = 0  # of the n read
    unreadable: int = 0  # judged, but the verdict could not be read (or the call failed)
    not_judged: int = 0  # no answer to judge: declined or failed

    @property
    def correct_rate(self) -> float:
        """Correct answers over all items: a declined, failed or unreadable one is not
        correct, so declining more can never look better."""
        return self.correct / self.items if self.items else 0.0


def judged_summary(results: list[AnswerResult], verdicts: dict[str, Verdict]) -> list[JudgedRow]:
    """Correctness and faithfulness per expected kind, for items that should be answered
    (out-of-scope items are scored by the behaviour eval: a decline has nothing to judge)."""
    rows = {kind: JudgedRow(kind) for kind in JUDGED_KINDS}
    for r in results:
        row = rows.get(r.item.expected_kind)
        if row is None:
            continue
        row.items += 1
        verdict = verdicts.get(r.item.id)
        if verdict is None:
            row.not_judged += 1
        elif verdict.correctness is None or verdict.faithful is None:
            row.unreadable += 1
        else:
            row.n += 1
            setattr(row, verdict.correctness, getattr(row, verdict.correctness) + 1)
            row.faithful += verdict.faithful
    return [row for row in rows.values() if row.items]


def language_matches(results: list[AnswerResult]) -> tuple[int, int]:
    """(answers in the question's language, answers whose language could be told), over every
    reply the model wrote, declines included. An answer whose language the detector cannot
    tell (quoted passages are ignored; a tie or too few words) is left out, not counted as a
    mismatch."""
    written = [
        r
        for r in results
        if r.kind in (*JUDGED_KINDS, "out_of_scope") and r.answer.completion is not None
    ]
    languages = [(language_of(r.answer.text), r.item.original_language) for r in written]
    told = [(a, q) for a, q in languages if a is not None]
    return sum(a == q for a, q in told), len(told)
