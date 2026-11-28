"""The judge's faithfulness benchmark (permitnav.evals.faithfulness): the stored cases,
labels and predictions line up, and the figures reported on 2026-11-28 can be recomputed
from them without a model call."""

from __future__ import annotations

import json

import pytest

from permitnav import cli
from permitnav.evals import faithfulness
from permitnav.evals.faithfulness import Case, Label, Metrics, Prediction
from permitnav.evals.judge import CalibrationItem, Judge
from permitnav.llm.fake import ScriptedLLM

DEPLOYED, MEDIUM, STRONGER = "flash-lite-low", "flash-lite-medium", "flash-3.8-low"


def result(name: str) -> faithfulness.Result:
    cases, labels = faithfulness.load_cases(), faithfulness.load_labels()
    return faithfulness.evaluate(name, cases, labels, faithfulness.load_predictions(name))


def test_the_stored_set_is_40_answers_with_a_label_each() -> None:
    cases, labels = faithfulness.load_cases(), faithfulness.load_labels()
    assert len(cases) == 40 and cases.keys() == labels.keys()
    assert sum(not label.faithful for label in labels.values()) == 21
    assert sum(case.origin == "planted" for case in cases.values()) == 22
    assert all(case.item.sources and case.item.answer for case in cases.values())
    # A planted change that adds a claim is in the answer as stored.
    for case in cases.values():
        if case.planted_text:
            assert case.planted_text in case.item.answer, case.case
    assert sorted(c for c, label in labels.items() if label.known_miss) == [
        "case-23",
        "case-26",
        "case-28",
        "case-34",
        "case-35",
    ]


def test_every_configuration_has_three_runs_of_every_case() -> None:
    assert faithfulness.configurations() == sorted([DEPLOYED, MEDIUM, STRONGER])
    for name in faithfulness.configurations():
        predictions = faithfulness.load_predictions(name)
        assert len(predictions) == 120
        assert {p.run for p in predictions} == {1, 2, 3}


def test_every_stored_run_is_tied_to_the_prompt_it_was_made_with() -> None:
    """Not to the prompt as it is now: a later change to the judge's prompt leaves these
    runs what they were, and this suite green. The mismatch is said where it matters, when
    configurations are compared or run again."""
    used = faithfulness.prompt_hashes()
    assert sorted(used) == faithfulness.configurations()
    assert set(used.values()) == {"3aab38c98ac7"}
    assert faithfulness.shared_settings()["max_tokens"] == 8000


def test_a_prompt_that_differs_is_said_when_runs_are_compared() -> None:
    used = {"old": "aaaaaaaaaaaa", "new": "bbbbbbbbbbbb"}
    assert faithfulness.prompt_notes({"old": "aaaaaaaaaaaa"}, "aaaaaaaaaaaa") == {}
    changed = faithfulness.prompt_notes({"old": "aaaaaaaaaaaa"}, "bbbbbbbbbbbb")
    assert changed == {"old": "judge prompt aaaaaaaaaaaa (not the current prompt)"}
    mixed = faithfulness.prompt_notes(used, "bbbbbbbbbbbb")
    assert mixed["new"].endswith("(the current prompt)") and "not the current" in mixed["old"]
    cases, labels = faithfulness.load_cases(), faithfulness.load_labels()
    r = result(DEPLOYED)
    earlier = "\n".join(faithfulness.report([r], cases, labels, {DEPLOYED: changed["old"]}))
    assert "made with an earlier judge prompt" in earlier and "(not the current prompt)" in earlier
    both = "\n".join(faithfulness.report([r, r], cases, labels, {DEPLOYED: "x", "other": "y"}))
    assert "not all made with the same judge prompt" in both
    assert "judge prompt" not in "\n".join(faithfulness.report([r], cases, labels))


