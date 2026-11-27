"""Counts per day (permitnav.stats): a fixed vocabulary, sums only."""

from __future__ import annotations

import psycopg
import pytest

from permitnav import stats


@pytest.mark.db
def test_facts_add_up_per_day_measure_and_value(conn: psycopg.Connection) -> None:
    stats.add(conn, {"kind": "answered", "second_call": "no"})
    stats.add(conn, {"kind": "answered", "second_call": "yes"})
    stats.add(conn, {"kind": "follow_up", "second_call": "no"})
    stats.add(conn, {"rating": "helpful"})
    counts = stats.since(conn, 1)
    assert len({c.day for c in counts}) == 1  # all today
    assert stats.totals(counts) == {
        "kind": {"answered": 2, "follow_up": 1},
        "second_call": {"no": 2, "yes": 1},
        "rating": {"helpful": 1},
    }


@pytest.mark.db
def test_only_the_last_days_are_read(conn: psycopg.Connection) -> None:
    conn.execute(
        "INSERT INTO daily_count (day, measure, value, n) VALUES"
        " ((now() AT TIME ZONE 'Europe/Stockholm')::date - 40, 'kind', 'answered', 7),"
        " ((now() AT TIME ZONE 'Europe/Stockholm')::date - 3, 'kind', 'answered', 2)"
    )
    stats.add(conn, {"kind": "answered"})
    assert stats.totals(stats.since(conn, 30)) == {"kind": {"answered": 3}}
    assert stats.totals(stats.since(conn, 1)) == {"kind": {"answered": 1}}
    assert stats.totals(stats.since(conn, 365)) == {"kind": {"answered": 10}}


@pytest.mark.db
@pytest.mark.parametrize(
    "facts",
    [{"kind": "Can I change employer?"}, {"question": "yes"}, {"kind": "answered", "rating": "x"}],
)
def test_nothing_outside_the_vocabulary_is_stored(
    conn: psycopg.Connection, facts: dict[str, str]
) -> None:
    """The table must not be a place where a person's text can end up."""
    with pytest.raises(ValueError, match="not a known count"):
        stats.add(conn, facts)
    assert stats.since(conn, 30) == []  # not even the valid part of a refused call


def test_the_report_gives_each_values_count_and_share() -> None:
    from datetime import date

    from permitnav.cli import stats_report

    day, next_day = date(2026, 11, 27), date(2026, 11, 28)
    counts = [
        stats.Count(day, "kind", "answered", 6),
        stats.Count(next_day, "kind", "answered", 1),
        stats.Count(day, "kind", "follow_up", 1),
        stats.Count(day, "second_call", "no", 7),
        stats.Count(day, "second_call", "yes", 1),
        stats.Count(next_day, "faithful", "no", 1),
    ]
    assert stats_report(counts, 30) == [
        "last 30 days (2026-11-27 to 2026-11-28, 2 with questions)",
        "  kind                8  answered 7 (88%), follow_up 1 (12%)",
        "  second_call         8  yes 1 (12%), no 7 (88%)",
        "  faithful            1  no 1 (100%)",
    ]
    assert stats_report([], 7) == ["no questions counted in the last 7 days"]
