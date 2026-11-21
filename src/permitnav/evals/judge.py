"""The LLM judge (phase 2, slice 7; D19): is an answer correct, and faithful to its sources?

The behaviour eval checks only an answer's kind. The judge reads the answer and gives two
verdicts: correctness against the item's reference answer (correct / partly / incorrect), and
faithfulness to the sources the answer model was shown (every claim supported, or a list of
the unsupported ones). It must be a different model family from the answer model, and its
verdicts count only once they agree with hand labels on at least 85% (D19), measured here
by `agreement` on a fixed calibration sample (`calibration_ids`).

Real answers are mostly right, so labels alone cannot show that the judge catches a wrong one:
a judge that approves everything would agree. The calibration therefore also includes seeded
errors (`load_seeded`): copies of real answers with one mistake planted on purpose (a wrong
number, a flipped conclusion, an invented requirement, an unsupported detail), labelled by
construction.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from permitnav.answering.answerer import _json_object
from permitnav.evals.golden import GoldenItem
from permitnav.llm.port import LLM, Completion, CompletionRequest, LLMError
from permitnav.prompting import load_prompt

CORRECTNESS = ("correct", "partly", "incorrect")
THRESHOLD = 0.85  # D19
REASK = "That was not the JSON object asked for. Reply again with only that JSON object."


@dataclass(frozen=True)
class CalibrationItem:
    """One answer to be judged: the golden item's question and reference, the answer the
    answer model gave, and the sources it was shown."""

    id: str
    question: str
    language: str
    expected_kind: str
    reference_answer: str
    kind: str
    answer: str
    sources: list[dict]


@dataclass(frozen=True)
class Label:
    id: str
    correctness: str
    faithful: bool
    note: str = ""


@dataclass(frozen=True)
class Verdict:
    correctness: str | None  # None: the judge's reply could not be read
    faithful: bool | None
    unsupported: tuple[str, ...]
    reason: str
    completions: tuple[Completion, ...] = ()


class Judge:
    """Two separate calls, so neither verdict can leak into the other: correctness sees the
    question, the reference and the answer; faithfulness sees the question, the answer and
    the sources, never the reference."""

    def __init__(
        self, llm: LLM, model: str, max_tokens: int = 2000, reasoning_effort: str | None = None
    ) -> None:
        # A reasoning model thinks before it answers, and the thinking counts against
        # max_tokens: Mistral Large 4 returned empty replies at 1,500 tokens and took about 3
        # minutes a verdict even at "low". The calibrated judge (gemini-3.5-flash-lite) runs
        # with reasoning_effort "low" and max_tokens 8000 (see cli._judge); the defaults here
        # suit a non-reasoning model.
        self._llm = llm
        self._model = model
        self._max_tokens = max_tokens
        self._reasoning_effort = reasoning_effort
        self._correctness = load_prompt("judge_correctness.md")
        self._faithfulness = load_prompt("judge_faithfulness.md")

    def judge(self, item: CalibrationItem) -> Verdict:
        """Both verdicts. A model call that fails gives an unreadable verdict that still
        carries the calls already made (and paid for)."""
        completions: list[Completion] = []
        try:
            correctness = self._ask(
                self._correctness, _correctness_material(item), completions, "correctness"
            )
            faithfulness = self._ask(
                self._faithfulness, _faithfulness_material(item), completions, "faithful"
            )
        except LLMError as error:
            return Verdict(None, None, (), f"model call failed: {error}", tuple(completions))
        c = correctness.get("correctness") if correctness else None
        f = faithfulness.get("faithful") if faithfulness else None
        unsupported = (faithfulness or {}).get("unsupported") or []
        if not isinstance(unsupported, list):
            unsupported = [str(unsupported)]
        reasons = [
            f"{name}: {data.get('reason', '')}"
            for name, data in (("correctness", correctness), ("faithfulness", faithfulness))
            if data
        ]
        return Verdict(
            c if c in CORRECTNESS else None,
            f if isinstance(f, bool) else None,
            tuple(str(u) for u in unsupported),
            " ".join(reasons) or "the judge's reply could not be read",
            tuple(completions),
        )

    def _ask(
        self, system: str, material: str, completions: list[Completion], key: str
    ) -> dict | None:
        """The judge's JSON reply, asked for once more if unreadable; None if still not."""
        messages = [{"role": "user", "content": material}]
        for _ in range(2):
            completion = self._llm.complete(
                CompletionRequest(
                    model=self._model,
                    system=system,
                    messages=messages,
                    max_tokens=self._max_tokens,
                    reasoning_effort=self._reasoning_effort,
                )
            )
            completions.append(completion)
            data = _json_object(completion.text)
            if data is not None and _valid(data, key):
                return data
            if completion.text.strip():
                messages = [
                    *messages,
                    {"role": "assistant", "content": completion.text},
                    {"role": "user", "content": REASK},
                ]
        return None


def _valid(data: dict, key: str) -> bool:
    """Whether the reply has the verdict this call asked for."""
    if key == "correctness":
        return data.get("correctness") in CORRECTNESS
    return isinstance(data.get("faithful"), bool)


def _sources(item: CalibrationItem) -> str:
    return "\n\n---\n\n".join(
        f"[{s['marker']}] {s['heading']}\n({s.get('role', '')}; {s.get('dates', '')})\n{s['text']}"
        for s in item.sources
    )


