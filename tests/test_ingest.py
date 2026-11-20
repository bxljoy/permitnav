"""Storing parsed statutes as dated versions (corpus.store), against a real Postgres."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path

import psycopg
import pytest

from permitnav.corpus.store import ingest_statute, versions_on
from permitnav.parsing.statute import parse_statute
from permitnav.sources.riksdagen import RawFetch

pytestmark = pytest.mark.db

FIXTURE = (Path(__file__).parent / "fixtures" / "utlanningslagen_excerpt.txt").read_text(
    encoding="utf-8"
)
URL = "https://data.riksdagen.se/dokument/sfs-2005-716.text"
S2 = "sfs:2005:716/6kap/2§"
S2A = "sfs:2005:716/6kap/2a§"
S4 = "sfs:2005:716/6kap/4§"


def ingest(conn: psycopg.Connection, text: str, day: int):
    raw = RawFetch(
        source="riksdagen",
        url=URL,
        fetched_at=datetime(2026, 11, day, 9, 0, tzinfo=UTC),
        content=text.encode(),
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        http_status=200,
    )
    return ingest_statute(conn, raw, parse_statute(text, url=URL))


def count(conn: psycopg.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()  # type: ignore[arg-type]
    assert row is not None
    return row[0]


def test_first_ingest_stores_every_wording(conn: psycopg.Connection) -> None:
    report = ingest(conn, FIXTURE, 20)
    parsed = parse_statute(FIXTURE, url=URL)
    assert report.new_versions == len(parsed.units)
    assert report.unchanged_versions == report.closed_versions == 0
    assert count(conn, "SELECT count(*) FROM unit_version") == len(parsed.units)
    assert report.links > 0


def test_the_right_wording_applies_on_each_date(conn: psycopg.Connection) -> None:
    ingest(conn, FIXTURE, 20)
    (today,) = versions_on(conn, S4, date(2026, 11, 20))
    (later,) = versions_on(conn, S4, date(2028, 2, 1))
    assert today["valid_to"] == date(2028, 1, 1)
    assert later["valid_from"] == date(2028, 1, 1)
    # The wording of 6 kap. 2 § before 2026-06-01 is not in the API text (D10, phase 4).
    assert versions_on(conn, S2, date(2026, 5, 1)) == []


def test_reingesting_the_same_text_changes_nothing(conn: psycopg.Connection) -> None:
    first = ingest(conn, FIXTURE, 20)
    second = ingest(conn, FIXTURE, 21)
    assert second.new_versions == second.closed_versions == second.updated_versions == 0
    assert second.unchanged_versions == first.new_versions
    assert count(conn, "SELECT count(*) FROM download") == 2


def test_a_dated_change_closes_the_old_wording_when_the_new_one_starts(
    conn: psycopg.Connection,
) -> None:
    ingest(conn, FIXTURE, 20)
    changed = FIXTURE.replace(
        "endast till ett visst slag av arbete. Lag (2022:303).",
        "endast till ett visst yrke. Lag (2027:1).",
    )
    changed = changed.replace(
        "2026:1687\n", "2027:1\n\n1. Denna lag träder i kraft den 1 mars 2027.\n\n2026:1687\n"
    )
    assert changed.count("Lag (2027:1).") == 1, "fixture edit did not apply"
    report = ingest(conn, changed, 22)
    # New: the changed 2 a § and the new act's transitional point. Closed: the old 2 a §.
    assert (report.new_versions, report.closed_versions) == (2, 1)
    (before,) = versions_on(conn, S2A, date(2027, 2, 28))
    (after,) = versions_on(conn, S2A, date(2027, 3, 1))
    assert before["valid_to"] == date(2027, 3, 1) and not before["dates_estimated"]
    assert after["valid_from"] == date(2027, 3, 1) and after["amended_by"] == "2027:1"
    kinds = {
        row[0]
        for row in conn.execute(
            "SELECT kind FROM unit_link WHERE from_unit_id = 'sfs:2027:1/övergång/1'"
        ).fetchall()
    }
    assert kinds == {"transitional_introduced", "transitional_ended"}


def test_an_undated_change_is_closed_on_the_day_it_was_seen_and_marked_estimated(
    conn: psycopg.Connection,
) -> None:
    ingest(conn, FIXTURE, 20)
    changed = FIXTURE.replace(
        "Arbetstillstånd ska ges för viss tid.", "Arbetstillstånd ges för viss tid."
    )
    report = ingest(conn, changed, 22)
    assert report.estimated == ["sfs:2005:716/6kap/1§"]
    (old,) = versions_on(conn, "sfs:2005:716/6kap/1§", date(2026, 11, 21))
    (new,) = versions_on(conn, "sfs:2005:716/6kap/1§", date(2026, 11, 22))
    assert old["valid_to"] == new["valid_from"] == date(2026, 11, 22)
    assert old["dates_estimated"] and new["dates_estimated"]


def test_a_pending_start_never_applies_until_it_gets_a_date(conn: psycopg.Connection) -> None:
    pending = FIXTURE.replace(
        "4 § /Träder i kraft I:2028-01-01/",
        "4 § /Träder i kraft I:den dag som regeringen bestämmer/",
    ).replace(
        "4 § /Upphör att gälla U:2028-01-01/",
        "4 § /Upphör att gälla U:den dag som regeringen bestämmer/",
    )
    ingest(conn, pending, 20)
    (only,) = versions_on(conn, S4, date(2030, 1, 1))
    assert only["pending"] == "ends"  # the current wording still applies
    report = ingest(conn, FIXTURE, 21)  # the government has now set 2028-01-01
    assert report.updated_versions == 2 and report.new_versions == 0
    (later,) = versions_on(conn, S4, date(2028, 2, 1))
    assert later["valid_from"] == date(2028, 1, 1) and later["pending"] is None


def test_a_repealed_section_ends_when_the_repealing_act_takes_effect(
    conn: psycopg.Connection,
) -> None:
    ingest(conn, FIXTURE, 20)
    start = FIXTURE.index("2 c § Ett arbetstillstånd")
    end = FIXTURE.index("\n\n", FIXTURE.index("Lag (", start))
    repealed = FIXTURE[:start] + "2 c § Har upphävts genom lag (2027:2)." + FIXTURE[end:]
    repealed = repealed.replace(
        "2026:1687\n", "2027:2\n\n1. Denna lag träder i kraft den 1 april 2027.\n\n2026:1687\n"
    )
    report = ingest(conn, repealed, 22)
    assert report.closed_versions == 1
    assert versions_on(conn, "sfs:2005:716/6kap/2c§", date(2027, 3, 31))
    assert versions_on(conn, "sfs:2005:716/6kap/2c§", date(2027, 4, 1)) == []


# --- regressions from the slice 2 code review (2026-11-22) -------------------------------------

UPCOMING_4 = (
    "4 § /Träder i kraft I:2028-01-01/\nEn utlänning som vill ha arbetstillstånd i Sverige ska ha"
)
CURRENT_4 = (
    "4 § /Upphör att gälla U:2028-01-01/\nEn utlänning som vill ha arbetstillstånd i Sverige ska ha"
)


def links(conn: psycopg.Connection) -> int:
    return count(conn, "SELECT count(*) FROM unit_link")


def test_an_upcoming_wording_changed_before_it_starts_is_withdrawn(
    conn: psycopg.Connection,
) -> None:
    ingest(conn, FIXTURE, 20)
    assert FIXTURE.count(UPCOMING_4) == 1
    changed = FIXTURE.replace(UPCOMING_4, UPCOMING_4 + " ansökt i god tid och", 1)
    report = ingest(conn, changed, 22)
    assert (report.withdrawn_versions, report.new_versions) == (1, 1)
    (later,) = versions_on(conn, S4, date(2028, 2, 1))
    assert "i god tid" in later["text"]


def test_a_cancelled_upcoming_change_is_withdrawn_and_the_current_wording_keeps_applying(
    conn: psycopg.Connection,
) -> None:
    ingest(conn, FIXTURE, 20)
    start = FIXTURE.index(UPCOMING_4)
    end = FIXTURE.index("Förlängning av tillståndstiden", start)
    cancelled = FIXTURE[:start] + FIXTURE[end:]
    cancelled = cancelled.replace("4 § /Upphör att gälla U:2028-01-01/\n", "4 § ", 1)
    report = ingest(conn, cancelled, 22)
    assert report.withdrawn_versions == 1
    (still,) = versions_on(conn, S4, date(2028, 2, 1))
    assert still["valid_to"] is None  # the withdrawn end date is cleared


def test_a_corrected_current_wording_and_an_upcoming_one_do_not_overlap(
    conn: psycopg.Connection,
) -> None:
    ingest(conn, FIXTURE, 20)
    corrected = FIXTURE.replace(CURRENT_4, CURRENT_4 + " i förväg", 1)
    report = ingest(conn, corrected, 22)
    assert report.estimated == [S4]
    (now,) = versions_on(conn, S4, date(2026, 12, 1))
    assert "i förväg" in now["text"] and now["valid_from"] == date(2026, 11, 22)
    assert now["valid_to"] == date(2028, 1, 1)


def test_an_estimate_is_not_given_to_a_pending_wording(conn: psycopg.Connection) -> None:
    pending_new = FIXTURE.replace(
        "4 § /Träder i kraft I:2028-01-01/",
        "4 § /Träder i kraft I:den dag som regeringen bestämmer/",
    ).replace(
        "4 § /Upphör att gälla U:2028-01-01/",
        "4 § /Upphör att gälla U:den dag som regeringen bestämmer/",
    )
    ingest(conn, FIXTURE.replace("Arbetstillstånd ska ges för viss tid.", "Gammal."), 20)
    report = ingest(conn, pending_new, 22)  # 1 § changes (estimated) and 4 § goes pending
    assert report.estimated == ["sfs:2005:716/6kap/1§"]
    row = conn.execute(
        "SELECT dates_estimated, valid_from FROM unit_version"
        " WHERE unit_id = %s AND pending = 'starts'",
        (S4,),
    ).fetchone()
    assert row == (False, None)


def test_a_real_start_date_replaces_the_estimate_on_both_sides_of_the_change(
    conn: psycopg.Connection,
) -> None:
    s1 = "sfs:2005:716/6kap/1§"
    ingest(conn, FIXTURE.replace("Arbetstillstånd ska ges för viss tid.", "Gammal."), 20)
    ingest(conn, FIXTURE, 22)  # 1 § changed; no date in the source: estimated 2026-11-22
    # Later the source names the act and its in-force date for the new wording of 1 §.
    dated = FIXTURE.replace(
        "2026:1687\n", "2014:198\n\n1. Denna lag träder i kraft den 1 april 2026.\n\n2026:1687\n"
    )
    report = ingest(conn, dated, 24)
    assert report.updated_versions >= 1
    (before,) = versions_on(conn, s1, date(2026, 3, 31))
    (after,) = versions_on(conn, s1, date(2026, 4, 1))
    assert before["text"].startswith("Gammal.") and before["valid_to"] == date(2026, 4, 1)
    assert after["valid_from"] == date(2026, 4, 1)
    assert not before["dates_estimated"] and not after["dates_estimated"]


def test_identical_texts_with_different_dates_are_kept_apart(conn: psycopg.Connection) -> None:
    same = FIXTURE.replace(
        UPCOMING_4 + " \nansökt",
        UPCOMING_4 + " \nansökt",
    )
    # Make the upcoming 4 § textually identical to the current one.
    start_cur = same.index(CURRENT_4)
    start_up = same.index(UPCOMING_4)
    current_body = same[start_cur + len(CURRENT_4) : same.index("\n\n", start_cur)]
    end_up = same.index("\n\n", start_up)
    same = same[: start_up + len(UPCOMING_4)] + current_body + same[end_up:]
    first = ingest(conn, same, 20)
    second = ingest(conn, same, 21)
    assert second.new_versions == 0 and second.unchanged_versions == first.new_versions
    assert len(versions_on(conn, S4, date(2026, 12, 1))) == 1
    assert len(versions_on(conn, S4, date(2028, 2, 1))) == 1


def test_the_same_download_ingested_twice_is_recorded_once(conn: psycopg.Connection) -> None:
    first = ingest(conn, FIXTURE, 20)
    again = ingest(conn, FIXTURE, 20)
    assert again.already_recorded and again.download_id == first.download_id
    assert count(conn, "SELECT count(*) FROM download") == 1


def test_link_counts_match_the_rows_stored(conn: psycopg.Connection) -> None:
    report = ingest(conn, FIXTURE, 20)
    assert report.links == links(conn)
    again = ingest(conn, FIXTURE, 21)
    assert again.links == links(conn)


def test_a_replaced_version_has_its_pending_end_cleared(conn: psycopg.Connection) -> None:
    pending = FIXTURE.replace(
        "4 § /Träder i kraft I:2028-01-01/",
        "4 § /Träder i kraft I:den dag som regeringen bestämmer/",
    ).replace(
        "4 § /Upphör att gälla U:2028-01-01/",
        "4 § /Upphör att gälla U:den dag som regeringen bestämmer/",
    )
    ingest(conn, pending, 20)
    # The government sets 2028-01-01, and the current wording's text is corrected too.
    ingest(conn, FIXTURE.replace(CURRENT_4, CURRENT_4 + " i förväg", 1), 22)
    rows = conn.execute(
        "SELECT pending FROM unit_version WHERE unit_id = %s AND observed_to IS NOT NULL", (S4,)
    ).fetchall()
    assert rows == [(None,)]
