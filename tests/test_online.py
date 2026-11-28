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
        self.metadata: dict[str, dict | None] = {}

    def named(self, name: str) -> list[object]:
        return [value for _, n, value in self.scores if n == name]

    def add(self, trace_id, name, value, metadata=None) -> None:
        self.scores.append((trace_id, name, value))
        self.metadata[name] = metadata

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
    plain = online.facts(answer(language="match"), QUESTION, [])
    assert plain == {
        "kind": "answered",
        "language": "sv",
        "follow_on": "no",
        "second_call": "no",
        "recovered": "no",
        "output": "valid",
        "citation_generated": "valid",
        "citation_final": "valid",
        "language_match": "yes",
    }
    again = Completion(text="", model="m", input_tokens=1, output_tokens=1, cost_usd=0.0)
    harder = online.facts(
        answer(
            "not_settled",
            earlier=[again],
            recovered=True,
            language="mismatch",
            dropped_markers=["S9"],
        ),
        "What is the salary requirement, and can I change employer with my permit?",
        ["an earlier question"],
    )
    assert harder == {
        "kind": "not_settled",
        "language": "en",
        "follow_on": "yes",
        "second_call": "yes",
        "recovered": "yes",
        "output": "valid",
        "citation_generated": "invalid",  # the reply cited a source it was not given
        "citation_final": "valid",  # and the answer shown no longer does
        "language_match": "no",
    }
    assert online.facts(answer(), "12345", [])["language"] == "unknown"
    # An answer whose language cannot be told is unknown, never a match.
    assert online.facts(answer(), QUESTION, [])["language_match"] == "unknown"
    for counted in (plain, harder):  # every value is one the table accepts
        assert all(stats.known(m, v) for m, v in counted.items())


def test_a_failed_reply_is_an_invalid_output_and_nothing_is_said_of_its_citations() -> None:
    failed = online.facts(Answer(kind="failed", text="could not be read"), QUESTION, [])
    assert failed["output"] == "invalid"
    assert not {"citation_generated", "citation_final", "language_match"} & failed.keys()


def test_the_check_on_the_answer_as_shown() -> None:
    assert online.cites_only_its_sources(answer())
    assert online.cites_only_its_sources(Answer(kind="answered", text="No markers."))
    stray = Answer(kind="answered", text="Yes [S1] and [S7].", sources=[SOURCE])
    assert not online.cites_only_its_sources(stray)
    assert online.facts(stray, QUESTION, [])["citation_final"] == "invalid"


@pytest.mark.db
def test_a_question_is_counted_and_scored_without_any_text(fresh_database_url: str) -> None:
    sent = Sent()
    live = Online(
        database_url=fresh_database_url,
        scores=sent,  # type: ignore[arg-type]
        release="0ab9798c1d2e",
    )
    live.answered(answer(language="match"), QUESTION, [], TRACE)
    assert live.rated(TRACE, False) is True
    assert live.rated(None, True) is True  # an answer without a trace: counted all the same
    finish(live)
    assert sent.scores == [  # quality signals only: the kind is a tag on the trace, not a score
        (TRACE, "output_valid", True),
        (TRACE, "second_call", False),
        (TRACE, "citation_id_valid", True),
        (TRACE, "language_match", True),
        (TRACE, "helpful", False),
        (None, "helpful", True),
    ]
    counted = totals(fresh_database_url)
    assert counted["kind"] == {"answered": 1}
    assert counted["output"] == {"valid": 1}
    assert counted["judge"] == {"off": 1}  # no judge configured: said, not left blank
    assert counted["release"] == {"0ab9798c1d2e": 1}
    assert counted["rating"] == {"helpful": 1, "not_helpful": 1}
    assert "faithful" not in counted and "judge_version" not in counted
    with psycopg.connect(fresh_database_url) as conn:
        stored = str(conn.execute("SELECT * FROM daily_count").fetchall())
    assert "byta" not in stored and "SVARSTEXT" not in stored and TRACE not in stored