def _correctness_material(item: CalibrationItem) -> str:
    return (
        f"# Question\n{item.question}\n\n"
        f"# Reference answer\n{item.reference_answer}\n\n"
        f"# The assistant's answer (kind: {item.kind})\n{item.answer}"
    )


def _faithfulness_material(item: CalibrationItem) -> str:
    return (
        f"# Question\n{item.question}\n\n"
        f"# The assistant's answer\n{item.answer}\n\n"
        f"# Sources the assistant was shown\n{_sources(item) or '(none)'}"
    )


@dataclass
class Agreement:
    n: int
    # Share of items where judge and label agree on correct vs not correct: the decision that
    # matters. "partly" vs "incorrect" is a fine judgement even between people.
    correctness: float
    faithfulness: float
    correctness_exact: float = 0.0  # all three levels must match
    correctness_confusion: Counter[tuple[str, str]] = field(
        default_factory=Counter
    )  # (label, judge)
    disagreements: list[tuple[str, str, str]] = field(
        default_factory=list
    )  # (id, dimension, detail)

    def passes(self, threshold: float = THRESHOLD) -> bool:
        return self.correctness >= threshold and self.faithfulness >= threshold


def agreement(labels: dict[str, Label], verdicts: dict[str, Verdict]) -> Agreement:
    """Agreement on the items that have both a label and a verdict. An unreadable verdict
    counts as a disagreement."""
    ids = sorted(labels.keys() & verdicts.keys())
    result = Agreement(n=len(ids), correctness=0.0, faithfulness=0.0)
    same_c = same_f = exact_c = 0
    for i in ids:
        label, verdict = labels[i], verdicts[i]
        judged_c = verdict.correctness or "unreadable"
        result.correctness_confusion[(label.correctness, judged_c)] += 1
        exact_c += verdict.correctness == label.correctness
        if verdict.correctness is not None and (verdict.correctness == "correct") == (
            label.correctness == "correct"
        ):
            same_c += 1
        else:
            result.disagreements.append(
                (i, "correctness", f"you: {label.correctness}, judge: {judged_c}")
            )
        if verdict.faithful is label.faithful:
            same_f += 1
        else:
            judged_f = "unreadable" if verdict.faithful is None else str(verdict.faithful).lower()
            result.disagreements.append(
                (i, "faithfulness", f"you: {str(label.faithful).lower()}, judge: {judged_f}")
            )
    if ids:
        result.correctness = same_c / len(ids)
        result.faithfulness = same_f / len(ids)
        result.correctness_exact = exact_c / len(ids)
    return result


def load_seeded(
    path: Path, answers: dict[str, CalibrationItem]
) -> tuple[list[CalibrationItem], dict[str, Label]]:
    """Seeded errors: each line names a real answer (`base_id`), text to `find` in it (every
    occurrence is replaced) and its `replace`ment, and the labels the result has by
    construction."""
    items, labels = [], {}
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        raw = json.loads(line)
        base = answers.get(raw["base_id"])
        if base is None:
            raise ValueError(f"{path} line {n}: no answer {raw['base_id']!r}")
        if raw["find"] not in base.answer:
            raise ValueError(f"{path} line {n}: {raw['find']!r} is not in {raw['base_id']}")
        if raw["correctness"] not in CORRECTNESS or not isinstance(raw["faithful"], bool):
            raise ValueError(f"{path} line {n}: bad labels")
        items.append(
            CalibrationItem(
                **{
                    **base.__dict__,
                    "id": raw["id"],
                    "answer": base.answer.replace(raw["find"], raw["replace"]),
                }
            )
        )
        labels[raw["id"]] = Label(
            raw["id"], raw["correctness"], raw["faithful"], raw.get("kind_of_error", "")
        )
    return items, labels


def calibration_ids(items: list[GoldenItem]) -> list[str]:
    """30 in-scope answers: one per question pair (alternating English and Swedish), and the
    other language for the first six pairs. Deterministic, so labels stay valid."""
    pairs: dict[str, dict[str, str]] = {}
    for item in items:
        if item.expected_kind == "out_of_scope":
            continue
        pairs.setdefault(item.id.rsplit("-", 1)[0], {})[item.original_language] = item.id
    chosen: list[str] = []
    for n, base in enumerate(sorted(pairs)):
        languages = pairs[base]
        first = "en" if n % 2 == 0 else "sv"
        chosen.append(languages.get(first) or next(iter(languages.values())))
        if n < 6 and len(languages) > 1:
            chosen.append(languages["sv" if first == "en" else "en"])
    return chosen


def load_items(path: Path) -> list[CalibrationItem]:
    return [
        CalibrationItem(**json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def save_items(path: Path, items: list[CalibrationItem]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(i.__dict__, ensure_ascii=False) + "\n" for i in items), encoding="utf-8"
    )


def load_labels(path: Path) -> dict[str, Label]:
    labels = {}
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        raw = json.loads(line)
        if raw.get("correctness") not in CORRECTNESS:
            raise ValueError(f"{path} line {n}: correctness must be one of {CORRECTNESS}")
        if not isinstance(raw.get("faithful"), bool):
            raise ValueError(f"{path} line {n}: faithful must be true or false")
        labels[raw["id"]] = Label(
            raw["id"], raw["correctness"], raw["faithful"], raw.get("note", "")
        )
    return labels
