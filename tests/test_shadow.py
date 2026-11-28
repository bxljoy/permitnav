"""The shadow judge (permitnav.shadow): a second faithfulness judge read against the
deployed one on the maintainer's own questions. It is off by default, sees exactly what the
deployed judge saw, stops at its sample limit and budget, stores no text, and can change
nothing the deployed judge measured."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime

import psycopg
import pytest

from permitnav import cli, online, shadow, shadow_report, stats, tracing
from permitnav.evals.judge import CalibrationItem, Judge, Verdict
from permitnav.evals.provenance import prompt_hash
from permitnav.llm.port import Completion, CompletionRequest, LLMError
from permitnav.online import Online
from tests.test_online import FAITHFUL, QUESTION, Sent, answer

pytestmark = pytest.mark.db
DEPLOYED, SHADOW = "deployed-model", "shadow-model"
SHADOW_SAYS = json.dumps(
    {"faithful": False, "unsupported": ["SVARSTEXT is required"], "reason": "no source says so"}
)


class TwoJudges:
    """A model client that answers per model, and can fail or misprice the shadow's calls."""

    def __init__(
        self,
        deployed: str = FAITHFUL,
        shadow_reply: object = SHADOW_SAYS,
        cost: float | None = 0.003,
    ):
        self.replies = {DEPLOYED: deployed, SHADOW: shadow_reply}
        self.cost = cost
        self.requests: list[CompletionRequest] = []

    def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        reply = self.replies[request.model]
        if isinstance(reply, Exception):
            raise reply
        return Completion(
            text=str(reply),
            model=request.model,
            input_tokens=700,
            output_tokens=40,
            cost_usd=self.cost,
        )

    def to(self, model: str) -> list[CompletionRequest]:
        return [r for r in self.requests if r.model == model]


def live(url: str, llm: TwoJudges, sent: Sent | None = None, **more) -> Online:
    settings = {"limit": 100, "budget_usd": 1.0, **more}
    return Online(
        database_url=url,
        scores=sent,  # type: ignore[arg-type]
        judge=Judge(llm, DEPLOYED, max_tokens=8000, reasoning_effort="low"),
        judge_share=1.0,
        judge_version="aaaaaaaaaaaa",
        shadow=shadow.Shadow(
            judge=Judge(llm, SHADOW, max_tokens=8000, reasoning_effort="low"),
            model=SHADOW,
            effort="low",
            version="bbbbbbbbbbbb",
            deployed=(DEPLOYED, "low", "aaaaaaaaaaaa"),
            token_limit=8000,
            release="0123456789ab",
            **settings,
        ),
    )


def ask(service: Online, *, mine: bool = True, text: str = "SVARSTEXT [S1].") -> str:
    """One answered question, as the maintainer's own (recorded in full) or someone else's."""
    given = answer()
    given.text = text
    with (
        tracing.request(full=mine, user="maintainer" if mine else None),
        tracing.span("ask") as root,
    ):
        trace_id = tracing.trace_id(root)
        service.answered(given, QUESTION, [], trace_id)
    assert service.settle(5)
    return trace_id or ""


def skips(url: str) -> dict[str, int]:
    with psycopg.connect(url) as conn:
        return shadow.skips(conn)


def stored(url: str) -> list[shadow.Row]:
    with psycopg.connect(url) as conn:
        return shadow.rows(conn)


def test_it_is_off_unless_a_shadow_model_is_named(
    monkeypatch: pytest.MonkeyPatch, fresh_database_url
) -> None:
    monkeypatch.setenv("JUDGE_MODEL", DEPLOYED)
    monkeypatch.setenv("LIVE_JUDGE_SHARE", "1")
    assert shadow.settings() is None
    assert online.shadow_judge(TwoJudges()) is None
    llm = TwoJudges()
    plain = Online(database_url=fresh_database_url, judge=Judge(llm, DEPLOYED), judge_share=1.0)
    ask(plain)
    plain.close()
    assert not llm.to(SHADOW) and stored(fresh_database_url) == []


