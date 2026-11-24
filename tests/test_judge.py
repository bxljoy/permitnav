"""The LLM judge and its calibration (phase 2, slice 7; D19)."""

from __future__ import annotations

import json

import pytest

from permitnav.evals.judge import (
    CalibrationItem,
    Judge,
    Label,
    agreement,
    calibration_ids,
)
from permitnav.llm.fake import ScriptedLLM

SOURCES = [
    {
        "marker": "S1",
        "unit_id": "sfs:2005:716/6kap/2§",
        "role": "retrieved",
        "heading": "6 kap. Arbetstillstånd, 2 §",
        "dates": "applies from 2026-06-01",
        "text": "Arbetstillstånd får ges om lönen minst uppgår till 90 procent av medianlönen.",
    }
]


def item(item_id: str = "work-salary-001-en", **kw) -> CalibrationItem:
    base: dict = dict(
        id=item_id,
        question="What salary do I need?",
        language="en",
        expected_kind="answered",
        reference_answer="At least 90 percent of the median salary (6 kap. 2 §).",
        kind="answered",
        answer="At least 90 percent of the median salary [S1].",
        sources=SOURCES,
    )
    base.update(kw)
    return CalibrationItem(**base)


def by_task(correctness: dict, faithfulness: dict):
    """A scripted judge that answers each of the two calls with its own reply."""

    def reply(request) -> str:
        task = correctness if "Reference answer" in request.messages[0]["content"] else faithfulness
        return json.dumps(task)

    return ScriptedLLM(reply)


def test_correctness_and_faithfulness_are_judged_in_separate_calls() -> None:
    llm = by_task(
        {"correctness": "correct", "reason": "Matches the reference."},
        {"faithful": True, "unsupported": [], "reason": "S1 says 90 percent."},
    )
    verdict = Judge(llm, model="judge-m").judge(item())
    correctness, faithfulness = llm.requests
    assert {r.model for r in llm.requests} == {"judge-m"}
    c, f = correctness.messages[0]["content"], faithfulness.messages[0]["content"]
    assert "At least 90 percent of the median salary (6 kap. 2 §)." in c  # the reference
    assert "90 procent av medianlönen" not in c  # no sources for correctness
    assert "[S1] 6 kap. Arbetstillstånd, 2 §" in f and "90 procent av medianlönen" in f
    assert "(6 kap. 2 §)." not in f and "Reference answer" not in f  # never the reference
    assert "At least 90 percent of the median salary [S1]." in c and f  # the answer, in both
    assert (verdict.correctness, verdict.faithful, verdict.unsupported) == ("correct", True, ())
    assert correctness.reasoning_effort is None  # the judge is a non-reasoning model by default
    reasoning = by_task({"correctness": "correct"}, {"faithful": True})
    Judge(reasoning, model="m", max_tokens=8000, reasoning_effort="low").judge(item())
    assert {(r.reasoning_effort, r.max_tokens) for r in reasoning.requests} == {("low", 8000)}


def test_an_unfaithful_verdict_lists_the_unsupported_claims() -> None:
    llm = by_task(
        {"correctness": "partly", "reason": "Misses a condition."},
        {"faithful": False, "unsupported": ["The salary must be paid monthly."], "reason": "x"},
    )
    verdict = Judge(llm, model="m").judge(item())
    assert verdict.correctness == "partly" and not verdict.faithful
    assert verdict.unsupported == ("The salary must be paid monthly.",)


def test_an_unreadable_verdict_is_asked_for_again_then_fails_cleanly() -> None:
    replies = iter(
        [
            "Looks fine to me.",
            json.dumps({"correctness": "correct"}),
            json.dumps({"faithful": True}),
        ]
    )
    llm = ScriptedLLM(lambda request: next(replies))
    verdict = Judge(llm, model="m").judge(item())
    assert (verdict.correctness, verdict.faithful, len(llm.requests)) == ("correct", True, 3)
    bad = Judge(ScriptedLLM('{"correctness": "great", "faithful": "yes"}'), model="m").judge(item())
    assert bad.correctness is None and bad.faithful is None


