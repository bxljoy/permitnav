"""Counts per day of what happened to the questions asked (online evals).

Langfuse holds the same facts per trace, but its free plan shows only the last 30 days and
cannot be told when to delete; this table keeps the trend. It holds a count per Stockholm
calendar day, measure and value, and nothing else: no text, no trace or conversation id.
Every measure and value is from the vocabulary below, so no caller can store free text.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

import psycopg

from permitnav.spend import TODAY

YES_NO = ("yes", "no")
MEASURES: dict[str, tuple[str, ...]] = {
    # what the person got; "failed": no usable reply even when asked again
    "kind": ("answered", "follow_up", "not_settled", "out_of_scope", "failed"),
    "language": ("sv", "en", "unknown"),  # of the question
    "follow_on": YES_NO,  # asked with earlier questions in the conversation
    "second_call": YES_NO,  # the answer model was asked more than once
    "recovered": YES_NO,  # read from a reply that was not valid JSON
    "other_language": YES_NO,  # answered in another language than the question
    # the live judge: every claim supported by the sources shown; "unread": no verdict
    "faithful": ("yes", "no", "unread"),
    "rating": ("helpful", "not_helpful"),  # a person's click under an answer
}


@dataclass(frozen=True)
class Count:
    day: date
    measure: str
    value: str
    n: int


def add(conn: psycopg.Connection, facts: Mapping[str, str]) -> None:
    """Add one to today's count of each measure's value."""
    for measure, value in facts.items():
        if value not in MEASURES.get(measure, ()):
            raise ValueError(f"not a known count: {measure}={value}")
    for measure, value in facts.items():
        conn.execute(
            f"INSERT INTO daily_count (day, measure, value, n) VALUES ({TODAY}, %s, %s, 1)"  # noqa: S608
            " ON CONFLICT (day, measure, value) DO UPDATE SET n = daily_count.n + 1",
            (measure, value),
        )


def since(conn: psycopg.Connection, days: int = 30) -> list[Count]:
    """The counts of the last `days` days (today included), oldest day first."""
    rows = conn.execute(
        "SELECT day, measure, value, n FROM daily_count"  # noqa: S608
        f" WHERE day > {TODAY} - %s ORDER BY day, measure, value",
        (days,),
    ).fetchall()
    return [Count(*row) for row in rows]


def totals(counts: list[Count]) -> dict[str, dict[str, int]]:
    """Per measure, the sum of each value over the given counts."""
    summed: dict[str, dict[str, int]] = {}
    for count in counts:
        of_measure = summed.setdefault(count.measure, {})
        of_measure[count.value] = of_measure.get(count.value, 0) + count.n
    return summed