def test_what_cannot_be_told_gets_no_score() -> None:
    sent = Sent()
    live = Online(scores=sent)  # type: ignore[arg-type]
    live._count = lambda counted: True  # type: ignore[method-assign]
    live.answered(answer(language="unknown", dropped_markers=["S9"]), QUESTION, [], TRACE)
    live.answered(Answer(kind="failed", text="x"), QUESTION, [], TRACE)
    finish(live)
    assert sent.named("language_match") == []  # unknown is not a pass, and not a fail
    assert sent.named("citation_id_valid") == [False]  # of the answer; none for the failed reply
    assert sent.named("output_valid") == [True, False]
    assert sent.named("faithful") == [] and sent.named("judge_status") == []


@pytest.mark.db
def test_a_model_call_that_fails_is_counted_as_an_error(fresh_database_url: str) -> None:
    sent = Sent()
    live = Online(database_url=fresh_database_url, scores=sent, release="0ab9798c1d2e")  # type: ignore[arg-type]
    live.errored(TRACE)
    finish(live)
    assert sent.scores == [(TRACE, "output_valid", False)]
    assert totals(fresh_database_url) == {"output": {"error": 1}, "release": {"0ab9798c1d2e": 1}}


@pytest.mark.db
def test_the_live_judge_reads_the_answer_and_its_sources(fresh_database_url: str, spans) -> None:
    llm = ScriptedLLM(UNFAITHFUL)
    sent = Sent()
    live = Online(
        database_url=fresh_database_url,
        scores=sent,  # type: ignore[arg-type]
        judge=Judge(llm, "judge-model"),
        judge_share=1.0,
        judge_version="abcdef012345",
    )
    with tracing.span("ask") as root:
        status = live.answered(
            answer(), QUESTION, ["Jag har arbetstillstånd."], tracing.trace_id(root)
        )
        trace_id = tracing.trace_id(root)
    finish(live)
    assert status == "queued"
    (request,) = llm.requests  # faithfulness only: there is no reference answer to compare with
    material = request.messages[0]["content"]
    assert QUESTION in material and "SVARSTEXT" in material and "LAGTEXT" in material
    assert "Jag har arbetstillstånd." in material  # a follow-on is judged with what came before
    assert "Reference answer" not in material
    assert (trace_id, "faithful", False) in sent.scores
    assert (trace_id, "judge_status", "completed") in sent.scores
    assert sent.metadata["faithful"] == {"judge_version": "abcdef012345"}  # which judge said so
    counted = totals(fresh_database_url)
    assert counted["faithful"] == {"no": 1} and counted["judge"] == {"completed": 1}
    assert counted["judge_version"] == {"abcdef012345": 1}
    judged = {s.name: s for s in spans.get_finished_spans()}
    assert format(judged["live_judge"].context.trace_id, "032x") == trace_id  # the same trace
    attributes = dict(judged["live_judge"].attributes or {})
    assert attributes["permitnav.faithful"] == "no"
    assert attributes["permitnav.judge_status"] == "completed"
    assert attributes["permitnav.judge_version"] == "abcdef012345"
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
def test_a_judge_that_fails_gives_no_verdict_either_way(fresh_database_url: str, caplog) -> None:
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
    counted = totals(fresh_database_url)
    assert counted["judge"] == {"failed": 1}
    assert "faithful" not in counted  # neither faithful nor unfaithful
    assert sent.named("faithful") == [] and sent.named("judge_status") == ["failed"]
    assert "acct_123" not in caplog.text


