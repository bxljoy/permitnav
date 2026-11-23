"""Phase 4 freshness: unchanged downloads are not stored again, and ingest writes the "what
changed" log (added, changed, removed, re-dated wordings, with a diff for changes)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from permitnav.sources.raw import latest_raw, save_raw
from tests.test_webpage import BASE, FIXTURE, SV, raw_page


def test_an_identical_download_is_not_stored_again(tmp_path: Path) -> None:
    day1 = datetime(2026, 11, 20, tzinfo=UTC)
    day2 = datetime(2026, 11, 21, tzinfo=UTC)
    first = save_raw(tmp_path, "s", "https://x/a", b"same", 200, "html", day1)
    again = save_raw(tmp_path, "s", "https://x/a", b"same", 200, "html", day2)
    # The existing file, but this fetch's time: a page seen again is observed from today.
    assert again.storage_path == first.storage_path and again.fetched_at == day2
    assert len(list(tmp_path.glob("*.html"))) == 1
    changed = save_raw(tmp_path, "s", "https://x/a", b"new", 200, "html", day2)
    assert changed.fetched_at == day2 and len(list(tmp_path.glob("*.html"))) == 2
    latest = latest_raw(tmp_path)
    assert latest is not None and latest.content == b"new"


def changes(conn: psycopg.Connection) -> list[tuple[str, str, str]]:
    rows = conn.execute("SELECT unit_id, kind, diff FROM change_log ORDER BY id").fetchall()
    return [(u.removeprefix(BASE), k, d) for u, k, d in rows]


@pytest.mark.db
def test_ingest_logs_added_changed_and_removed_sections(conn: psycopg.Connection) -> None:
    from permitnav.corpus.store import ingest_page
    from permitnav.parsing.webpage import parse_page

    first = ingest_page(conn, raw_page(FIXTURE, 20), parse_page(FIXTURE, SV))
    assert first.changes == {"added": 5}  # a first ingest adds every section

    edited = FIXTURE.replace("Avgiften är 2 200 kronor.", "Avgiften är 2 500 kronor.")
    edited = edited.replace("Avgiften betalas när du ansöker.", "")  # a section disappears
    assert edited != FIXTURE
    conn.execute("DELETE FROM change_log")
    report = ingest_page(conn, raw_page(edited, 22), parse_page(edited, SV))

    assert report.changes == {"changed": 1, "removed": 1}
    logged = changes(conn)
    (changed,) = [c for c in logged if c[1] == "changed"]
    assert changed[0] == "#vadkostardetattansoka"
    assert "-Avgiften är 2 200 kronor." in changed[2] and "+Avgiften är 2 500 kronor." in changed[2]
    (removed,) = [c for c in logged if c[1] == "removed"]
    assert removed[0] == "#vadkostardetattansoka-2"


@pytest.mark.db
def test_an_unchanged_page_logs_nothing(conn: psycopg.Connection) -> None:
    from permitnav.corpus.store import ingest_page
    from permitnav.parsing.webpage import parse_page

    ingest_page(conn, raw_page(FIXTURE, 20), parse_page(FIXTURE, SV))
    conn.execute("DELETE FROM change_log")
    report = ingest_page(conn, raw_page(FIXTURE, 21), parse_page(FIXTURE, SV))
    assert report.changes == {} and changes(conn) == []


@pytest.mark.db
def test_a_statute_change_is_logged_with_its_diff(conn: psycopg.Connection) -> None:
    from tests.test_ingest import FIXTURE as STATUTE
    from tests.test_ingest import ingest

    ingest(conn, STATUTE, 20)
    conn.execute("DELETE FROM change_log")
    edited = STATUTE.replace("lönen", "månadslönen", 1)
    assert edited != STATUTE
    report = ingest(conn, edited, 22)
    assert report.changes.get("changed", 0) == 1
    ((unit, kind, diff),) = conn.execute("SELECT unit_id, kind, diff FROM change_log").fetchall()
    assert unit.startswith("sfs:2005:716/") and kind == "changed"
    assert "+" in diff and "månadslönen" in diff


@pytest.mark.db
def test_a_disappeared_page_logs_its_sections_as_removed(conn: psycopg.Connection) -> None:
    from permitnav.corpus.store import close_documents, ingest_page
    from permitnav.parsing.webpage import parse_page

    ingest_page(conn, raw_page(FIXTURE, 20), parse_page(FIXTURE, SV))
    conn.execute("DELETE FROM change_log")
    closed = close_documents(conn, keep=set(), at=datetime(2026, 11, 23, tzinfo=UTC))
    assert closed == 5
    kinds = conn.execute("SELECT kind, count(*) FROM change_log GROUP BY kind").fetchall()
    assert kinds == [("removed", 5)]


@pytest.mark.db
def test_a_corrected_ruling_is_logged_as_changed_with_its_diff(conn: psycopg.Connection) -> None:
    import json

    from permitnav.corpus.store import ingest_rulings
    from permitnav.parsing.ruling import parse_rulings
    from permitnav.sources.domstol import API
    from permitnav.sources.raw import RawFetch
    from tests.test_ruling import NO_TEXT

    def raw(content: bytes, day: int) -> RawFetch:
        at = datetime(2026, 11, day, tzinfo=UTC)
        return RawFetch("domstol", f"{API}?domstolkod=MIOD", at, content, f"h{day}", 200)

    summary_only = json.dumps([NO_TEXT]).encode()
    full = json.dumps([{**NO_TEXT, "innehall": "<p>Domskälen.</p>"}]).encode()
    ingest_rulings(conn, raw(summary_only, 20), parse_rulings(summary_only))
    conn.execute("DELETE FROM change_log")
    report = ingest_rulings(conn, raw(full, 22), parse_rulings(full))
    assert report.changes == {"added": 0, "changed": 1}
    ((kind, diff),) = conn.execute("SELECT kind, diff FROM change_log").fetchall()
    assert kind == "changed" and "+Domskälen." in diff


def test_replaced_wordings_are_paired_with_the_most_similar_new_text() -> None:
    from unittest.mock import MagicMock

    from permitnav.corpus.store import IngestReport, _log_changes, _Stored

    old = _Stored(1, "u", "h", None, None, None, False, "valid")
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = ("Lönen ska vara 90 procent av medianlönen.",)
    report = IngestReport(download_id=1)
    inserted = [
        (10, "Helt ny bestämmelse om något annat som börjar 2028."),  # a future wording
        (11, "Lönen ska vara 80 procent av medianlönen."),  # the replacement
    ]
    raw = MagicMock(fetched_at=datetime(2026, 11, 23, tzinfo=UTC))
    _log_changes(conn, "u", [old], inserted, [], raw, 1, report)
    rows = [c.args[1] for c in conn.execute.call_args_list if "INSERT INTO change_log" in c.args[0]]
    kinds = {(r[3], r[4], r[5]) for r in rows}  # (kind, old, new)
    assert ("changed", 1, 11) in kinds and ("added", None, 10) in kinds
