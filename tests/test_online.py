"""Online evals (permitnav.online): what is counted and scored for a live question, the
live judge, and that none of it carries text or can fail an answer."""

from __future__ import annotations

import json
import time

import psycopg
import pytest

from permitnav import online, stats, tracing
from permitnav.answering.answerer import Answer
from permitnav.answering.context import Source
from permitnav.evals.judge import Judge
from permitnav.llm.fake import ScriptedLLM
from permitnav.llm.port import Completion, LLMError
from permitnav.online import Online

TRACE = "0123456789abcdef0123456789abcdef"
QUESTION = "Kan jag byta arbetsgivare?"
SOURCE = Source("S1", "u1", 1, "retrieved", "law", "6 kap. 2 §", "https://x", "LAGTEXT", "applies")
FAITHFUL = json.dumps({"faithful": True, "unsupported": [], "reason": "all supported"})
UNFAITHFUL = json.dumps({"faithful": False, "unsupported": ["two years"], "reason": "no source"})


def answer(kind: str = "answered", **more) -> Answer:
    return Answer(kind=kind, text="SVARSTEXT [S1].", sources=[SOURCE], **more)


class Sent:
    """Stands in for permitnav.scores.Scores."""

    def __init__(self) -> None:
        self.scores: list[tuple[str | None, str, object]] = []

    def add(self, trace_id, name, value) -> None:
        self.scores.append((trace_id, name, value))

    def flush(self, timeout_s: float = 5.0) -> bool:
        return True


def finish(live: Online) -> None:
    """Let the judge read what it was given, then stop."""
    assert live.settle(5)
    live.close()


def totals(url: str) -> dict[str, dict[str, int]]:
    with psycopg.connect(url) as conn:
        return stats.totals(stats.since(conn, 1))


def test_the_facts_counted_for_a_question() -> None:
    plain = online.facts(answer(), QUESTION, [])
    assert plain == {
        "kind": "answered",
        "language": "sv",
        "follow_on": "no",
        "second_call": "no",
        "recovered": "no",
        "other_language": "no",
    }
    again = Completion(text="", model="m", input_tokens=1, output_tokens=1, cost_usd=0.0)
    harder = online.facts(
        answer("not_settled", earlier=[again], recovered=True, other_language=True),
        "What is the salary requirement, and can I change employer with my permit?",
        ["an earlier question"],
    )
    assert harder == {
        "kind": "not_settled",
        "language": "en",
        "follow_on": "yes",
        "second_call": "yes",
        "recovered": "yes",
        "other_language": "yes",
    }
    assert online.facts(answer(), "12345", [])["language"] == "unknown"
    for counted in (plain, harder):  # every value is one the table accepts
        assert all(v in stats.MEASURES[m] for m, v in counted.items())


@pytest.mark.db
def test_a_question_is_counted_and_scored_without_any_text(fresh_database_url: str) -> None:
    sent = Sent()
    live = Online(database_url=fresh_database_url, scores=sent)  # type: ignore[arg-type]
    live.answered(answer(), QUESTION, [], TRACE)
    assert live.rated(TRACE, False) is True
    assert live.rated(None, True) is True  # an answer without a trace: counted all the same
    finish(live)
    assert sent.scores == [
        (TRACE, "kind", "answered"),
        (TRACE, "second_call", False),
        (TRACE, "helpful", False),
        (None, "helpful", True),
    ]
    counted = totals(fresh_database_url)
    assert counted["kind"] == {"answered": 1}
    assert counted["rating"] == {"helpful": 1, "not_helpful": 1}
    assert "faithful" not in counted  # no judge configured
    with psycopg.connect(fresh_database_url) as conn:
        stored = str(conn.execute("SELECT * FROM daily_count").fetchall())
    assert "byta" not in stored and "SVARSTEXT" not in stored and TRACE not in stored


