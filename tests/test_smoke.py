"""The LLM smoke slice: its checks, and that the slice file matches the golden set."""

from __future__ import annotations

from pathlib import Path

from permitnav.answering.answerer import Answer, Citation
from permitnav.evals.answers import AnswerResult
from permitnav.evals.golden import load_golden
from permitnav.evals.smoke import SmokeSlice, problems
from permitnav.llm.port import Completion

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = {i.id: i for i in load_golden(ROOT / "evals" / "golden.dev.jsonl")}
CITATION = Citation("S1", "sfs:2005:716/6kap/2§", "6 kap. 2 §", "u", "law", "d")


def result(item_id: str, kind: str, text: str, citations=(), written=True) -> AnswerResult:
    completion = Completion(text="{}", model="m") if written else None
    answer = Answer(kind=kind, text=text, citations=list(citations), completion=completion)
    return AnswerResult(GOLDEN[item_id], answer)


ENGLISH = (
    "The salary must be at least 90 percent of the median salary, and the employer must offer it."
)
SWEDISH = "Lönen måste vara minst 90 procent av medianlönen, och arbetsgivaren ska erbjuda den."


def test_a_right_answer_passes() -> None:
    assert problems(result("work-salary-001-en", "answered", ENGLISH, [CITATION])) == []


def test_the_wrong_kind_fails() -> None:
    assert problems(result("work-salary-001-en", "follow_up", ENGLISH)) == [
        "kind follow_up, expected answered"
    ]


def test_an_answer_needs_a_citation() -> None:
    assert problems(result("work-salary-001-en", "answered", ENGLISH)) == [
        "answered without a citation"
    ]


def test_a_reply_in_the_wrong_language_fails() -> None:
    found = problems(result("work-salary-001-sv", "answered", ENGLISH, [CITATION]))
    assert found == ["replied in en to a sv question"]


def test_a_decline_without_a_model_call_is_not_checked_for_language() -> None:
    assert problems(result("oos-unrelated-607-sv", "out_of_scope", ENGLISH, written=False)) == []


def test_the_slice_is_checked_dev_items_covering_every_kind() -> None:
    smoke = SmokeSlice.load(ROOT / "evals" / "smoke.json")
    assert 10 <= len(smoke.items) <= 15 and len(set(smoke.items)) == len(smoke.items)
    items = [GOLDEN[i] for i in smoke.items]
    assert all(i.review == "checked" for i in items)
    assert {i.expected_kind for i in items} == {
        "answered",
        "follow_up",
        "not_settled",
        "out_of_scope",
    }
    assert {i.original_language for i in items} == {"en", "sv"}
    assert 0 <= smoke.max_failures < len(items) // 4


def test_every_smoke_question_is_recorded_for_replay() -> None:
    """Checked on every pull request: the smoke job only runs after a merge, and the
    retrieval eval skips out-of-scope questions, so nothing else would notice a smoke question
    that CI cannot replay."""
    from permitnav.evals.replay import QueryCache
    from permitnav.retrieval.translate import is_swedish

    cache = QueryCache.load(ROOT / "evals" / "queries.dev.json")
    for item_id in SmokeSlice.load(ROOT / "evals" / "smoke.json").items:
        question = GOLDEN[item_id].question
        assert question in cache.vectors, f"{item_id}: re-record evals/queries.dev.json"
        if not is_swedish(question):
            translation = cache.translations.get(question)
            assert translation in cache.vectors, f"{item_id}: translation not recorded"


def test_the_smoke_replay_survives_options_that_only_lay_results_out(
    tmp_path, capsys, monkeypatch
) -> None:
    """The smoke slice compares recorded settings too: a new layout option (e.g. paper slots)
    must not refuse a recording, as it did on main after paper_slots was added."""
    from dataclasses import asdict

    from permitnav import cli
    from permitnav.evals.replay import QueryCache
    from permitnav.retrieval.retriever import RetrievalConfig

    old = {k: v for k, v in asdict(RetrievalConfig()).items() if not k.startswith("paper_")}
    assert cli._query_settings({"config": old}) == cli._query_settings(
        {"config": asdict(RetrievalConfig())}
    )
    QueryCache(settings={"config": {**old, "translate": not old["translate"]}}).save(
        tmp_path / "q.json"
    )
    monkeypatch.setenv("ANSWER_MODEL", "test/model")  # refused before any model is called
    status = cli.main(["smoke", "--queries", str(tmp_path / "q.json")])
    assert status == 1 and "recorded with other retrieval settings" in capsys.readouterr().out
