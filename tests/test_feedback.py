"""Feedback a person chose to save (phase 6, slice 3): stored privately, refusable,
withdrawable, never in the public snapshot."""

from __future__ import annotations

from datetime import date

import psycopg
import pytest

from permitnav import feedback
from permitnav.feedback import FeedbackRefused, identifiers_in

UNIT = "sfs:2005:716/6kap/2§"
ARGS = ("Lönekrav?", "Svar [S1].", "Fel belopp.", "answered", date(2026, 11, 25), [UNIT])
CODE = "c" * 43


def add_unit(conn: psycopg.Connection, unit_id: str = UNIT) -> None:
    conn.execute(
        "INSERT INTO unit (id, source_type, authority, language, document, heading, url)"
        " VALUES (%s, 'law', 'Riksdagen', 'sv', 'sfs:2005:716', '2 §', 'https://example.org')"
        " ON CONFLICT DO NOTHING",
        (unit_id,),
    )


@pytest.mark.db
def test_a_case_is_stored_as_given_with_nothing_that_ties_it_to_a_request(conn) -> None:
    add_unit(conn)
    feedback.save(conn, CODE, *ARGS)
    (case,) = feedback.waiting(conn)
    assert (case.question, case.answer, case.comment) == ARGS[:3]
    assert (case.kind, case.as_of, case.cited) == ("answered", date(2026, 11, 25), [UNIT])
    rows = conn.execute(
        "SELECT column_name, data_type, column_default FROM information_schema.columns"
        " WHERE table_name = 'feedback'"
    ).fetchall()
    columns = {row[0]: row[1] for row in rows}
    assert columns["created_on"] == "date"  # a day, never a moment an access log could match
    assert not {"ip", "token", "user_agent", "session", "trace_id"} & set(columns)
    assert not any("nextval" in (row[2] or "") for row in rows)  # no counter: ids are random
    stored = conn.execute("SELECT withdraw_hash FROM feedback").fetchone()
    assert stored is not None and CODE not in stored[0]  # only the hash


@pytest.mark.db
def test_ids_do_not_follow_the_order_of_arrival(conn: psycopg.Connection) -> None:
    for n in range(12):
        feedback.save(conn, f"{n:02d}" + "c" * 41, f"fråga {n:02d}", *ARGS[1:])
    arrival = [c.id for c in sorted(feedback.waiting(conn), key=lambda c: c.question)]
    assert len(set(arrival)) == 12 and arrival != sorted(arrival)


@pytest.mark.db
def test_saving_again_with_the_same_code_replaces_the_row(conn: psycopg.Connection) -> None:
    feedback.save(conn, CODE, *ARGS)  # the reply to this one was lost; the person retries
    feedback.save(conn, CODE, "Lönekrav, redigerad?", *ARGS[1:])
    (case,) = feedback.waiting(conn)
    assert case.question == "Lönekrav, redigerad?"


@pytest.mark.db
def test_withdrawing_needs_the_code_and_removes_the_row(conn: psycopg.Connection) -> None:
    feedback.save(conn, CODE, *ARGS)
    assert not feedback.withdraw(conn, "d" * 43)
    assert len(feedback.waiting(conn)) == 1
    assert feedback.withdraw(conn, CODE)
    assert feedback.waiting(conn) == []
    assert not feedback.withdraw(conn, CODE)  # already gone


@pytest.mark.db
def test_identifiers_are_refused_wherever_they_are(conn: psycopg.Connection) -> None:
    email = " kontakta anna.svensson@example.com"
    question, answer, comment, kind, as_of, cited = ARGS
    for texts in ((question + email, answer, comment), (question, answer + email, comment),
                  (question, answer, comment + email)):  # fmt: skip
        with pytest.raises(FeedbackRefused, match="email address"):
            feedback.save(conn, CODE, *texts, kind, as_of, cited)
    with pytest.raises(FeedbackRefused, match="personal number"):
        feedback.save(conn, CODE, "Mitt personnummer är 19811218-9876", *ARGS[1:])
    with pytest.raises(FeedbackRefused, match="character"):
        feedback.save(conn, CODE, "fråga\x00", *ARGS[1:])
    assert feedback.waiting(conn) == []