def test_the_deployed_judge_as_reported() -> None:
    r = result(DEPLOYED)
    assert r.majority == Metrics(tp=13, fp=0, tn=19, fn=8)
    assert [m.tp for m in r.per_run.values()] == [14, 13, 10]
    assert [m.fp for m in r.per_run.values()] == [0, 1, 0]
    assert r.false_negatives == [
        f"case-{n}" for n in ("03", "21", "23", "26", "28", "34", "35", "39")
    ]
    assert len(r.unstable) == 7
    assert r.planted.recall == pytest.approx(12 / 18) and r.real.recall == pytest.approx(1 / 3)
    assert r.named == (30, 34)
    assert all(r.verdicts[f"case-{n}"] == "FFF" for n in (23, 26, 28, 34, 35))


def test_the_two_alternatives_as_reported() -> None:
    medium, stronger = result(MEDIUM), result(STRONGER)
    assert medium.majority == Metrics(tp=20, fp=2, tn=17, fn=1)
    assert medium.false_positives == ["case-07", "case-17"]
    assert medium.false_negatives == ["case-39"]
    assert stronger.majority == Metrics(tp=19, fp=0, tn=19, fn=2)
    assert stronger.false_negatives == ["case-21", "case-39"]
    assert stronger.unstable == ["case-24"]
    for r in (medium, stronger):
        assert r.planted == Metrics(tp=18, fp=0, tn=4, fn=0)
        assert r.named == (54, 54)
        assert all(r.verdicts[f"case-{n}"] == "UUU" for n in (23, 26, 28, 34, 35))
    assert stronger.majority.f1 == pytest.approx(0.95, abs=0.005)
    assert (stronger.input_tokens, stronger.output_tokens) == (761886, 9277)


def test_precision_recall_and_f1() -> None:
    m = Metrics(tp=3, fp=1, tn=5, fn=1)
    assert (m.accuracy, m.precision, m.recall) == (0.8, 0.75, 0.75)
    assert m.f1 == pytest.approx(0.75)
    nothing_flagged = Metrics(tp=0, fp=0, tn=4, fn=2)
    assert nothing_flagged.precision is None and nothing_flagged.f1 is None


def test_a_quotation_of_the_planted_text_and_what_is_not_one() -> None:
    planted = "You must also hold a university degree of at least three years in the field [S2]."
    assert faithfulness.quotes(
        "You must also hold a university degree of at least three years", planted
    )
    assert faithfulness.quotes(
        "…hold a university degree of at least three years in the field.", planted
    )
    assert faithfulness.quotes("The answer says: " + planted + " And more.", planted)
    assert faithfulness.quotes(
        "Paragrafen säger att arbetstillstånd får ges i två situationer",
        "får ges i två situationer",
    )
    assert not faithfulness.quotes("Your permit is granted under section 19", planted)
    assert not faithfulness.quotes("a degree is needed", planted)  # a paraphrase
    assert not faithfulness.quotes("", planted)


def item(answer: str) -> CalibrationItem:
    return CalibrationItem(
        "i", "Q?", "en", "", "", "answered", answer, [{"marker": "S1", "heading": "h", "text": "t"}]
    )


def test_an_unreadable_verdict_flags_nothing_and_majority_needs_two_runs() -> None:
    cases = {
        "a": Case(
            "a", "planted", "invented requirement", "a booked return ticket is required", item("x")
        ),
        "b": Case("b", "real", None, None, item("y")),
    }
    labels = {"a": Label("a", False), "b": Label("b", True)}
    predictions = [
        Prediction(1, "a", False, ("a booked return ticket is required",)),
        Prediction(2, "a", None),
        Prediction(3, "a", True),
        Prediction(1, "b", False, ("something",)),
        Prediction(2, "b", True),
        Prediction(3, "b", True),
    ]
    r = faithfulness.evaluate("x", cases, labels, predictions)
    assert r.majority == Metrics(tp=0, fp=0, tn=1, fn=1)  # one flag of three is not a majority
    assert r.verdicts == {"a": "U?F", "b": "UFF"} and r.unreadable == 1
    assert r.unstable == ["a", "b"] and r.named == (1, 1)
    assert r.by_type == {"invented requirement": (1, 1, 3)}
    assert r.cost_usd is None and r.input_tokens is None  # not recorded: not called zero


