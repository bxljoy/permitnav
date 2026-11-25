"""The daily spend cap: what the site's own model calls cost per day, and a stop at a limit.

It exists for one risk: a leaked access token. Without it, whoever holds the token can run up
model costs until someone notices; with it, at most the cap per day. It is one limit for the
whole site, not a quota per person (that needs accounts). A leaked OpenRouter key is a
different problem (its holder never calls this server): that needs a credit limit on the key
at OpenRouter.

Only the API's service is metered. Evals and `permitnav ask` are the maintainer's own use.
The table holds sums per Stockholm calendar day and nothing about a question or who asked.
"""

from __future__ import annotations

import logging
import math
import os
import threading
from dataclasses import dataclass
from datetime import date

import psycopg

from permitnav import db
from permitnav.llm.port import LLM, Completion, CompletionRequest, LLMError

log = logging.getLogger("permitnav.spend")

# Counted for a call whose cost the provider did not report (or reported as zero, negative or
# not a number), so that missing prices cannot switch the cap off, and a nonsense one cannot
# close the site. About five times a usual answer call (DESIGN.md: $0.002).
UNPRICED_ESTIMATE_USD = 0.01
# Counted for a call that ended in an error: the adapter makes up to three attempts, and a
# timed-out or unreadable reply can still be billed.
FAILED_ESTIMATE_USD = 3 * UNPRICED_ESTIMATE_USD
TODAY = "(now() AT TIME ZONE 'Europe/Stockholm')::date"


class SpendCapReached(RuntimeError):
    """Today's total has reached the cap: no further model calls until the next day."""


@dataclass(frozen=True)
class Day:
    day: date
    usd: float
    calls: int
    unpriced: int
    refused: int


def cap_usd() -> float | None:
    """DAILY_SPEND_CAP_USD from the environment; unset or empty: no cap (development)."""
    value = os.environ.get("DAILY_SPEND_CAP_USD", "").strip()
    if not value:
        return None
    try:
        cap = float(value)
    except ValueError:
        cap = -1.0
    if not 0 < cap < float("inf"):
        raise ValueError(f"DAILY_SPEND_CAP_USD must be a positive number of dollars: {value!r}")
    return cap


def priced(cost_usd: float | None) -> bool:
    """Whether a reported cost can be taken at face value."""
    return cost_usd is not None and math.isfinite(cost_usd) and cost_usd > 0


def record(conn: psycopg.Connection, usd: float, calls: int = 1, unpriced: int = 0) -> None:
    """Add model calls to today's total (`usd`: their cost, estimates included)."""
    conn.execute(
        f"INSERT INTO spend (day, usd, calls, unpriced) VALUES ({TODAY}, %(usd)s, %(calls)s,"  # noqa: S608
        " %(unpriced)s) ON CONFLICT (day) DO UPDATE SET usd = spend.usd + %(usd)s,"
        " calls = spend.calls + %(calls)s, unpriced = spend.unpriced + %(unpriced)s",
        {"usd": usd, "calls": calls, "unpriced": unpriced},
    )


def record_call(conn: psycopg.Connection, cost_usd: float | None) -> None:
    """Add one finished model call: its reported cost, or the estimate when it has none."""
    if priced(cost_usd):
        record(conn, cost_usd or 0.0)
    else:
        record(conn, UNPRICED_ESTIMATE_USD, unpriced=1)


def spent_today(conn: psycopg.Connection) -> float:
    row = conn.execute(f"SELECT usd FROM spend WHERE day = {TODAY}").fetchone()  # noqa: S608
    return float(row[0]) if row else 0.0


def reached(conn: psycopg.Connection, cap: float | None) -> bool:
    """Whether today's total has reached `cap`; a True is counted as a declined question.
    It returns instead of raising so that the caller's transaction, with that count, commits.
    Asked once per question, before its first model call: questions already in progress
    finish (the server runs at most 40 at a time), so a day can end that much over."""
    if cap is None or spent_today(conn) < cap:
        return False
    conn.execute(
        f"INSERT INTO spend (day, refused) VALUES ({TODAY}, 1)"  # noqa: S608
        " ON CONFLICT (day) DO UPDATE SET refused = spend.refused + 1"
    )
    return True


def days(conn: psycopg.Connection, last: int = 7) -> list[Day]:
    """The most recent days with any spend or refusal, newest first."""
    rows = conn.execute(
        "SELECT day, usd, calls, unpriced, refused FROM spend ORDER BY day DESC LIMIT %s", (last,)
    ).fetchall()
    return [Day(r[0], float(r[1]), r[2], r[3], r[4]) for r in rows]


def unreported(conn: psycopg.Connection) -> list[Day]:
    """Finished days on which questions were declined and that have not been reported yet,
    oldest first. Every such day, not only yesterday: a refresh that failed or did not run
    must not lose one."""
    rows = conn.execute(
        "SELECT day, usd, calls, unpriced, refused FROM spend"  # noqa: S608
        f" WHERE refused > 0 AND NOT reported AND day < {TODAY} ORDER BY day"
    ).fetchall()
    return [Day(r[0], float(r[1]), r[2], r[3], r[4]) for r in rows]


def mark_reported(conn: psycopg.Connection) -> int:
    """Remember that the finished capped days were reported; returns how many."""
    return conn.execute(
        f"UPDATE spend SET reported = true WHERE refused > 0 AND NOT reported AND day < {TODAY}"  # noqa: S608
    ).rowcount


class MeteredLLM:
    """An LLM whose every call is added to the day's total, each in a transaction of its
    own (the call was paid for whatever happens to the question afterwards). A call that
    ends in an error is counted with an estimate. What could not be written (the database
    was away for a moment) is kept in memory and written with the next call."""

    def __init__(self, inner: LLM, database_url: str | None = None) -> None:
        self._inner = inner
        self._database_url = database_url
        self._lock = threading.Lock()
        self._owed = (0.0, 0, 0)  # dollars, calls, unpriced calls not yet written

    def complete(self, request: CompletionRequest) -> Completion:
        try:
            completion = self._inner.complete(request)
        except LLMError:
            self._add(FAILED_ESTIMATE_USD, unpriced=1)
            raise
        if priced(completion.cost_usd):
            self._add(completion.cost_usd or 0.0)
        else:
            self._add(UNPRICED_ESTIMATE_USD, unpriced=1)
        return completion

    def _add(self, usd: float, unpriced: int = 0) -> None:
        with self._lock:
            owed = self._owed
            self._owed = (0.0, 0, 0)
        usd, calls, unpriced = owed[0] + usd, owed[1] + 1, owed[2] + unpriced
        try:
            with db.connect(self._database_url) as conn:
                record(conn, usd, calls, unpriced)
        except psycopg.Error as error:
            # The answer is not lost over bookkeeping, and the amount is not lost either.
            with self._lock:
                self._owed = (self._owed[0] + usd, self._owed[1] + calls, self._owed[2] + unpriced)
            log.error("model spend could not be recorded yet: %s", type(error).__name__)
