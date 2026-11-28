"""The faithfulness benchmark for the judge: 40 answers with the sources they were built
from, the maintainer's label for each (does every factual claim have a source?), and what
each judge configuration said of them in three runs (evals/judge/faithfulness/).

18 answers are real; 22 are real answers with one planted change of a known type, so a miss
can be traced to a kind of error. The labels were made without knowing which were which.

"Positive" here means an answer that is not faithful: recall is the share of unfaithful
answers a judge flags, precision the share of its flags that are right. An unreadable
verdict flags nothing. Everything is computed from the stored predictions, so the reported
figures can be checked without a model call; a new configuration is run on request.

The labels follow one rule the judge's prompt does not yet: a statement of uncertainty or of
missing evidence is exempt, but a statement the supplied sources directly contradict is not
exempt for being phrased as a statement about the sources (case 39).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from permitnav.evals.judge import CalibrationItem, Judge

DIRECTORY = Path("evals/judge/faithfulness")
RUNS = (1, 2, 3)
CONDITION_LEFT_OUT = "condition left out"  # a planted change that adds no claim
_MARKER = re.compile(r"\[S\d+\]")
_NOT_WORD = re.compile(r"[^\w]+", re.UNICODE)
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,40}")  # a configuration's file name
SHARED = 30  # characters two texts must share in a row to count as a quotation


@dataclass(frozen=True)
class Case:
    case: str
    origin: str  # real | planted
    planted_type: str | None
    planted_text: str | None  # the text the planted change put into the answer
    item: CalibrationItem


@dataclass(frozen=True)
class Label:
    case: str
    faithful: bool
    known_miss: bool = False


@dataclass(frozen=True)
class Prediction:
    run: int
    case: str
    faithful: bool | None
    unsupported: tuple[str, ...] = ()
    reason: str = ""
    calls: int = 1
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True)
class Metrics:
    """Detection of answers that are not faithful, over one set of cases."""

    tp: int
    fp: int
    tn: int
    fn: int

    @property
    def n(self) -> int:
        return self.tp + self.fp + self.tn + self.fn

    @property
    def accuracy(self) -> float:
        return (self.tp + self.tn) / self.n if self.n else 0.0

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None or p + r == 0:
            return None
        return 2 * p * r / (p + r)


@dataclass
class Result:
    """One configuration against the labels."""

    name: str
    per_run: dict[int, Metrics]
    majority: Metrics
    planted: Metrics
    real: Metrics
    false_negatives: list[str]  # by majority
    false_positives: list[str]
    unstable: list[str]  # cases whose verdict differed between runs
    by_type: dict[str, tuple[int, int, int]]  # labelled unfaithful: cases, flagged runs, runs
    named: tuple[int, int]  # planted claim quoted, of the runs that flagged a planted claim
    verdicts: dict[str, str] = field(default_factory=dict)  # case -> "FUF"
    unreadable: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


def load_cases(directory: Path = DIRECTORY) -> dict[str, Case]:
    cases = {}
    for row in _rows(directory / "cases.jsonl"):
        planted = row.get("planted") or {}
        cases[row["case"]] = Case(
            case=row["case"],
            origin=row["origin"],
            planted_type=row.get("planted_type"),
            planted_text=planted.get("text") or None,
            item=CalibrationItem(
                id=row["id"],
                question=row["question"],
                language=row["language"],
                expected_kind="",
                reference_answer="",  # faithfulness is judged without one
                kind=row["kind"],
                answer=row["answer"],
                sources=row["sources"],
            ),
        )
    return cases


def load_labels(directory: Path = DIRECTORY) -> dict[str, Label]:
    return {
        row["case"]: Label(row["case"], row["faithful"], bool(row.get("known_miss")))
        for row in _rows(directory / "labels.jsonl")
    }


def configurations(directory: Path = DIRECTORY) -> list[str]:
    return sorted(p.stem for p in (directory / "predictions").glob("*.jsonl"))


def load_predictions(name: str, directory: Path = DIRECTORY) -> list[Prediction]:
    return [
        Prediction(
            run=row["run"],
            case=row["case"],
            faithful=row["faithful"],
            unsupported=tuple(row.get("unsupported") or ()),
            reason=row.get("reason", ""),
            calls=row.get("calls", 1),
            input_tokens=row.get("input_tokens"),
            output_tokens=row.get("output_tokens"),
            cost_usd=row.get("cost_usd"),
        )
        for row in _rows(directory / "predictions" / f"{name}.jsonl")
    ]


def save_predictions(name: str, predictions: list[Prediction], directory: Path = DIRECTORY) -> Path:
    path = directory / "predictions" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as out:
        for p in sorted(predictions, key=lambda p: (p.run, p.case)):
            row: dict[str, object] = {
                "run": p.run,
                "case": p.case,
                "faithful": p.faithful,
                "unsupported": list(p.unsupported),
                "reason": p.reason,
                "calls": p.calls,
            }
            if p.input_tokens is not None:
                row["input_tokens"] = p.input_tokens
                row["output_tokens"] = p.output_tokens
            row["cost_usd"] = p.cost_usd
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def shared_settings(directory: Path = DIRECTORY) -> dict:
    """What the first stored runs had in common (runs.json): the prompt, the token limit."""
    with (directory / "runs.json").open(encoding="utf-8") as file:
        return json.load(file)["shared"]


def prompt_hashes(directory: Path = DIRECTORY) -> dict[str, str]:
    """Per stored configuration, the hash of the judge prompt its verdicts were given with.
    A stored run belongs to that prompt for good: when the judge's prompt changes, the old
    runs stay what they were, and the report says which runs used which prompt."""
    with (directory / "runs.json").open(encoding="utf-8") as file:
        recorded = json.load(file)
    shared = recorded["shared"].get("prompt_hash", "")
    return {
        name: settings.get("prompt_hash") or shared
        for name, settings in recorded["configurations"].items()
    }


def record_run(
    name: str,
    model: str,
    reasoning_effort: str | None,
    predictions: list[Prediction],
    date: str,
    prompt: str,
    max_tokens: int,
    directory: Path = DIRECTORY,
) -> None:
    """Write a new run's settings and totals into runs.json, beside its verdicts: stored
    verdicts without the model that gave them are not a benchmark of anything."""
    path = directory / "runs.json"
    with path.open(encoding="utf-8") as file:
        recorded = json.load(file)
    tokens_in = [p.input_tokens for p in predictions]
    tokens_out = [p.output_tokens for p in predictions]
    costs = [p.cost_usd for p in predictions]
    recorded["configurations"][name] = {
        "model": model,
        "reasoning_effort": reasoning_effort,
        "prompt_hash": prompt,
        "max_tokens": max_tokens,
        "date": date,
        "runs": len({p.run for p in predictions}),
        "input_tokens": None if None in tokens_in else sum(t or 0 for t in tokens_in),
        "output_tokens": None if None in tokens_out else sum(t or 0 for t in tokens_out),
        "cost_usd": None if None in costs else round(sum(c or 0.0 for c in costs), 4),
    }
    path.write_text(json.dumps(recorded, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as lines:
        return [json.loads(line) for line in lines if line.strip()]


def _plain(text: str) -> str:
    return _NOT_WORD.sub(" ", _MARKER.sub(" ", text)).strip().lower()


def quotes(claim: str, text: str) -> bool:
    """Whether a judge's "unsupported claim" quotes the given text: the two share a run of
    at least SHARED characters (or the whole text, when it is shorter). Source markers,
    punctuation and case are ignored, so a quotation that is shortened, or that takes in the
    sentence around the text, still counts. A paraphrase does not, so this undercounts
    rather than overcounts."""
    wanted = _plain(text)
    return bool(wanted) and shared_run(claim, text) >= min(len(wanted), SHARED)


def shared_run(a: str, b: str, whole: bool = False) -> int:
    """The longest run of characters two texts share, ignoring source markers, punctuation
    and case. With `whole`: 1 when the two are the same text and not empty, else 0."""
    one, other = _plain(a), _plain(b)
    if not one or not other:
        return 0
    if whole:
        return int(one == other)
    return SequenceMatcher(None, one, other, autojunk=False).find_longest_match().size


def metrics(labels: dict[str, Label], flagged: dict[str, bool], cases: Iterable[str]) -> Metrics:
    """`flagged`: per case, whether the judge called the answer not faithful."""
    tp = fp = tn = fn = 0
    for case in cases:
        unfaithful, flag = not labels[case].faithful, flagged[case]
        tp += unfaithful and flag
        fn += unfaithful and not flag
        fp += (not unfaithful) and flag
        tn += (not unfaithful) and not flag
    return Metrics(tp, fp, tn, fn)


def evaluate(
    name: str, cases: dict[str, Case], labels: dict[str, Label], predictions: list[Prediction]
) -> Result:
    """A configuration's predictions against the labels. Every case needs every run."""
    by_case: dict[str, dict[int, Prediction]] = {case: {} for case in cases}
    for p in predictions:
        if p.case not in by_case:
            raise ValueError(f"{name}: a verdict for {p.case!r}, which is not a case")
        if p.run in by_case[p.case]:
            raise ValueError(f"{name}: two verdicts for {p.case} in run {p.run}")
        by_case[p.case][p.run] = p
    runs = sorted({p.run for p in predictions})
    if not runs or not cases:
        raise ValueError(f"{name}: no runs to evaluate")
    missing = [case for case, got in by_case.items() if sorted(got) != runs]
    if missing or cases.keys() != labels.keys():
        raise ValueError(f"{name}: cases, labels and runs do not line up ({missing[:3]})")
    ordered = sorted(cases)
    flags = {run: {c: by_case[c][run].faithful is False for c in ordered} for run in runs}
    majority = {c: sum(flags[run][c] for run in runs) * 2 > len(runs) for c in ordered}
    by_type: dict[str, tuple[int, int, int]] = {}
    named = flagged_planted = 0
    for c in ordered:
        case = cases[c]
        if not labels[c].faithful:
            kind = case.planted_type or "real answer"
            n, hit, total = by_type.get(kind, (0, 0, 0))
            hits = sum(flags[run][c] for run in runs)
            by_type[kind] = (n + 1, hit + hits, total + len(runs))
        if case.planted_text and case.planted_type != CONDITION_LEFT_OUT:
            for run in runs:
                if flags[run][c]:
                    flagged_planted += 1
                    claims = by_case[c][run].unsupported
                    named += any(quotes(claim, case.planted_text) for claim in claims)
    tokens_in = [p.input_tokens for p in predictions]
    tokens_out = [p.output_tokens for p in predictions]
    costs = [p.cost_usd for p in predictions]
    return Result(
        name=name,
        per_run={run: metrics(labels, flags[run], ordered) for run in runs},
        majority=metrics(labels, majority, ordered),
        planted=metrics(labels, majority, [c for c in ordered if cases[c].origin == "planted"]),
        real=metrics(labels, majority, [c for c in ordered if cases[c].origin == "real"]),
        false_negatives=[c for c in ordered if not labels[c].faithful and not majority[c]],
        false_positives=[c for c in ordered if labels[c].faithful and majority[c]],
        unstable=[c for c in ordered if len({flags[run][c] for run in runs}) > 1],
        by_type=by_type,
        named=(named, flagged_planted),
        verdicts={c: "".join(_letter(by_case[c][run].faithful) for run in runs) for c in ordered},
        unreadable=sum(p.faithful is None for p in predictions),
        input_tokens=None if None in tokens_in else sum(t or 0 for t in tokens_in),
        output_tokens=None if None in tokens_out else sum(t or 0 for t in tokens_out),
        cost_usd=None if None in costs else sum(c or 0.0 for c in costs),
    )


