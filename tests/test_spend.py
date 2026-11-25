"""The daily spend cap (permitnav.spend)."""

from __future__ import annotations

import psycopg
import pytest

from permitnav import spend
from permitnav.llm.port import Completion, CompletionRequest


def test_no_cap_unless_one_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DAILY_SPEND_CAP_USD", raising=False)
    assert spend.cap_usd() is None
    monkeypatch.setenv("DAILY_SPEND_CAP_USD", " 5 ")
    assert spend.cap_usd() == 5.0
    for wrong in ("five", "0", "-1", "inf", "nan"):
        monkeypatch.setenv("DAILY_SPEND_CAP_USD", wrong)
        with pytest.raises(ValueError, match="DAILY_SPEND_CAP_USD"):
            spend.cap_usd()


@pytest.mark.db
def test_calls_add_up_per_day_and_the_cap_declines(conn: psycopg.Connection) -> None:
    assert not spend.reached(conn, 0.01)  # nothing spent yet
    spend.record_call(conn, 0.004)
    spend.record_call(conn, 0.004)
    assert not spend.reached(conn, 0.01)  # 0.008: under
    spend.record_call(conn, 0.002)
    assert not spend.reached(conn, None)  # no cap: never declines
    assert spend.reached(conn, 0.01)  # reached, not only exceeded
    assert spend.reached(conn, 0.01)
    (today,) = spend.days(conn)
    assert (round(today.usd, 3), today.calls, today.unpriced, today.refused) == (0.01, 3, 0, 2)


@pytest.mark.db
def test_a_cost_that_cannot_be_taken_at_face_value_counts_as_the_estimate(
    conn: psycopg.Connection,
) -> None:
    for cost in (None, 0.0, -1.0, float("nan"), float("inf")):
        spend.record_call(conn, cost)  # missing prices cannot switch the cap off, or close the site
    (today,) = spend.days(conn)
    assert today.usd == pytest.approx(5 * spend.UNPRICED_ESTIMATE_USD)
    assert (today.calls, today.unpriced) == (5, 5)


@pytest.mark.db
def test_every_finished_capped_day_is_reported_once(conn: psycopg.Connection) -> None:
    assert spend.unreported(conn) == []
    conn.execute(
        "INSERT INTO spend (day, usd, calls, refused) VALUES"
        f" ({spend.TODAY}, 5.2, 950, 30), ({spend.TODAY} - 1, 5.01, 900, 12),"
        f" ({spend.TODAY} - 3, 5.0, 880, 1), ({spend.TODAY} - 2, 0.1, 40, 0)"
    )
    days = spend.unreported(conn)  # not today (unfinished), not the day nothing was declined
    assert [(d.refused, d.calls) for d in days] == [(1, 880), (12, 900)]
    assert spend.mark_reported(conn) == 2
    assert spend.unreported(conn) == [] and spend.mark_reported(conn) == 0


class _Priced:
    def __init__(self, cost: float | None) -> None:
        self.cost = cost

    def complete(self, request: CompletionRequest) -> Completion:
        return Completion(text="ok", model=request.model, cost_usd=self.cost)


@pytest.mark.db
def test_a_metered_model_records_each_call_also_one_that_fails(fresh_database_url: str) -> None:
    from permitnav.llm.port import LLMError

    class Failing:
        def complete(self, request: CompletionRequest) -> Completion:
            raise LLMError("timeout after three attempts")

    request = CompletionRequest(model="m", system="s", messages=[])
    llm = spend.MeteredLLM(_Priced(0.25), fresh_database_url)
    assert llm.complete(request).text == "ok"
    assert llm.complete(request).text == "ok"
    with pytest.raises(LLMError):
        spend.MeteredLLM(Failing(), fresh_database_url).complete(request)
    with psycopg.connect(fresh_database_url) as conn:
        (today,) = spend.days(conn)
    assert today.usd == pytest.approx(0.5 + spend.FAILED_ESTIMATE_USD)
    assert (today.calls, today.unpriced) == (3, 1)


@pytest.mark.db
def test_spend_that_could_not_be_written_is_written_with_the_next_call(
    fresh_database_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    request = CompletionRequest(model="m", system="s", messages=[])
    llm = spend.MeteredLLM(_Priced(0.25), "postgresql://nobody@127.0.0.1:1/none?connect_timeout=1")
    assert llm.complete(request).text == "ok"  # the answer is not lost over bookkeeping
    assert llm.complete(request).text == "ok"
    assert "could not be recorded" in caplog.text
    llm._database_url = fresh_database_url  # the database is back
    assert llm.complete(request).text == "ok"
    with psycopg.connect(fresh_database_url) as conn:
        (today,) = spend.days(conn)
    assert (today.usd, today.calls) == (0.75, 3)