def test_agreement_is_reported_per_dimension_with_the_disagreements() -> None:
    from permitnav.evals.judge import Verdict

    labels = {
        "a": Label("a", "correct", True),
        "b": Label("b", "partly", False),
        "c": Label("c", "incorrect", True),
        "d": Label("d", "correct", True),
    }
    verdicts = {
        "a": Verdict("correct", True, (), ""),
        "b": Verdict("partly", True, (), ""),
        "c": Verdict("partly", True, (), ""),
        "d": Verdict(None, None, (), ""),  # unreadable: counted as a disagreement
    }
    result = agreement(labels, verdicts)
    # Correct vs not correct: a, b and c agree ("incorrect" vs "partly" are both not correct).
    assert (result.n, result.correctness, result.faithfulness) == (4, 0.75, 0.5)
    assert result.correctness_exact == 0.5
    assert result.correctness_confusion[("incorrect", "partly")] == 1
    assert {d[0] for d in result.disagreements} == {"b", "d"}
    assert not result.passes(0.85)


def test_the_calibration_sample_is_thirty_in_scope_answers_in_both_languages() -> None:
    from pathlib import Path

    from permitnav.evals.golden import load_golden

    items = load_golden(Path("evals/golden.dev.jsonl"))
    ids = calibration_ids(items)
    chosen = [i for i in items if i.id in ids]
    assert len(ids) == 30 == len(set(ids))
    assert all(i.expected_kind != "out_of_scope" for i in chosen)
    assert {i.original_language for i in chosen} == {"en", "sv"}
    assert len({i.id.rsplit("-", 1)[0] for i in chosen}) == 24  # every in-scope pair
    assert calibration_ids(items) == ids  # deterministic


def test_labels_must_be_well_formed(tmp_path) -> None:
    from permitnav.evals.judge import load_labels

    path = tmp_path / "labels.jsonl"
    path.write_text(json.dumps({"id": "a", "correctness": "maybe", "faithful": True}) + "\n")
    with pytest.raises(ValueError, match="correctness"):
        load_labels(path)


