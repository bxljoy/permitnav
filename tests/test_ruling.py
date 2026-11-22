"""Migration Court of Appeal rulings: parsing, fetching, storing and their place in results."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import psycopg
import pytest

from permitnav.parsing.ruling import cited_sections, html_paragraphs, in_scope, parse_rulings
from permitnav.sources.domstol import API, fetch_rulings, latest_rulings
from permitnav.sources.http import FixtureHttp


def record(
    number: str,
    decided: str,
    lagrum: list[tuple[str, str]],
    text: str | None = "<p>Första stycket.</p>\n<p>Andra &amp; sista stycket.</p>",
) -> dict:
    return {
        "id": f"id-{number}",
        "referatNummerLista": [number],
        "avgorandedatum": decided,
        "sammanfattning": f"Sammanfattning av {number}.",
        "lagrumLista": [{"referens": ref, "sfsNummer": sfs} for ref, sfs in lagrum],
        **({"innehall": text} if text is not None else {}),
    }


WORK = record(
    "MIG 2017:24", "2017-12-13", [("6 kap. 1 § och 2 § första stycket utlänningslagen", "2005:716")]
)
ASYLUM = record(
    "MIG 2018:1",
    "2018-01-10",
    [("4 kap. 1 § utlänningslagen", "2005:716"), ("6 kap. 2 § utlänningslagen", "2005:716")],
)
STUDIES = record("MIG 2015:25", "2015-12-22", [("5 kap. 10 § utlänningslagen", "2005:716")])
OLD = record("MIG 2008:3", "2008-02-01", [("6 kap. 2 § utlänningslagen", "2005:716")])
NO_TEXT = record(
    "MIG 2024:13", "2024-10-30", [("5 kap. 17 § utlänningslagen", "2005:716")], text=None
)
NO_NUMBER = {**WORK, "referatNummerLista": []}


def test_only_rulings_in_scope_and_after_the_2009_reform_are_kept() -> None:
    content = json.dumps([WORK, ASYLUM, STUDIES, OLD, NO_TEXT, NO_NUMBER]).encode()
    parsed = parse_rulings(content)
    assert [u.unit_id for u in parsed.units] == ["mig:2017:24", "mig:2024:13"]
    assert (parsed.out_of_scope, parsed.too_old, parsed.no_reference) == (2, 1, 1)
    assert parsed.no_text == ["MIG 2024:13"]


def test_a_ruling_is_one_dated_unit_that_cites_its_sections() -> None:
    unit = parse_rulings(json.dumps([WORK]).encode()).units[0]
    assert (unit.source_type, unit.authority, unit.language) == ("ruling", "ruling", "sv")
    assert unit.valid_from == date(2017, 12, 13) and unit.valid_to is None
    assert unit.heading == "MIG 2017:24 (Migrationsöverdomstolen, avgjort 2017-12-13)"
    assert unit.url == "https://rattspraxis.etjanst.domstol.se/sok/detaljsida/id-MIG 2017:24"
    assert unit.links == (("cites", "sfs:2005:716/6kap/1§"), ("cites", "sfs:2005:716/6kap/2§"))
    assert unit.text.split("\n\n") == [
        "Sammanfattning: Sammanfattning av MIG 2017:24.",
        "Lagrum: 6 kap. 1 § och 2 § första stycket utlänningslagen",
        "Första stycket.",
        "Andra & sista stycket.",
    ]


def test_a_ruling_without_full_text_keeps_its_summary_and_says_so() -> None:
    unit = parse_rulings(json.dumps([NO_TEXT]).encode()).units[0]
    assert "Sammanfattning av MIG 2024:13." in unit.text
    assert unit.text.endswith("(Domskälen finns inte i källan; endast sammanfattningen.)")


def test_cited_sections_read_each_entry_inside_its_own_statute() -> None:
    lagrum = [
        {
            "referens": "5 kap. 5 § första stycket 1 och 6 kap. 1 § och 2 § "
            "utlänningslagen (2005:716)",
            "sfsNummer": "2005:716",
        },
        {"referens": "4 kap. 4 e § utlänningsförordningen", "sfsNummer": "2006:97"},
        {"referens": "Artikel 9 i rådets direktiv 2003/86/EG", "sfsNummer": None},
    ]
    assert cited_sections(lagrum) == [
        "sfs:2005:716/5kap/5§",
        "sfs:2005:716/6kap/1§",
        "sfs:2005:716/6kap/2§",
        "sfs:2006:97/4kap/4e§",
    ]


def test_scope_excludes_protection_and_studies_and_needs_an_in_scope_section() -> None:
    assert in_scope(["sfs:2005:716/5kap/3§"])  # family ties
    assert in_scope(["sfs:2005:716/5akap/1§"])  # long-term residents
    assert in_scope(["sfs:2005:716/5kap/10§", "sfs:2005:716/6kap/2§"])  # work-based permit
    assert not in_scope(["sfs:2005:716/5kap/10§"])  # temporary permits alone: mostly studies
    assert not in_scope(["sfs:2005:716/6kap/2§", "sfs:2005:716/5kap/15a§"])  # spårbyte
    assert not in_scope(["sfs:2005:716/12kap/1§"])
    assert not in_scope([])


def test_html_paragraphs_become_plain_text() -> None:
    assert html_paragraphs("<p>Ett <i>stycke</i>.</p><p> </p><p>Två<br/>rader</p>") == [
        "Ett stycke.",
        "Två",
        "rader",
    ]
    # Headings are their own blocks; markup inside a word does not split it.
    assert html_paragraphs("<h3>Bakgrund</h3><p><b>M</b>igrationsverket beslöt.</p>") == [
        "Bakgrund",
        "Migrationsverket beslöt.",
    ]


def test_a_republished_ruling_number_keeps_the_latest_publication() -> None:
    old = {**WORK, "id": "old", "publiceringstid": "2017-12-14T12:00:00"}
    new = {**WORK, "id": "new", "publiceringstid": "2018-01-10T12:00:00"}
    parsed = parse_rulings(json.dumps([old, new, new]).encode())
    assert [u.url.rsplit("/", 1)[1] for u in parsed.units] == ["new"]
    assert parsed.duplicates == 2


def page_url(page: int) -> str:
    return f"{API}?domstolkod=MIOD&page={page}&pagesize=100"


def test_fetch_pages_until_a_short_page_and_saves_one_download(tmp_path: Path) -> None:
    first = [record(f"MIG 2020:{n}", "2020-01-01", []) for n in range(100)]
    http = FixtureHttp(
        {page_url(0): json.dumps(first).encode(), page_url(1): json.dumps([WORK]).encode()}
    )
    raw = fetch_rulings(http, tmp_path)
    assert http.requested == [page_url(0), page_url(1)]
    assert len(json.loads(raw.content)) == 101
    saved = latest_rulings(tmp_path)
    assert saved is not None and saved.content == raw.content
    assert saved.url == f"{API}?domstolkod=MIOD"


def test_a_failed_page_saves_nothing(tmp_path: Path) -> None:
    first = [record(f"MIG 2020:{n}", "2020-01-01", []) for n in range(100)]
    http = FixtureHttp({page_url(0): json.dumps(first).encode()})  # page 1 is a 404
    with pytest.raises(RuntimeError, match="HTTP 404"):
        fetch_rulings(http, tmp_path)
    assert latest_rulings(tmp_path) is None


@pytest.mark.db
def test_a_stored_ruling_links_to_the_wording_that_applied_on_its_decision_date(
    conn: psycopg.Connection,
) -> None:
    from permitnav.corpus.store import ingest_rulings
    from permitnav.sources.raw import RawFetch
    from tests.test_ingest import FIXTURE, ingest

    ingest(conn, FIXTURE, 20)  # 6 kap. 2 § applies from 2026-06-01
    later = record("MIG 2026:20", "2026-07-01", [("6 kap. 2 § utlänningslagen", "2005:716")])
    earlier = record("MIG 2020:1", "2020-03-01", [("6 kap. 2 § utlänningslagen", "2005:716")])
    content = json.dumps([later, earlier]).encode()
    fetched = datetime(2026, 11, 22, tzinfo=UTC)
    raw = RawFetch("domstol", f"{API}?domstolkod=MIOD", fetched, content, "h", 200)
    report = ingest_rulings(conn, raw, parse_rulings(content))
    assert report.units == 2 and report.new_versions == 2
    linked = conn.execute(
        """SELECT l.from_unit_id, v.unit_id, v.valid_from FROM unit_link l
           JOIN unit_version v ON v.id = l.to_version_id WHERE l.kind = 'cites'"""
    ).fetchall()
    # The 2020 ruling read a wording we do not have (history before 2026-06-01 is not stored).
    assert linked == [("mig:2026:20", "sfs:2005:716/6kap/2§", date(2026, 6, 1))]
    again = ingest_rulings(conn, raw, parse_rulings(content))
    assert again.already_recorded and again.links == 1  # relinked, not duplicated
    assert conn.execute("SELECT count(*) FROM unit_link WHERE kind = 'cites'").fetchone() == (1,)


@pytest.mark.db
def test_a_corrected_ruling_text_replaces_the_old_one_without_overlap(
    conn: psycopg.Connection,
) -> None:
    from permitnav.corpus.store import ingest_rulings
    from permitnav.sources.raw import RawFetch

    def raw(content: bytes, day: int) -> RawFetch:
        at = datetime(2026, 11, day, tzinfo=UTC)
        return RawFetch("domstol", f"{API}?domstolkod=MIOD", at, content, f"h{day}", 200)

    summary_only = json.dumps([NO_TEXT]).encode()
    full = json.dumps([{**NO_TEXT, "innehall": "<p>Domskälen.</p>"}]).encode()
    ingest_rulings(conn, raw(summary_only, 20), parse_rulings(summary_only))
    report = ingest_rulings(conn, raw(full, 22), parse_rulings(full))
    assert report.withdrawn_versions == 1 and report.new_versions == 1
    rows = conn.execute(
        "SELECT withdrawn, valid_from, observed_to IS NULL FROM unit_version"
        " WHERE unit_id = 'mig:2024:13' ORDER BY id"
    ).fetchall()
    assert rows == [(True, date(2024, 10, 30), False), (False, date(2024, 10, 30), True)]