def test_both_judges_are_given_the_same_thing_and_both_are_recorded(
    fresh_database_url, spans
) -> None:
    llm, sent = TwoJudges(), Sent()
    service = live(fresh_database_url, llm, sent)
    trace_id = ask(service)
    service.close()
    (first,), (second,) = llm.to(DEPLOYED), llm.to(SHADOW)
    assert first.messages == second.messages and first.system == second.system  # nothing rebuilt
    assert first.max_tokens == second.max_tokens == 8000
    (row,) = stored(fresh_database_url)
    assert (row.trace_id, row.state, row.release) == (trace_id, "done", "0123456789ab")
    assert (row.deployed_model, row.deployed_effort, row.deployed_version) == (
        DEPLOYED,
        "low",
        "aaaaaaaaaaaa",
    )
    assert (row.shadow_model, row.shadow_effort, row.shadow_version) == (
        SHADOW,
        "low",
        "bbbbbbbbbbbb",
    )
    assert (row.deployed_faithful, row.deployed_unsupported) == (True, 0)
    assert (row.shadow_status, row.shadow_faithful, row.shadow_unsupported) == (
        "completed",
        False,
        1,
    )
    assert (row.deployed_input_tokens, row.deployed_output_tokens, row.deployed_calls) == (
        700,
        40,
        1,
    )
    assert (row.shadow_input_tokens, row.shadow_output_tokens, row.shadow_calls) == (700, 40, 1)
    assert float(row.deployed_cost_usd or 0) == pytest.approx(0.003)
    assert float(row.shadow_cost_usd or 0) == pytest.approx(0.003)
    assert float(row.charged_usd) == pytest.approx(0.003)  # the real cost, not the estimate
    assert row.deployed_latency_ms >= 0 and row.shadow_latency_ms is not None
    assert row.token_limit == 8000 and row.prompt_hash == prompt_hash("judge_faithfulness.md")
    assert len(row.context_hash) == 64
    by_name = {s.name: dict(s.attributes or {}) for s in spans.get_finished_spans()}
    assert by_name["shadow_judge"]["permitnav.faithful"] == "no"
    assert by_name["shadow_judge"]["permitnav.agrees_with_deployed"] == "no"
    assert by_name["shadow_judge"]["permitnav.judge_version"] == "bbbbbbbbbbbb"
    # The maintainer's own trace holds the shadow's quotation; the table holds no words.
    assert "SVARSTEXT is required" in str(by_name["shadow_judge"]["langfuse.observation.output"])
    with psycopg.connect(fresh_database_url) as conn:
        everything = str(conn.execute("SELECT * FROM judge_shadow").fetchall())
    assert "SVARSTEXT" not in everything and "no source says so" not in everything
    assert QUESTION not in everything


def test_nothing_the_deployed_judge_measured_is_changed(fresh_database_url) -> None:
    def measured(with_shadow: bool) -> tuple[list, dict]:
        llm, sent = TwoJudges(), Sent()
        service = live(fresh_database_url, llm, sent)
        if not with_shadow:
            service.shadow = None
        trace_id = ask(service)
        service.close()
        with psycopg.connect(fresh_database_url) as conn:
            counted = stats.totals(stats.since(conn, 1))
            conn.execute("DELETE FROM daily_count")
        return [(name, value) for t, name, value in sent.scores if t == trace_id], counted

    without, with_it = measured(False), measured(True)
    assert without == with_it  # the same scores (no new one) and the same daily counts
    assert ("faithful", True) in with_it[0]  # the deployed verdict, not the shadow's


def test_only_the_maintainers_own_questions_are_shadowed(fresh_database_url) -> None:
    llm = TwoJudges()
    service = live(fresh_database_url, llm)
    ask(service, mine=False)
    service.close()
    assert len(llm.to(DEPLOYED)) == 1 and not llm.to(SHADOW)  # no second model saw it
    assert stored(fresh_database_url) == []
    assert skips(fresh_database_url) == {"not_maintainer": 1}  # counted, as never eligible


def test_an_answer_the_deployed_judge_could_not_judge_is_not_shadowed(fresh_database_url) -> None:
    llm = TwoJudges(deployed="not json at all")
    service = live(fresh_database_url, llm)
    ask(service)
    service.close()
    assert not llm.to(SHADOW) and stored(fresh_database_url) == []