def test_seeded_errors_plant_a_known_mistake_in_a_real_answer(tmp_path) -> None:
    from permitnav.evals.judge import load_seeded

    base = item()
    path = tmp_path / "seeded.jsonl"
    path.write_text(
        json.dumps(
            {
                "id": "seeded-01",
                "base_id": base.id,
                "kind_of_error": "wrong number",
                "find": "90 percent",
                "replace": "80 percent",
                "correctness": "incorrect",
                "faithful": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (seeded,), labels = load_seeded(path, {base.id: base})
    assert seeded.id == "seeded-01" and "80 percent" in seeded.answer and "90" not in seeded.answer
    assert seeded.sources == base.sources and seeded.reference_answer == base.reference_answer
    assert labels["seeded-01"] == Label("seeded-01", "incorrect", False, "wrong number")
    path.write_text(path.read_text().replace('"find": "90 percent"', '"find": "nowhere"'))
    with pytest.raises(ValueError, match="is not in"):
        load_seeded(path, {base.id: base})


def test_the_checked_in_seeded_errors_apply_to_the_calibration_answers() -> None:
    from pathlib import Path

    from permitnav.evals.judge import load_items, load_seeded

    answers = {i.id: i for i in load_items(Path("evals/judge/answers.jsonl"))}
    items, labels = load_seeded(Path("evals/judge/seeded.jsonl"), answers)
    real = {a.answer for a in answers.values()}
    assert len(items) == 12 and all(i.answer not in real for i in items)
    assert sum(not label.faithful for label in labels.values()) == 12
    assert {label.correctness for label in labels.values()} == {"incorrect", "partly", "correct"}


def test_judged_answers_are_summarised_per_expected_kind() -> None:
    from datetime import date

    from permitnav.answering.answerer import Answer
    from permitnav.answering.context import Source
    from permitnav.evals.answers import AnswerResult, judgeable, judged_summary
    from permitnav.evals.golden import GoldenItem
    from permitnav.evals.judge import Verdict

    def result(item_id: str, kind: str, expected: str = "answered") -> AnswerResult:
        golden = GoldenItem(
            id=item_id,
            question="q",
            original_language="en",
            translation_of=None,
            type="lookup",
            as_of=date(2026, 11, 20),
            expected=(),
            review="checked",
            authority="law",
            expected_kind=expected,
            reference_answer="ref",
        )
        src = Source("S1", "sfs:2005:716/6kap/2§", 1, "retrieved", "law", "h", "u", "text", "d")
        return AnswerResult(golden, Answer(kind=kind, text="answer [S1]", sources=[src]))

    results = [
        result("a", "answered"),
        result("b", "answered"),
        result("c", "out_of_scope"),  # should have been answered, but was declined
        result("d", "answered"),  # the judge's call failed
        result("e", "out_of_scope", expected="out_of_scope"),  # a right decline
    ]
    items = [judgeable(r) for r in results]
    assert items[2] is None and items[4] is None  # a decline has nothing to judge
    assert items[0] is not None and items[0].reference_answer == "ref"
    assert items[0].sources[0]["marker"] == "S1" and items[0].answer == "answer [S1]"
    verdicts = {
        "a": Verdict("correct", True, (), ""),
        "b": Verdict("partly", False, ("x",), ""),
        "d": Verdict(None, None, (), "model call failed"),
    }
    (row,) = judged_summary(results, verdicts)  # out-of-scope items have no judged row
    assert (row.expected, row.items, row.n, row.correct, row.partly, row.faithful) == (
        "answered",
        4,
        2,
        1,
        1,
        1,
    )
    assert (row.unreadable, row.not_judged) == (1, 1)
    assert row.correct_rate == 0.25  # the decline and the failed verdict are not correct


def test_a_failed_judge_call_keeps_the_calls_already_made() -> None:
    from permitnav.llm.openrouter import LLMError
    from permitnav.llm.port import Completion

    class FailsSecond(ScriptedLLM):
        def complete(self, request):  # type: ignore[override]
            self.requests.append(request)
            if len(self.requests) == 2:
                raise LLMError("rate limited")
            return Completion(
                text=json.dumps({"correctness": "correct"}), model="m", cost_usd=0.001
            )

    verdict = Judge(FailsSecond(""), model="m").judge(item())
    assert verdict.correctness is None and "rate limited" in verdict.reason
    assert [c.cost_usd for c in verdict.completions] == [0.001]


def test_a_reply_with_the_wrong_verdict_is_asked_for_again() -> None:
    replies = iter(
        [
            json.dumps({"correctness": "correct"}),
            json.dumps({"correctness": "correct"}),  # faithfulness call, wrong verdict
            json.dumps({"faithful": True}),
        ]
    )
    llm = ScriptedLLM(lambda request: next(replies))
    verdict = Judge(llm, model="m").judge(item())
    assert verdict.faithful is True and len(llm.requests) == 3


def test_the_configured_reasoning_effort_belongs_to_the_configured_judge(monkeypatch) -> None:
    from permitnav.cli import _judge

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("JUDGE_MODEL", "google/gemini-3.5-flash-lite")
    monkeypatch.setenv("JUDGE_REASONING_EFFORT", "low")
    assert _judge()[1:] == ("google/gemini-3.5-flash-lite", "low")
    # Another model chosen by hand does not inherit the configured judge's effort.
    assert _judge("mistralai/mistral-large-2512")[1:] == ("mistralai/mistral-large-2512", None)
    assert _judge("openai/gpt-6-luna", "medium")[1:] == ("openai/gpt-6-luna", "medium")
    monkeypatch.setenv("JUDGE_REASONING_EFFORT", "lots")
    with pytest.raises(SystemExit, match="low, medium or high"):
        _judge()


def test_a_follow_on_question_is_judged_with_the_earlier_questions() -> None:
    llm = by_task({"correctness": "correct"}, {"faithful": True})
    earlier = "My work permit was granted in February 2026. What salary do I need?"
    Judge(llm, model="m").judge(item(question="And for my spouse?", turns=[earlier]))
    for request in llm.requests:
        content = request.messages[0]["content"]
        assert content.index(earlier) < content.index("And for my spouse?")
    plain = by_task({"correctness": "correct"}, {"faithful": True})
    Judge(plain, model="m").judge(item())
    assert all("Earlier questions" not in r.messages[0]["content"] for r in plain.requests)