def _letter(faithful: bool | None) -> str:
    return "?" if faithful is None else ("F" if faithful else "U")


def run(judge: Judge, cases: dict[str, Case], runs: int = len(RUNS), workers: int = 6):
    """Ask a judge about every case `runs` times (paid model calls): the predictions."""
    from concurrent.futures import ThreadPoolExecutor

    def one(job: tuple[int, str]) -> Prediction:
        number, case = job
        verdict = judge.faithfulness(cases[case].item)
        costs = [c.cost_usd for c in verdict.completions]
        return Prediction(
            run=number,
            case=case,
            faithful=verdict.faithful,
            unsupported=verdict.unsupported,
            # A failed call's reason is the provider's error text: not for a stored file.
            reason=(
                "no verdict (the call failed or the reply could not be read)"
                if verdict.faithful is None
                else verdict.reason.removeprefix("faithfulness: ")
            ),
            calls=len(verdict.completions),
            input_tokens=sum(c.input_tokens for c in verdict.completions),
            output_tokens=sum(c.output_tokens for c in verdict.completions),
            cost_usd=(
                None if not costs or None in costs else round(sum(c or 0.0 for c in costs), 6)
            ),
        )

    jobs = [(number, case) for number in range(1, runs + 1) for case in sorted(cases)]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, jobs))