@pytest.mark.db
def test_every_way_an_answer_can_go_unjudged_is_told_apart(fresh_database_url: str) -> None:
    from permitnav import spend

    sent = Sent()
    picks = iter([0.9, 0.0])
    live = Online(
        database_url=fresh_database_url,
        scores=sent,  # type: ignore[arg-type]
        judge=Judge(ScriptedLLM(FAITHFUL), "j"),
        judge_share=0.5,
        spend_cap_usd=5.0,
        pick=lambda: next(picks),
    )
    assert live.answered(answer("follow_up"), QUESTION, [], TRACE) == "ineligible"
    assert live.answered(answer(), QUESTION, [], TRACE) == "not_sampled"
    with psycopg.connect(fresh_database_url) as conn:
        spend.record(conn, 5.0)  # the day's cap is reached before the judge gets to it
    assert live.answered(answer(), QUESTION, [], TRACE) == "queued"
    assert live.settle(5)
    live._waiting = online.MAX_JUDGES_WAITING  # as if that many answers were waiting
    live.pick = lambda: 0.0
    assert live.answered(answer(), QUESTION, [], TRACE) == "queue_full"
    live._waiting = 0
    finish(live)
    off = Online(database_url=fresh_database_url)
    assert off.answered(answer(), QUESTION, [], TRACE) == "off"
    finish(off)
    counted = totals(fresh_database_url)
    assert counted["judge"] == {
        "ineligible": 1,
        "not_sampled": 1,
        "capped": 1,
        "queue_full": 1,
        "off": 1,
    }
    assert "faithful" not in counted  # none of them is a verdict
    # A score only where the answer was chosen for the judge and got no verdict.
    assert sorted(map(str, sent.named("judge_status"))) == ["capped", "queue_full"]
    assert sent.named("faithful") == []


def test_a_database_that_is_away_does_not_stop_anything(caplog) -> None:
    sent = Sent()
    live = Online(database_url="postgresql://nobody@127.0.0.1:1/none", scores=sent)  # type: ignore[arg-type]
    live.answered(answer(language="match"), QUESTION, [], TRACE)
    assert live.rated(TRACE, True) is False  # not counted: the page is told, and no score
    finish(live)
    assert [name for _, name, _ in sent.scores] == [
        "output_valid",
        "second_call",
        "citation_id_valid",
        "language_match",
    ]
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


def test_a_database_that_cannot_be_read_is_not_called_the_spend_cap() -> None:
    sent = Sent()
    llm = ScriptedLLM(FAITHFUL)
    live = Online(
        database_url="postgresql://nobody@127.0.0.1:1/none",
        scores=sent,  # type: ignore[arg-type]
        judge=Judge(llm, "j"),
        judge_share=1.0,
        spend_cap_usd=5.0,
    )
    live.answered(answer(), QUESTION, [], TRACE)
    finish(live)
    assert llm.requests == []  # nothing is spent when the day's total cannot be read
    assert sent.named("judge_status") == ["failed"] and sent.named("faithful") == []


def test_the_judges_slot_is_given_back_when_an_answer_cannot_be_recorded() -> None:
    live = Online(judge=Judge(ScriptedLLM(FAITHFUL), "j"), judge_share=1.0)
    live.close()  # the service is stopping: nothing more can be queued
    for _ in range(online.MAX_JUDGES_WAITING + 2):
        with pytest.raises(RuntimeError):
            live.answered(answer(), QUESTION, [], TRACE)
    assert live._waiting == 0


@pytest.mark.db
def test_the_judges_version_is_counted_only_for_answers_it_read(fresh_database_url: str) -> None:
    picks = iter([0.9, 0.9, 0.0])
    live = Online(
        database_url=fresh_database_url,
        judge=Judge(ScriptedLLM(FAITHFUL), "j"),
        judge_share=0.5,
        judge_version="abcdef012345",
        pick=lambda: next(picks),
    )
    for _ in range(3):
        live.answered(answer(), QUESTION, [], TRACE)
    finish(live)
    counted = totals(fresh_database_url)
    assert counted["judge"] == {"completed": 1, "not_sampled": 2}
    assert counted["judge_version"] == {"abcdef012345": 1}