@pytest.mark.db
def test_the_live_judge_reads_the_answer_and_its_sources(fresh_database_url: str, spans) -> None:
    llm = ScriptedLLM(UNFAITHFUL)
    sent = Sent()
    live = Online(
        database_url=fresh_database_url,
        scores=sent,  # type: ignore[arg-type]
        judge=Judge(llm, "judge-model"),
        judge_share=1.0,
    )
    with tracing.span("ask") as root:
        live.answered(answer(), QUESTION, ["Jag har arbetstillstånd."], tracing.trace_id(root))
        trace_id = tracing.trace_id(root)
    finish(live)
    (request,) = llm.requests  # faithfulness only: there is no reference answer to compare with
    material = request.messages[0]["content"]
    assert QUESTION in material and "SVARSTEXT" in material and "LAGTEXT" in material
    assert "Jag har arbetstillstånd." in material  # a follow-on is judged with what came before
    assert "Reference answer" not in material
    assert (trace_id, "faithful", False) in sent.scores
    assert totals(fresh_database_url)["faithful"] == {"no": 1}
    judged = {s.name: s for s in spans.get_finished_spans()}
    assert format(judged["live_judge"].context.trace_id, "032x") == trace_id  # the same trace
    attributes = dict(judged["live_judge"].attributes or {})
    assert attributes["permitnav.faithful"] == "no"
    assert attributes["permitnav.unsupported_claims"] == 1
    for span in spans.get_finished_spans():  # metadata only: not the claim, nor any text
        assert "two years" not in str(dict(span.attributes or {}))
        assert "SVARSTEXT" not in str(dict(span.attributes or {}))


@pytest.mark.db
def test_the_judge_records_its_reasons_only_for_the_maintainers_own_question(
    fresh_database_url: str, spans
) -> None:
    live = Online(
        database_url=fresh_database_url, judge=Judge(ScriptedLLM(UNFAITHFUL), "j"), judge_share=1.0
    )
    mine = tracing.request(full=True, session="a-conversation-id", user="maintainer")
    with mine, tracing.span("ask"):
        live.answered(answer(), QUESTION, [], None)
    finish(live)  # the judge ran on its own thread, after the request's block had ended
    judged = {s.name: dict(s.attributes or {}) for s in spans.get_finished_spans()}
    assert "two years" in str(judged["live_judge"]["langfuse.observation.output"])
    assert QUESTION in str(judged["judge"]["langfuse.observation.input"])
    assert judged["judge"]["langfuse.session.id"] == "a-conversation-id"
    assert judged["live_judge"]["langfuse.user.id"] == "maintainer"
    assert tracing.content_mode() == "metadata"  # and nothing leaked out of the request


@pytest.mark.db
def test_which_answers_the_judge_reads(fresh_database_url: str) -> None:
    llm = ScriptedLLM(FAITHFUL)
    picks = iter([0.49, 0.5, 0.0, 0.0, 0.0])
    live = Online(
        database_url=fresh_database_url,
        judge=Judge(llm, "j"),
        judge_share=0.5,
        pick=lambda: next(picks),
    )
    live.answered(answer(), QUESTION, [], TRACE)  # 0.49 < 0.5: judged
    live.answered(answer(), QUESTION, [], TRACE)  # 0.5: not
    live.answered(answer("not_settled"), QUESTION, [], TRACE)  # judged: it states the rules
    for kind in ("follow_up", "out_of_scope", "failed"):  # nothing to check against sources
        live.answered(answer(kind), QUESTION, [], TRACE)
    live.answered(Answer(kind="answered", text="x"), QUESTION, [], TRACE)  # shown no sources
    finish(live)
    assert len(llm.requests) == 2
    assert totals(fresh_database_url)["faithful"] == {"yes": 2}


@pytest.mark.db
def test_no_judge_once_the_days_spend_cap_is_reached(fresh_database_url: str) -> None:
    from permitnav import spend

    with psycopg.connect(fresh_database_url) as conn:
        spend.record(conn, 5.0)
    llm = ScriptedLLM(FAITHFUL)
    live = Online(
        database_url=fresh_database_url, judge=Judge(llm, "j"), judge_share=1.0, spend_cap_usd=5.0
    )
    live.answered(answer(), QUESTION, [], TRACE)
    finish(live)
    assert llm.requests == [] and "faithful" not in totals(fresh_database_url)


@pytest.mark.db
def test_a_judge_that_fails_is_counted_as_unread_and_scores_nothing(
    fresh_database_url: str, caplog
) -> None:
    def fail(_):
        raise LLMError("OpenRouter HTTP 402: account acct_123 has no credit")

    sent = Sent()
    live = Online(
        database_url=fresh_database_url,
        scores=sent,  # type: ignore[arg-type]
        judge=Judge(ScriptedLLM(fail), "j"),
        judge_share=1.0,
    )
    with tracing.request(full=True):  # even in full mode, an error text is not recorded
        live.answered(answer(), QUESTION, [], TRACE)
    finish(live)
    assert totals(fresh_database_url)["faithful"] == {"unread": 1}
    assert [name for _, name, _ in sent.scores] == ["kind", "second_call"]
    assert "acct_123" not in caplog.text