@pytest.mark.db
def test_only_the_ids_of_real_sources_are_kept_as_cited(conn: psycopg.Connection) -> None:
    add_unit(conn)
    smuggled = ["anna.svensson@example.com 19811218-9876", UNIT, "", "sfs:no-such-unit"]
    feedback.save(conn, CODE, *ARGS[:5], smuggled)
    (case,) = feedback.waiting(conn)
    assert case.cited == [UNIT]


def test_only_real_identifiers_are_flagged() -> None:
    number = ["a Swedish personal number"]
    assert identifiers_in("personnummer 811218-9876") == number  # the well-known test number
    assert identifiers_in("198112189876") == number
    assert identifiers_in("samordningsnummer 701063-2391") == number
    assert identifiers_in("fråga", "svar", "mejla andré@exempel.se") == ["an email address"]
    ordinary = (
        "Enligt 6 kap. 2 § (SFS 2005:716) krävs 34 470 kronor från 2026-06-01, se MIG 2015:11 "
        "och RS/084/2021; ansökan registrerades 2026-11-25 och gäller i 24 månader. "
        "Arbetsgivaren (org.nr 556677-8899) nås på 0701234567 eller 0703124560; ärende 811218-9875."
    )
    assert identifiers_in("född 070312-4560") == number  # written as a number, it still counts
    assert (
        identifiers_in(ordinary) == []
    )  # an organisation number, a phone number, a bad check digit


@pytest.mark.db
def test_the_queue_is_bounded_and_reviewed_cases_are_deleted(conn, monkeypatch) -> None:
    monkeypatch.setattr(feedback, "MAX_WAITING", 2)
    for n in range(2):
        feedback.save(conn, str(n) * 43, *ARGS)
    with pytest.raises(FeedbackRefused, match="full"):
        feedback.save(conn, CODE, *ARGS)
    feedback.save(conn, "0" * 43, "Redigerad?", *ARGS[1:])  # a retry is not a new row
    first, second = (c.id for c in feedback.waiting(conn))
    assert feedback.delete(conn, [first, "no-such-id"]) == 1
    assert [c.id for c in feedback.waiting(conn)] == [second]


def test_feedback_is_never_part_of_the_public_snapshot() -> None:
    from permitnav.corpus import snapshot

    tables = {name for name, _ in snapshot.TABLES}
    assert "feedback" not in tables and "unit" in tables


@pytest.mark.db
def test_the_maintainer_lists_exports_and_deletes(
    fresh_database_url, monkeypatch, tmp_path, capsys
):
    import argparse
    import json

    from permitnav.cli import cmd_feedback

    with psycopg.connect(fresh_database_url) as conn:
        feedback.save(conn, CODE, *ARGS)
        (saved,) = feedback.waiting(conn)
    monkeypatch.setenv("DATABASE_URL", fresh_database_url)

    def run(action: str, ids: list[str] | None = None, out: str | None = None) -> int:
        return cmd_feedback(argparse.Namespace(action=action, ids=ids or [], out=out))

    assert run("list") == 0
    listed = capsys.readouterr().out
    assert "1 waiting for review" in listed and "Lönekrav?" in listed and "Fel belopp." in listed
    assert saved.id in listed
    out = tmp_path / "private" / "feedback.jsonl"
    assert run("export", out=str(out)) == 0
    (row,) = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert row["question"] == "Lönekrav?" and "withdraw" not in json.dumps(row)
    assert run("export") == 2 and run("delete") == 2  # nothing happens without a target
    capsys.readouterr()
    assert run("export", out="-") == 0  # from the server's container: to stdout
    assert json.loads(capsys.readouterr().out)["question"] == "Lönekrav?"
    assert run("delete", ids=[saved.id]) == 0
    capsys.readouterr()
    run("list")
    assert "0 waiting for review" in capsys.readouterr().out