@pytest.mark.parametrize("failure", [LLMError("provider down"), RuntimeError("anything else")])
def test_a_shadow_that_fails_changes_nothing_and_is_charged_the_estimate(
    fresh_database_url, caplog, failure: Exception
) -> None:
    llm, sent = TwoJudges(shadow_reply=failure), Sent()
    service = live(fresh_database_url, llm, sent)
    trace_id = ask(service)
    service.close()
    assert (trace_id, "faithful", True) in sent.scores
    assert (trace_id, "judge_status", "completed") in sent.scores
    (row,) = stored(fresh_database_url)
    assert (row.state, row.shadow_status, row.shadow_faithful) == ("done", "failed", None)
    assert float(row.charged_usd) == pytest.approx(shadow.ESTIMATE_USD)
    with psycopg.connect(fresh_database_url) as conn:
        assert shadow.used(conn) == (
            0,
            pytest.approx(shadow.ESTIMATE_USD),
        )  # its place is free again
    assert "provider down" not in caplog.text and "anything else" not in caplog.text


def test_an_unreadable_shadow_reply_is_a_failure_charged_at_least_what_it_cost(
    fresh_database_url,
) -> None:
    llm = TwoJudges(shadow_reply="no verdict here", cost=0.05)
    service = live(fresh_database_url, llm)
    ask(service)
    service.close()
    (row,) = stored(fresh_database_url)
    assert (row.shadow_status, row.shadow_calls) == ("failed", 2)  # asked once more, then given up
    assert float(row.charged_usd) == pytest.approx(0.10)  # more than the estimate: what it cost


def test_a_cost_the_provider_did_not_report_is_charged_as_the_estimate(fresh_database_url) -> None:
    llm = TwoJudges(cost=None)
    service = live(fresh_database_url, llm)
    ask(service)
    service.close()
    (row,) = stored(fresh_database_url)
    assert row.shadow_status == "completed" and row.shadow_cost_usd is None
    assert float(row.charged_usd) == pytest.approx(shadow.ESTIMATE_USD)


def test_it_stops_at_the_sample_limit_also_after_a_restart(fresh_database_url) -> None:
    llm = TwoJudges()
    first = live(fresh_database_url, llm, limit=2)
    ask(first)
    ask(first)
    first.close()
    again = live(fresh_database_url, llm, limit=2)  # a new process: the table remembers
    ask(again)
    again.close()
    assert len(llm.to(SHADOW)) == 2 and len(llm.to(DEPLOYED)) == 3
    assert len(stored(fresh_database_url)) == 2
    assert skips(fresh_database_url) == {"sample_full": 1}


def test_it_stops_before_the_budget_would_be_passed(fresh_database_url) -> None:
    llm = TwoJudges(cost=0.03)
    service = live(fresh_database_url, llm, budget_usd=0.07)
    for _ in range(4):
        ask(service)
    service.close()
    # 0.03 + 0.03 held; a third needs the estimate (0.02) free and 0.06 + 0.02 > 0.07.
    assert len(llm.to(SHADOW)) == 2
    with psycopg.connect(fresh_database_url) as conn:
        assert shadow.used(conn) == (2, pytest.approx(0.06))
    assert skips(fresh_database_url) == {"budget": 2}


def reading(faithful: bool | None = True, cost: float | None = 0.003) -> shadow.Reading:
    status = "failed" if faithful is None else "completed"
    return shadow.Reading(SHADOW, "low", "v", status, faithful, 0, 1, 700, 40, cost, 900)


def take(
    conn: psycopg.Connection, trace_id: str, limit: int = 100, budget: float = 1.0
) -> int | str:
    return shadow.reserve(
        conn,
        trace_id=trace_id,
        limit=limit,
        budget_usd=budget,
        release=None,
        hashes=("p", "c"),
        token_limit=8000,
        deployed=reading(),
        shadow=(SHADOW, "low", "v"),
    )


