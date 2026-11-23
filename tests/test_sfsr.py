"""The amendment register and the SFS feed: parsing, recording acts, in-force dates."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import psycopg
import pytest

from permitnav.parsing.sfsr import Act, Register, parse_feed, parse_register

# Cut from rkrattsbaser.gov.se/sfsr?bet=2005:716 (fetched 2026-11-23), structure unchanged.
REGISTER = (Path(__file__).parent / "fixtures" / "sfsr_register_excerpt.html").read_text(
    encoding="utf-8"
)

FEED = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Lag om ändring i utlänningslagen (2005:716)</title>
<link>https://svenskforfattningssamling.se/doc/20261900.html</link>
<pubDate>tis, 17 nov 2026 13:00:00 +0100</pubDate><dc:creator>2026:1900</dc:creator></item>
<item><title>Förordning om ändring i utlänningsförordningen (2006:97)</title>
<link>https://svenskforfattningssamling.se/doc/20261901.html</link>
<pubDate>ons, 02 sep 2026 14:00:00 +0200</pubDate><dc:creator>2026:1901</dc:creator></item>
<item><title>Förordning om lån till nätföretag</title>
<link>https://svenskforfattningssamling.se/doc/20261887.html</link>
<pubDate>tis, 17 nov 2026 13:00:00 +0100</pubDate><dc:creator>2026:1887</dc:creator></item>
</channel></rss>"""


def test_the_register_gives_each_acts_date_and_the_statutes_own() -> None:
    register = parse_register(REGISTER, "2005:716")
    assert register.in_force == date(2006, 3, 31)
    acts = {a.sfs: a for a in register.acts}
    assert acts["2026:385"].in_force == date(2026, 6, 1) and not acts["2026:385"].partly_pending
    assert acts["2026:385"].title.startswith("Lag (2026:385) om ändring i utlänningslagen")
    assert acts["2026:385"].scope == "ändr. 1 kap. 1, 7 §§"
    assert acts["2026:1427"].partly_pending and acts["2026:1427"].transitional
    assert acts["2026:1691"].in_force is None


def test_a_page_without_acts_is_a_format_change() -> None:
    with pytest.raises(ValueError, match="format changed"):
        parse_register("<html>nothing here</html>", "2005:716")


def test_the_feed_keeps_acts_that_name_an_in_scope_statute() -> None:
    items = parse_feed(FEED)
    assert [(i.sfs, i.statute, i.published) for i in items] == [
        ("2026:1900", "2005:716", date(2026, 11, 17)),
        ("2026:1901", "2006:97", date(2026, 9, 2)),
    ]


@pytest.fixture
def statute(conn: psycopg.Connection) -> psycopg.Connection:
    from tests.test_ingest import FIXTURE, ingest

    ingest(conn, FIXTURE, 20)
    return conn


def register_for(acts: list[Act]) -> Register:
    return Register("2005:716", date(2006, 3, 31), acts)


def act(sfs: str, in_force: date | None, partly_pending: bool = False) -> Act:
    return Act(sfs, f"Lag ({sfs})", "", in_force, partly_pending, False)


def download(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "INSERT INTO download (source, url, fetched_at, content_hash, storage_path, http_status)"
        " VALUES ('sfsr', 'u', now(), 'h', 'p', 200) RETURNING id"
    ).fetchone()
    assert row is not None
    return row[0]


def undated(conn: psycopg.Connection, act_sfs: str | None) -> int:
    row = conn.execute(
        "SELECT count(*) FROM unit_version v JOIN unit u ON u.id = v.unit_id"
        " WHERE u.source_type = 'statute' AND v.valid_from IS NULL AND NOT v.withdrawn"
        " AND v.amended_by IS NOT DISTINCT FROM %s",
        (act_sfs,),
    ).fetchone()
    return row[0] if row else 0


@pytest.mark.db
def test_undated_wordings_get_their_acts_date_and_originals_the_statutes(statute) -> None:
    from permitnav.corpus.dates import apply_in_force_dates

    conn = statute
    assert undated(conn, "2026:1142") == 4 and undated(conn, None) == 3
    register = register_for([act("2026:1142", date(2026, 7, 1)), act("2014:198", date(2014, 6, 1))])
    seen = datetime(2026, 11, 23, tzinfo=UTC)
    report = apply_in_force_dates(conn, register, download(conn), seen)

    assert report.from_acts == 7 and report.original == 3 and report.conflicts == []
    assert undated(conn, "2026:1142") == 0 and undated(conn, None) == 0
    starts = conn.execute(
        "SELECT DISTINCT valid_from FROM unit_version WHERE amended_by = '2026:1142'"
    ).fetchall()
    assert starts == [(date(2026, 7, 1),)]
    logged = conn.execute(
        "SELECT count(*), min(diff) FROM change_log WHERE kind = 'dates'"
    ).fetchone()
    assert logged is not None and logged[0] == 10
    assert "register" in logged[1]