def _share(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _line(m: Metrics) -> str:
    f1 = "n/a" if m.f1 is None else f"{m.f1:.2f}"
    return (
        f"accuracy {m.tp + m.tn}/{m.n} ({m.accuracy:.0%}), precision {_share(m.precision)},"
        f" recall {m.tp}/{m.tp + m.fn} ({_share(m.recall)}), F1 {f1},"
        f" false positives {m.fp}, false negatives {m.fn}"
    )


def prompt_notes(used: dict[str, str], current: str) -> dict[str, str]:
    """Per configuration, a note when the runs were not all made with one prompt, or not
    with the judge's prompt as it is now; nothing when all is the same."""
    if not used or (set(used.values()) == {current}):
        return {}
    return {
        name: f"judge prompt {value or 'unknown'}"
        + (" (the current prompt)" if value == current else " (not the current prompt)")
        for name, value in used.items()
    }


def report(
    results: list[Result],
    cases: dict[str, Case],
    labels: dict[str, Label],
    prompts: dict[str, str] | None = None,
) -> list[str]:
    """The lines `permitnav judge-faithfulness` prints. `prompts`: per configuration, a
    note on the prompt its runs used (`prompt_notes`)."""
    unfaithful = sum(not label.faithful for label in labels.values())
    lines = [
        f"{len(cases)} answers: {unfaithful} labelled not faithful,"
        f" {len(cases) - unfaithful} faithful. Positive = not faithful.",
        "Faithful is narrow: every factual claim has a source. It says nothing of whether"
        " the answer is right.",
    ]
    if prompts and len(set(prompts.values())) > 1:
        lines.append(
            "The runs below were not all made with the same judge prompt: differences"
            " between them are the prompt's as much as the model's."
        )
    elif prompts:
        lines.append(
            "The stored runs were made with an earlier judge prompt: they describe that"
            " prompt, not the judge as it is now."
        )
    short = lambda case: case.removeprefix("case-")  # noqa: E731
    for r in results:
        lines += ["", r.name]
        if prompts and r.name in prompts:
            lines.append(f"  {prompts[r.name]}")
        lines += [f"  run {run}: {_line(m)}" for run, m in r.per_run.items()]
        lines += [
            f"  majority of {len(r.per_run)} runs: {_line(r.majority)}",
            f"    planted errors: {_line(r.planted)}",
            f"    real answers:   {_line(r.real)}",
            f"  missed (majority): {', '.join(map(short, r.false_negatives)) or 'none'}",
            f"  wrongly flagged (majority): {', '.join(map(short, r.false_positives)) or 'none'}",
            f"  verdict differed between runs: {', '.join(map(short, r.unstable)) or 'none'}",
            "  flagged runs, by kind of unfaithful answer:",
        ]
        lines += [
            f"    {kind}: {hit}/{total} runs ({n} cases)"
            for kind, (n, hit, total) in sorted(r.by_type.items())
        ]
        lines.append(
            f"  quoted the planted claim in {r.named[0]} of the {r.named[1]} runs that"
            " flagged an answer with one"
        )
        misses = [c for c in sorted(labels) if labels[c].known_miss]
        if misses:
            shown = ", ".join(f"{short(c)} {r.verdicts[c]}" for c in misses)
            lines.append(f"  the deployed judge's standing misses: {shown}")
        if r.unreadable:
            lines.append(f"  unreadable verdicts: {r.unreadable}")
        tokens = (
            "tokens not recorded"
            if r.input_tokens is None
            else f"{r.input_tokens:,} input and {r.output_tokens:,} output tokens"
        )
        cost = "cost not reported" if r.cost_usd is None else f"${r.cost_usd:.2f}"
        lines.append(f"  {tokens}, {cost}")
    differing = [
        c
        for c in sorted(cases)
        if any(set(r.verdicts[c]) != {_letter(labels[c].faithful)} for r in results)
    ]
    if differing:
        lines += ["", "cases where a run differs from the label (F faithful, U not, per run):"]
        width = max(len(r.name) for r in results)
        for c in differing:
            kind = cases[c].planted_type or "real answer"
            verdicts = "  ".join(f"{r.name:<{width}} {r.verdicts[c]}" for r in results)
            lines.append(f"  {short(c)} label {_letter(labels[c].faithful)}  {verdicts}  ({kind})")
    return lines