def test_runs_that_do_not_cover_every_case_are_refused() -> None:
    cases = {
        "a": Case("a", "real", None, None, item("x")),
        "b": Case("b", "real", None, None, item("y")),
    }
    labels = {"a": Label("a", True), "b": Label("b", True)}
    partial = [Prediction(1, "a", True), Prediction(2, "a", True), Prediction(1, "b", True)]
    with pytest.raises(ValueError, match="do not line up"):
        faithfulness.evaluate("x", cases, labels, partial)


def test_running_a_judge_stores_what_it_said(tmp_path) -> None:
    verdict = json.dumps(
        {"faithful": False, "unsupported": ["x is required"], "reason": "no source"}
    )
    judge = Judge(ScriptedLLM(verdict), "m")
    cases = {"a": Case("a", "real", None, None, item("x is required"))}
    predictions = faithfulness.run(judge, cases, runs=2, workers=1)
    faithfulness.save_predictions("probe", predictions, tmp_path)
    again = faithfulness.load_predictions("probe", tmp_path)
    assert [(p.run, p.faithful, p.unsupported, p.reason) for p in again] == [
        (1, False, ("x is required",), "no source"),
        (2, False, ("x is required",), "no source"),
    ]


def test_the_command_reports_from_the_stored_runs_without_a_model(capsys, monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("JUDGE_MODEL", raising=False)
    assert cli.main(["judge-faithfulness"]) == 0
    out = capsys.readouterr().out
    assert "21 labelled not faithful, 19 faithful" in out
    assert "recall 19/21 (90%)" in out and "recall 13/21 (62%)" in out
    assert "23 FFF, 26 FFF, 28 FFF, 34 FFF, 35 FFF" in out
    assert "says nothing of whether the answer is right" in out


def test_a_stored_name_is_not_overwritten_by_accident(capsys) -> None:
    assert cli.main(["judge-faithfulness", "--run", DEPLOYED]) == 1
    assert "--replace" in capsys.readouterr().out
    assert cli.main(["judge-faithfulness", "--run", "../x"]) == 1


def test_a_new_run_is_recorded_with_the_model_that_gave_it(tmp_path) -> None:
    (tmp_path / "runs.json").write_text(json.dumps({"shared": {}, "configurations": {}}))
    predictions = [
        Prediction(1, "a", False, (), "r", 1, 700, 40, 0.004),
        Prediction(2, "a", None, (), "r", 0, 0, 0, None),
    ]
    faithfulness.record_run(
        "probe", "x/y", "low", predictions, "2026-12-01", "cccccccccccc", 8000, tmp_path
    )
    assert faithfulness.prompt_hashes(tmp_path) == {"probe": "cccccccccccc"}
    recorded = json.loads((tmp_path / "runs.json").read_text())["configurations"]["probe"]
    assert (recorded["model"], recorded["reasoning_effort"], recorded["runs"]) == ("x/y", "low", 2)
    assert recorded["input_tokens"] == 700 and recorded["cost_usd"] is None  # one call unpriced


def test_a_failed_call_stores_no_provider_text_and_no_made_up_cost() -> None:
    from permitnav.llm.port import LLMError

    def fail(request):
        raise LLMError("OpenRouter HTTP 429: {user_id: secret-account}")

    cases = {"a": Case("a", "real", None, None, item("x"))}
    (prediction,) = faithfulness.run(Judge(ScriptedLLM(fail), "m"), cases, runs=1, workers=1)
    assert prediction.faithful is None and "secret-account" not in prediction.reason
    assert prediction.cost_usd is None and prediction.calls == 0


@pytest.mark.parametrize(
    "predictions, problem",
    [
        ([], "no runs"),
        ([Prediction(1, "a", True), Prediction(1, "zz", True)], "not a case"),
        ([Prediction(1, "a", True), Prediction(1, "a", False)], "two verdicts"),
    ],
)
def test_verdict_files_that_do_not_fit_the_cases_are_refused(predictions, problem) -> None:
    cases = {"a": Case("a", "real", None, None, item("x"))}
    with pytest.raises(ValueError, match=problem):
        faithfulness.evaluate("x", cases, {"a": Label("a", True)}, predictions)