@pytest.mark.db
def test_partly_pending_and_undated_acts_and_known_dates_are_left_alone(statute) -> None:
    from permitnav.corpus.dates import apply_in_force_dates

    conn = statute
    known = conn.execute(
        "SELECT id, valid_from FROM unit_version WHERE amended_by = '2026:295' LIMIT 1"
    ).fetchone()
    assert known is not None and known[1] is not None
    register = register_for(
        [
            act("2026:1142", date(2027, 12, 1), partly_pending=True),
            act("2014:198", None),
            act("2026:295", date(1999, 1, 1)),  # must not replace a date from the text
        ]
    )
    report = apply_in_force_dates(
        conn, register, download(conn), datetime(2026, 11, 23, tzinfo=UTC)
    )
    assert report.skipped["partly pending"] == 4
    assert report.skipped["no single date in the register"] == 3
    assert report.skipped["act not in the register"] >= 1  # e.g. 2026:1221, 2022:303
    assert undated(conn, "2026:1142") == 4 and undated(conn, "2014:198") == 3
    after = conn.execute("SELECT valid_from FROM unit_version WHERE id = %s", (known[0],))
    assert after.fetchone() == (known[1],)


@pytest.mark.db
def test_recording_acts_reports_only_new_acts_the_register_does_not_have_yet(
    conn: psycopg.Connection,
) -> None:
    from permitnav.corpus.dates import record_acts

    seen = datetime(2026, 11, 23, tzinfo=UTC)
    register = parse_register(REGISTER, "2005:716")
    feed = parse_feed(FEED)
    new = record_acts(conn, [register], feed, seen)
    assert [i.sfs for i in new] == ["2026:1900", "2026:1901"]
    assert record_acts(conn, [register], feed, seen) == []  # seen before: no second notice
    row = conn.execute(
        "SELECT in_register, in_force, partly_pending FROM amending_act WHERE sfs = '2026:385'"
    ).fetchone()
    assert row == (True, date(2026, 6, 1), False)


def test_an_ikraft_with_several_dates_has_no_single_date() -> None:
    page = REGISTER.replace(
        "Ikraft:</span> 2026-06-01 </div>",
        "Ikraft:</span> 2014-07-01, 2015-01-01 för 5 kap. 3 § </div>",
    )
    acts = {a.sfs: a for a in parse_register(page, "2005:716").acts}
    assert acts["2026:385"].in_force is None  # the register does not say which sections wait


def test_an_unreadable_publication_date_is_an_error_not_a_guess() -> None:
    with pytest.raises(ValueError, match="publication date"):
        parse_feed(FEED.replace("tis, 17 nov 2026", "Fri, 01 May 2026"))


@pytest.mark.db
def test_wordings_in_force_until_a_government_decision_still_get_their_start(statute) -> None:
    from permitnav.corpus.dates import apply_in_force_dates

    conn = statute
    conn.execute(
        "UPDATE unit_version SET pending = 'ends'"
        " WHERE amended_by = '2026:1142' AND valid_to IS NULL"
    )
    register = register_for([act("2026:1142", date(2026, 7, 1))])
    report = apply_in_force_dates(
        conn, register, download(conn), datetime(2026, 11, 23, tzinfo=UTC)
    )
    assert report.from_acts == 4 and undated(conn, "2026:1142") == 0


@pytest.mark.db
def test_the_register_corrects_the_statute_a_feed_item_guessed(conn: psycopg.Connection) -> None:
    from permitnav.corpus.dates import record_acts
    from permitnav.parsing.sfsr import FeedItem

    seen = datetime(2026, 11, 23, tzinfo=UTC)
    guessed = FeedItem("2026:385", "Lag om ändring", date(2026, 5, 1), "u", "2006:97")
    record_acts(conn, [], [guessed], seen)
    record_acts(conn, [parse_register(REGISTER, "2005:716")], [], seen)
    row = conn.execute("SELECT statute FROM amending_act WHERE sfs = '2026:385'").fetchone()
    assert row == ("2005:716",)


def register_page(sfs: str, in_force: str) -> bytes:
    return (
        '<div>Utlänningslag <span class="bold">Ikraft:</span> 2006-03-31</div>'
        f'<div class="result-inner-sub-box-header">Ändring, SFS {sfs}</div>'
        f'<div class="result-inner-sub-box"><span class="bold">Ikraft:</span> {in_force}</div>'
    ).encode()


@pytest.mark.db
def test_a_broken_feed_does_not_stop_the_dating_and_dates_are_logged_now(
    statute, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from permitnav.cli import _ingest_register
    from permitnav.sources.raw import save_raw

    conn = statute
    monkeypatch.setenv("RAW_DIR", str(tmp_path))
    long_ago = datetime(2026, 1, 1, tzinfo=UTC)  # an unchanged page keeps its first fetch time
    save_raw(
        tmp_path / "sfsr" / "sfs-2005-716",
        "sfsr",
        "https://rkrattsbaser.gov.se/sfsr?bet=2005:716",
        register_page("2026:1142", "2026-07-01"),
        200,
        "html",
        long_ago,
    )
    broken = FEED.replace("tis, 17 nov 2026", "Fri, 01 May 2026").encode()
    save_raw(tmp_path / "sfs-feed", "sfs-feed", "feed", broken, 200, "xml", long_ago)
    started = datetime.now(UTC)

    assert _ingest_register(conn, ["2005:716"]) == 1  # the feed failure is reported
    assert undated(conn, "2026:1142") == 0  # …and the dating still ran
    row = conn.execute("SELECT min(observed_at) FROM change_log WHERE kind = 'dates'").fetchone()
    assert row is not None and row[0] >= started  # counted by this run's refresh summary