def test_the_last_place_is_given_once_however_many_ask_at_the_same_time(fresh_database_url) -> None:
    got: list[int | str] = []

    def one(number: int) -> None:
        with psycopg.connect(fresh_database_url) as conn:
            got.append(take(conn, f"trace-{number}", limit=1))

    threads = [threading.Thread(target=one, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(isinstance(r, int) for r in got) == 1 and len(stored(fresh_database_url)) == 1
    assert got.count("sample_full") == 7


def test_a_call_cut_off_keeps_its_place_and_its_estimate(fresh_database_url) -> None:
    with psycopg.connect(fresh_database_url) as conn:
        assert isinstance(take(conn, "cut-off", limit=1), int)  # reserved, never finished
        assert shadow.used(conn) == (1, pytest.approx(shadow.ESTIMATE_USD))
        assert take(conn, "next", limit=1) == "sample_full"
        assert take(conn, "cut-off", limit=5) == "duplicate"  # a trace is never read twice
        with pytest.raises(ValueError, match="not a reason"):
            shadow.skipped(conn, "a person's words")


def test_what_a_finished_call_takes_from_the_budget() -> None:
    assert shadow.charge(reading(cost=0.004)) == 0.004
    assert shadow.charge(reading(cost=None)) == shadow.ESTIMATE_USD
    assert shadow.charge(reading(faithful=None, cost=0.001)) == shadow.ESTIMATE_USD
    assert shadow.charge(reading(faithful=None, cost=0.09)) == 0.09


def test_the_context_hash_follows_what_the_judges_were_given() -> None:
    def item(text: str) -> CalibrationItem:
        return CalibrationItem(
            "i",
            "Q?",
            "en",
            "",
            "",
            "answered",
            "A",
            [{"marker": "S1", "heading": "h", "text": text}],
        )

    assert shadow.context_hash(item("one")) == shadow.context_hash(item("one"))
    assert shadow.context_hash(item("one")) != shadow.context_hash(item("two"))


def test_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHADOW_JUDGE_MODEL", SHADOW)
    assert shadow.settings() == shadow.Settings(SHADOW, None, 100, 1.0)
    monkeypatch.setenv("SHADOW_JUDGE_REASONING_EFFORT", "low")
    monkeypatch.setenv("SHADOW_JUDGE_LIMIT", "25")
    monkeypatch.setenv("SHADOW_JUDGE_BUDGET_USD", "0.5")
    assert shadow.settings() == shadow.Settings(SHADOW, "low", 25, 0.5)
    for name, bad in (
        ("SHADOW_JUDGE_LIMIT", "many"),
        ("SHADOW_JUDGE_BUDGET_USD", "-1"),
        ("SHADOW_JUDGE_BUDGET_USD", "inf"),
        ("SHADOW_JUDGE_REASONING_EFFORT", "max"),
    ):
        with monkeypatch.context() as patch:
            patch.setenv(name, bad)
            with pytest.raises(ValueError, match=name):
                shadow.settings()


def test_the_shadow_judge_takes_the_deployed_judges_token_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHADOW_JUDGE_MODEL", SHADOW)  # no effort of its own
    with pytest.raises(ValueError, match="no live judge"):
        online.shadow_judge(TwoJudges())
    monkeypatch.setenv("JUDGE_MODEL", DEPLOYED)
    monkeypatch.setenv("JUDGE_REASONING_EFFORT", "low")
    monkeypatch.setenv("LIVE_JUDGE_SHARE", "1")
    llm = TwoJudges()
    made = online.shadow_judge(llm, "0123456789ab")
    assert made is not None and made.token_limit == 8000 and made.deployed[:2] == (DEPLOYED, "low")
    made.judge.faithfulness(CalibrationItem("i", "Q?", "en", "", "", "answered", "A", []))
    assert llm.to(SHADOW)[0].max_tokens == 8000 and llm.to(SHADOW)[0].reasoning_effort is None
    assert made.version != made.deployed[2]


def test_no_shadow_call_at_the_days_spend_cap(fresh_database_url) -> None:
    llm = TwoJudges()
    service = live(fresh_database_url, llm)
    service._at_cap = lambda: True  # type: ignore[method-assign]
    verdict = Verdict(None, True, (), "ok")
    service._shadow(
        CalibrationItem("i", "Q?", "en", "", "", "answered", "A", []), "t", verdict, 0.1
    )
    service.close()
    assert not llm.to(SHADOW) and stored(fresh_database_url) == []
    assert skips(fresh_database_url) == {"spend_cap": 1}


def test_the_rows_are_removed_in_one_step(fresh_database_url, monkeypatch, capsys) -> None:
    llm = TwoJudges()
    service = live(fresh_database_url, llm)
    ask(service)
    service.close()
    monkeypatch.setenv("DATABASE_URL", fresh_database_url)
    assert cli.main(["shadow", "status"]) == 0
    assert "places taken: 1" in capsys.readouterr().out
    assert (
        cli.main(["shadow", "clear"]) == 1 and len(stored(fresh_database_url)) == 1
    )  # not without --yes
    assert cli.main(["shadow", "clear", "--yes"]) == 0
    assert "removed 1 shadow rows" in capsys.readouterr().out
    assert stored(fresh_database_url) == [] and skips(fresh_database_url) == {}


def row(number: int, deployed: bool, theirs: bool | None, **more) -> shadow.Row:
    values = dict(
        id=number,
        at=datetime(2026, 12, 1, 9, 0, tzinfo=UTC),
        trace_id=f"{number:032x}",
        release="r",
        prompt_hash="p",
        context_hash="c",
        token_limit=8000,
        state="done",
        charged_usd=0.004,
        deployed_model=DEPLOYED,
        deployed_effort="low",
        deployed_version="a",
        deployed_faithful=deployed,
        deployed_unsupported=0 if deployed else 1,
        deployed_calls=1,
        deployed_input_tokens=7000,
        deployed_output_tokens=300,
        deployed_cost_usd=0.002,
        deployed_latency_ms=1500,
        shadow_model=SHADOW,
        shadow_effort="low",
        shadow_version="b",
        shadow_status="failed" if theirs is None else "completed",
        shadow_faithful=theirs,
        shadow_unsupported=0 if theirs else 1,
        shadow_calls=1,
        shadow_input_tokens=7000,
        shadow_output_tokens=80,
        shadow_cost_usd=0.004,
        shadow_latency_ms=900,
    )
    return shadow.Row(**{**values, **more})  # type: ignore[arg-type]


def test_the_report_puts_disagreements_first_and_samples_the_agreements() -> None:
    rows = [row(1, True, False), row(2, False, True), row(3, False, False), row(4, True, None)]
    rows += [row(n, True, True) for n in range(10, 30)]
    asked: list[str] = []

    def read(trace_id: str) -> shadow_report.Texts:
        asked.append(trace_id)
        return shadow_report.Texts(
            question="MIN FRÅGA",
            answer="MITT SVAR",
            deployed_claims=("the fee is 2 200 kronor for a seasonal permit",),
            shadow_claims=("Ansökan: the fee is 2 200 kronor for a seasonal permit [S7].",),
            shadow_reason="no source gives a fee",
        )

    counted = {"busy": 3, "sample_full": 2, "not_maintainer": 7}
    lines = shadow_report.report(
        rows, limit=100, budget_usd=1.0, agreements=5, seed=1, read=read, skips=counted
    )
    text = "\n".join(lines)
    assert (
        "- eligible answers: 29" in text
        and "- sampled (a shadow call was made or begun): 24" in text
    )
    assert "- skipped: 5 (the shadow judge was still reading the answer before: 3," in text
    assert "never eligible: 7" in text
    assert "not a statistically representative production benchmark" in text
    assert "answers both judges read: 23 of 100" in text and "shadow calls that failed: 1" in text
    assert "the judges agree on 21 of 23" in text
    order = [
        text.index(title)
        for title in (
            "Only the shadow judge says",
            "Only the deployed judge says",
            "Both say not faithful",
            "Both say faithful (a random 5 of 20, seed 1",
        )
    ]
    assert order == sorted(order)
    assert len(asked) == 3 + 5  # every disagreement and both-unfaithful, and the sample
    assert "MIN FRÅGA" in text and "Both quoted a claim in common: **yes**" in text
    assert "Review: [ ] faithful  [ ] not faithful" in text
    assert "7,000" not in text and "161,000 in" in text  # 23 × 7,000 tokens, per judge
    again = shadow_report.report(rows, agreements=5, seed=1, read=read)
    assert "\n".join(again).count("### ") == text.count("### ")  # the same sample for a seed
    bare = "\n".join(shadow_report.report(rows, agreements=5, read=None))
    assert "MIN FRÅGA" not in bare and f"{1:032x}" in bare  # numbers and trace ids only


def test_the_words_of_a_case_come_from_its_trace() -> None:
    judged = json.dumps({"unsupported": ["x is required"], "reason": "faithfulness: none says so"})
    found = shadow_report.texts(
        [
            {"name": "POST /ask", "input": '"Q?"', "output": '"A."'},
            {"name": "answer", "input": '"Q?"', "output": '"A [S1]."'},
            {"name": "live_judge", "output": json.dumps({"unsupported": [], "reason": ""})},
            {"name": "shadow_judge", "output": judged},
        ]
    )
    assert (found.question, found.answer) == ("Q?", "A [S1].")
    assert found.shadow_claims == ("x is required",) and found.shadow_reason == "none says so"
    assert found.deployed_claims == ()
    assert shadow_report.texts([{"name": "answer", "input": None, "output": None}]).problem
    assert shadow_report.same_claim((), ("x",)) is None
    assert (
        shadow_report.same_claim(
            ("something else entirely here",), ("x is required by the law now",)
        )
        is False
    )


def test_an_empty_table_reports_nothing_to_review() -> None:
    assert "Nothing to review yet." in shadow_report.report([])


def test_the_shadow_rows_are_never_part_of_the_public_snapshot() -> None:
    from permitnav.corpus import snapshot

    assert "judge_shadow" not in {name for name, _ in snapshot.TABLES}


def test_the_shadow_never_holds_up_the_deployed_judges_queue(fresh_database_url) -> None:
    """The shadow has its own thread: while it is busy the deployed judge goes on, and an
    answer that arrives meanwhile is judged as ever and merely left out of the sample."""
    release = threading.Event()

    class Slow(TwoJudges):
        def complete(self, request: CompletionRequest) -> Completion:
            if request.model == SHADOW:
                release.wait(5)
            return super().complete(request)

    llm, sent = Slow(), Sent()
    service = live(fresh_database_url, llm, sent)
    mine = tracing.request(full=True, user="maintainer")
    with mine, tracing.span("ask"):
        for number in range(3):
            service.answered(answer(), QUESTION, [], f"{number:032x}")
    deadline = time.monotonic() + 5
    while len(sent.named("faithful")) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(sent.named("faithful")) == 3 and service._waiting == 0  # all judged meanwhile
    release.set()
    assert service.settle(5)
    service.close()
    assert len(llm.to(SHADOW)) == 1 and len(stored(fresh_database_url)) == 1
    assert skips(fresh_database_url) == {"busy": 2}


def test_a_cost_of_zero_is_not_a_price() -> None:
    assert shadow.charge(reading(cost=0.0)) == shadow.ESTIMATE_USD
    assert shadow.charge(reading(cost=float("nan"))) == shadow.ESTIMATE_USD
    free = Verdict(None, True, (), "ok", (Completion("x", "m", 1, 1, 0.0),))
    assert shadow.reading("m", None, None, free, 0.1).cost_usd is None


def test_a_reasoning_shadow_needs_a_reasoning_deployed_judge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JUDGE_MODEL", DEPLOYED)
    monkeypatch.setenv("LIVE_JUDGE_SHARE", "1")
    monkeypatch.delenv("JUDGE_REASONING_EFFORT", raising=False)
    monkeypatch.setenv("SHADOW_JUDGE_MODEL", SHADOW)
    monkeypatch.setenv("SHADOW_JUDGE_REASONING_EFFORT", "high")
    with pytest.raises(ValueError, match="token limit"):
        online.shadow_judge(TwoJudges())


def test_a_trace_that_cannot_be_read_is_one_case_without_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import http.client

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")

    def dropped(*args, **kwargs):
        raise http.client.RemoteDisconnected("closed")

    monkeypatch.setattr(shadow_report.urllib.request, "urlopen", dropped)
    read = shadow_report.langfuse_reader()
    assert read is not None and "RemoteDisconnected" in read("0" * 32).problem


def test_a_short_claim_inside_a_longer_one_is_not_the_same_claim() -> None:
    assert (
        shadow_report.same_claim(("ja",), ("Det är inte sant att jag måste ansöka i förväg",))
        is False
    )
    assert (
        shadow_report.same_claim(
            ("30 days",), ("the permit must be applied for 30 days before it ends",)
        )
        is False
    )
    assert (
        shadow_report.same_claim(("The fee is 2 200 kronor.",), ("the fee is 2 200 kronor [S7]",))
        is True
    )