def test_a_database_that_is_away_does_not_stop_anything(caplog) -> None:
    sent = Sent()
    live = Online(database_url="postgresql://nobody@127.0.0.1:1/none", scores=sent)  # type: ignore[arg-type]
    live.answered(answer(), QUESTION, [], TRACE)
    assert live.rated(TRACE, True) is False  # not counted: the page is told, and no score
    finish(live)
    assert [name for _, name, _ in sent.scores] == ["kind", "second_call"]
    assert "daily counts not written" in caplog.text and "byta" not in caplog.text


def test_counting_is_not_on_the_answers_way_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """A database that hangs must not hold back an answer that is ready."""
    import threading

    release = threading.Event()
    live = Online()
    monkeypatch.setattr(live, "_count", lambda counted: release.wait(5))
    started = time.monotonic()
    live.answered(answer(), QUESTION, [], TRACE)
    assert time.monotonic() - started < 0.5
    release.set()
    finish(live)


def test_stopping_does_not_wait_for_a_judge_call_to_finish() -> None:
    import threading

    release = threading.Event()
    calling = threading.Event()

    def slow(_):
        calling.set()
        release.wait(10)
        return FAITHFUL

    live = Online(judge=Judge(ScriptedLLM(slow), "j"), judge_share=1.0)
    live._count = lambda counted: True  # type: ignore[method-assign]
    live.answered(answer(), QUESTION, [], TRACE)
    live.answered(answer(), QUESTION, [], TRACE)  # waits behind the first: dropped at a stop
    assert calling.wait(2)
    started = time.monotonic()
    live.close(wait_s=0.2)
    assert time.monotonic() - started < 2
    release.set()


def test_the_judge_sees_the_earlier_questions_the_answer_was_built_from() -> None:
    llm = ScriptedLLM(FAITHFUL)
    live = Online(judge=Judge(llm, "j"), judge_share=1.0)
    live._count = lambda counted: True  # type: ignore[method-assign]
    history = [f"earlier question {i}" for i in range(40)] + ["  "]
    live.answered(answer(), QUESTION, history, TRACE)
    finish(live)
    material = llm.requests[0].messages[0]["content"]
    assert "earlier question 39" in material and "earlier question 34" in material
    assert "earlier question 33" not in material  # intake used the last six


def test_no_more_answers_wait_for_the_judge_than_the_limit() -> None:
    import threading

    release = threading.Event()
    llm = ScriptedLLM(lambda _: (release.wait(10), FAITHFUL)[1])
    live = Online(judge=Judge(llm, "j"), judge_share=1.0)
    live._count = lambda counted: True  # type: ignore[method-assign]
    threads = [
        threading.Thread(target=live.answered, args=(answer(), QUESTION, [], TRACE))
        for _ in range(30)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert live._waiting == online.MAX_JUDGES_WAITING
    release.set()
    finish(live)


def test_the_share_of_answers_judged_is_a_number_from_0_to_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LIVE_JUDGE_SHARE", raising=False)
    assert online.judge_share() == 0.0
    assert online.live_judge(ScriptedLLM("")) is None  # off: no judge, JUDGE_MODEL not needed
    for value, share in ((" 1 ", 1.0), ("0.25", 0.25), ("0", 0.0)):
        monkeypatch.setenv("LIVE_JUDGE_SHARE", value)
        assert online.judge_share() == share
    for wrong in ("all", "1.5", "-0.1", "nan", "25%"):
        monkeypatch.setenv("LIVE_JUDGE_SHARE", wrong)
        with pytest.raises(ValueError, match="LIVE_JUDGE_SHARE"):
            online.judge_share()


def test_the_live_judge_is_the_configured_eval_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVE_JUDGE_SHARE", "1")
    monkeypatch.delenv("JUDGE_MODEL", raising=False)
    monkeypatch.delenv("JUDGE_REASONING_EFFORT", raising=False)
    with pytest.raises(ValueError, match="JUDGE_MODEL"):
        online.live_judge(ScriptedLLM(""))
    monkeypatch.setenv("JUDGE_MODEL", "google/judge")
    llm = ScriptedLLM(FAITHFUL)
    judge = online.live_judge(llm)
    assert judge is not None and judge._model == "google/judge" and judge._llm is llm
    monkeypatch.setenv("JUDGE_REASONING_EFFORT", "low")
    reasoning = online.live_judge(llm)
    assert reasoning is not None and reasoning._reasoning_effort == "low"
    assert reasoning._max_tokens == 8000  # room for the thinking
    monkeypatch.setenv("JUDGE_REASONING_EFFORT", "extreme")
    with pytest.raises(ValueError, match="JUDGE_REASONING_EFFORT"):
        online.live_judge(llm)
