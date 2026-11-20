"""Migrationsverket pages: parsing, robots rules, discovery and storing."""

from __future__ import annotations

import gzip
from datetime import UTC, date, datetime
from pathlib import Path

import psycopg
import pytest

from permitnav.parsing.webpage import page_path, parse_page
from permitnav.sources.http import FixtureHttp
from permitnav.sources.migrationsverket import (
    SITEMAP_INDEX,
    Robots,
    discover,
    fetch_page,
    in_scope,
    saved_pages,
)

FIXTURE = (Path(__file__).parent / "fixtures" / "migrationsverket_page.html").read_text(
    encoding="utf-8"
)
SV = "https://www.migrationsverket.se/du-vill-ansoka/arbeta/anstalld-eller-egen-foretagare/anstalld.html"
EN = "https://www.migrationsverket.se/en/you-want-to-apply/work/employee-or-self-employed/employees.html"
BASE = "web:sv/du-vill-ansoka/arbeta/anstalld-eller-egen-foretagare/anstalld"


@pytest.fixture(scope="module")
def page():
    return parse_page(FIXTURE, SV)


def by_id(page, unit_id):
    (unit,) = [u for u in page.units if u.unit_id == unit_id]
    return unit


def test_page_language_title_and_translations(page) -> None:
    assert (page.language, page.title) == ("sv", "Ansök om arbetstillstånd i Sverige")
    assert page.translations == {"en": EN, "sv": SV}


def test_one_unit_per_section_with_the_heading_id_as_anchor(page) -> None:
    ids = [u.unit_id for u in page.units]
    assert ids == [
        f"{BASE}#intro",
        f"{BASE}#kravforattfaarbetstillstand",
        f"{BASE}#fackforbundetfarmojlighetattyttrasig",
        f"{BASE}#vadkostardetattansoka",
        f"{BASE}#vadkostardetattansoka-2",  # the same heading repeated by a tab copy
    ]
    unit = by_id(page, f"{BASE}#kravforattfaarbetstillstand")
    assert unit.heading == "Ansök om arbetstillstånd i Sverige › Krav för att få arbetstillstånd"
    assert (unit.source_type, unit.authority, unit.time_basis) == (
        "web_page",
        "web_page",
        "observed",
    )
    assert unit.url == f"{SV}#h-Kravforattfaarbetstillstand"  # the page's own element id
    assert unit.document == SV


def test_section_text_keeps_lists_and_subheadings_without_scripts_or_navigation(page) -> None:
    text = by_id(page, f"{BASE}#kravforattfaarbetstillstand").text
    assert "- Din arbetsgivare ska ha tecknat försäkringar för dig" in text
    assert "trygghetsförsäkring och tjänstepensionsförsäkring" in text
    assert "## Hur hög lön måste jag ha för att uppfylla lönekravet?" in text
    assert "34 470 kronor" in text
    assert text.count("Din arbetsgivare ska ha tecknat") == 1  # nested blocks not repeated
    every = " ".join(u.text for u in page.units)
    assert "not content" not in every and "Meny som inte" not in every and "tracking" not in every


def test_repeated_lines_empty_sections_and_news_are_dropped(page) -> None:
    union = by_id(page, f"{BASE}#fackforbundetfarmojlighetattyttrasig").text
    assert union.count("Arbetsgivaren skickar erbjudandet") == 1
    assert not any(u.unit_id.endswith(("#tom", "#nyheter")) for u in page.units)


def test_english_paths_and_titles() -> None:
    assert page_path(EN) == ("en", "you-want-to-apply/work/employee-or-self-employed/employees")
    html = FIXTURE.replace("– Migrationsverket", "– Swedish Migration Agency")
    english = parse_page(html, EN)
    assert english.language == "en"
    assert english.title == "Ansök om arbetstillstånd i Sverige"
    assert english.units[0].unit_id.startswith("web:en/you-want-to-apply/work/")


def test_a_page_without_main_content_is_an_error() -> None:
    with pytest.raises(ValueError, match="no main content"):
        parse_page("<html><title>x</title><body><p>x</p></body></html>", SV)


# --- robots.txt, scope and discovery ----------------------------------------------------------

ROBOTS = """User-agent: *
Disallow: /*.html.printable
Disallow: /*?sv.state=*

User-agent: Yandex
Disallow: /changepassword

User-agent: *
Disallow: /*/flikar/
Disallow: /e-tjanster/
Allow: /e-tjanster/oppen.html
"""


def test_robots_rules_with_wildcards() -> None:
    robots = Robots(ROBOTS)
    site = "https://www.migrationsverket.se"
    assert robots.allowed(SV)
    assert not robots.allowed(f"{site}/du-vill-ansoka/arbeta/anstalld/flikar/ansok.html")
    assert not robots.allowed(f"{site}/sida.html.printable")
    assert not robots.allowed(f"{site}/sida.html?sv.state=1")
    assert not robots.allowed(f"{site}/e-tjanster/ansok.html")
    assert robots.allowed(f"{site}/e-tjanster/oppen.html")  # the longer Allow wins
    assert robots.allowed(f"{site}/changepassword")  # that rule is for Yandex only


def test_scope_rules() -> None:
    site = "https://www.migrationsverket.se"
    assert in_scope(SV)
    assert in_scope(f"{site}/arbetsgivare/du-vill-anstalla.html")
    assert in_scope(f"{site}/du-vill-ansoka/permanent-uppehallstillstand.html")
    assert in_scope(f"{site}/kontakta-oss/vantetider.html")
    assert not in_scope(f"{site}/du-vill-ansoka/internationellt-skydd-asyl.html")
    assert not in_scope(f"{site}/du-vill-ansoka/studera.html")
    assert not in_scope(
        f"{site}/du-vill-ansoka/studera/permanent-uppehallstillstand-for-doktorander.html"
    )
    assert not in_scope(f"{site}/download/18.1/file.pdf")
    news = "arbetsgivare/nyhetsarkiv-for-arbetsgivare/nyheter/2025-06-17-ny-medianlon.html"
    assert not in_scope(f"{site}/{news}")  # dated news, not current guidance


def test_discover_reads_a_gzipped_sitemap_and_keeps_in_scope_pages() -> None:
    sitemap = (
        "<urlset><url><loc>" + SV + "</loc></url>"
        "<url><loc>https://www.migrationsverket.se/du-vill-ansoka/studera.html</loc></url></urlset>"
    )
    http = FixtureHttp(
        {
            SITEMAP_INDEX: b"<sitemapindex><sitemap><loc>https://www.migrationsverket.se/s1.xml.gz</loc></sitemap></sitemapindex>",
            "https://www.migrationsverket.se/s1.xml.gz": gzip.compress(sitemap.encode()),
        }
    )
    assert discover(http) == [SV]


def test_fetch_refuses_disallowed_pages_and_saves_allowed_ones(tmp_path: Path) -> None:
    robots = Robots(ROBOTS)
    tab = "https://www.migrationsverket.se/du-vill-ansoka/arbeta/anstalld/flikar/ansok.html"
    http = FixtureHttp({SV: FIXTURE.encode(), tab: b"<html></html>"})
    with pytest.raises(PermissionError):
        fetch_page(http, robots, tab, tmp_path)
    assert tab not in http.requested  # never even requested
    now = datetime(2026, 11, 22, 8, 0, tzinfo=UTC)
    fetch_page(http, robots, SV, tmp_path, now=now)
    (saved,) = saved_pages(tmp_path)
    assert (saved.url, saved.fetched_at) == (SV, now)


# --- storing -----------------------------------------------------------------------------------


def raw_page(html: str, day: int, url: str = SV):
    import hashlib

    from permitnav.sources.raw import RawFetch

    return RawFetch(
        source="migrationsverket",
        url=url,
        fetched_at=datetime(2026, 11, day, 8, 0, tzinfo=UTC),
        content=html.encode(),
        content_hash=hashlib.sha256(html.encode()).hexdigest(),
        http_status=200,
    )


@pytest.mark.db
def test_page_sections_are_stored_dated_by_observation(conn: psycopg.Connection) -> None:
    from permitnav.corpus.store import ingest_page, versions_on

    report = ingest_page(conn, raw_page(FIXTURE, 20), parse_page(FIXTURE, SV))
    assert report.new_versions == 5
    unit = f"{BASE}#kravforattfaarbetstillstand"
    assert versions_on(conn, unit, date(2026, 11, 19)) == []  # not seen yet
    (now,) = versions_on(conn, unit, date(2026, 11, 21))
    assert now["valid_from"] is None and now["dates_estimated"] is False
    translation = conn.execute(
        "SELECT translation FROM page_translation WHERE document = %s", (SV,)
    ).fetchone()
    assert translation == (EN,)


@pytest.mark.db
def test_a_changed_section_closes_by_observation_without_legal_dates(
    conn: psycopg.Connection,
) -> None:
    from permitnav.corpus.store import ingest_page, versions_on

    ingest_page(conn, raw_page(FIXTURE, 20), parse_page(FIXTURE, SV))
    changed = FIXTURE.replace("34&nbsp;470 kronor", "35&nbsp;000 kronor")
    report = ingest_page(conn, raw_page(changed, 22), parse_page(changed, SV))
    assert (report.new_versions, report.closed_versions, report.estimated) == (1, 1, [])
    unit = f"{BASE}#kravforattfaarbetstillstand"
    (before,) = versions_on(conn, unit, date(2026, 11, 21))
    (after,) = versions_on(conn, unit, date(2026, 11, 23))
    assert "34 470" in before["text"] and "35 000" in after["text"]
    assert before["valid_to"] is None and after["valid_from"] is None


# --- regressions from the slice 5 code review (2026-11-22) -------------------------------------


def _page(body: str) -> str:
    head = "<html><head><title>T – Migrationsverket</title></head>"
    return f"{head}<body><div class='mv-main-content'>{body}</div></body></html>"


def test_text_outside_paragraphs_and_before_nested_lists_is_kept() -> None:
    html = _page(
        "<h2 id='h-A'>A</h2>"
        "<div class='sv-text-portlet'>Lönen måste vara minst 80 procent av medianlönen.</div>"
        "<ul><li>Du ska ha ett giltigt pass och<ul><li>en anställning</li></ul></li></ul>"
        "<blockquote>Citat från lagen.</blockquote>"
    )
    (unit,) = parse_page(html, SV).units
    assert "Lönen måste vara minst 80 procent av medianlönen." in unit.text
    assert "- Du ska ha ett giltigt pass och" in unit.text
    assert "- en anställning" in unit.text
    assert "Citat från lagen." in unit.text
    assert unit.text.count("en anställning") == 1


def test_generated_anchors_never_collide_with_real_ones() -> None:
    html = _page(
        "<h2 id='h-avgift'>Avgift</h2><p>a</p><h2 id='h-avgift'>Avgift</h2><p>b</p>"
        "<h2 id='h-avgift-2'>Avgift 2</h2><p>c</p>"
    )
    ids = [u.unit_id.split("#")[1] for u in parse_page(html, SV).units]
    assert len(ids) == len(set(ids)) == 3


def test_robots_txt_failures_stop_the_crawl_and_a_missing_file_allows_all() -> None:
    from permitnav.sources.http import FixtureHttp, HttpResponse
    from permitnav.sources.migrationsverket import ROBOTS, load_robots

    class Down(FixtureHttp):
        def get(self, url: str) -> HttpResponse:
            return HttpResponse(status=503, content=b"<html>maintenance</html>")

    with pytest.raises(RuntimeError, match="not crawling without its rules"):
        load_robots(Down({}))
    assert load_robots(FixtureHttp({})).allowed(SV)  # 404: no rules
    tab = SV.replace(".html", "/flikar/ansok.html")
    assert not load_robots(FixtureHttp({ROBOTS: ROBOTS_TXT.encode()})).allowed(tab)


ROBOTS_TXT = "User-agent: *\nDisallow: /*/flikar/\n"


def test_the_crawl_manifest_limits_which_saved_pages_are_ingested(tmp_path: Path) -> None:
    from permitnav.sources.migrationsverket import finish_crawl, load_crawl

    robots = Robots("")
    other = SV.replace("/anstalld.html", "/artist.html")
    http = FixtureHttp({SV: FIXTURE.encode(), other: FIXTURE.encode()})
    fetch_page(http, robots, SV, tmp_path)
    fetch_page(http, robots, other, tmp_path)
    assert {p.url for p in saved_pages(tmp_path)} == {SV, other}  # no manifest yet
    finish_crawl(tmp_path, fetched=[SV], failed=[])  # the artist page disappeared
    assert [p.url for p in saved_pages(tmp_path)] == [SV]
    crawl = load_crawl(tmp_path)
    assert crawl is not None and crawl.fetched == [SV]


@pytest.mark.db
def test_sections_of_pages_that_disappeared_are_closed(conn: psycopg.Connection) -> None:
    from permitnav.corpus.store import close_documents, ingest_page, versions_on

    ingest_page(conn, raw_page(FIXTURE, 20), parse_page(FIXTURE, SV))
    closed = close_documents(conn, keep=set(), at=datetime(2026, 11, 22, 8, 0, tzinfo=UTC))
    assert closed == 5
    unit = f"{BASE}#kravforattfaarbetstillstand"
    assert versions_on(conn, unit, date(2026, 11, 21))
    assert versions_on(conn, unit, date(2026, 11, 23)) == []


@pytest.mark.db
def test_a_page_is_never_recorded_as_its_own_translation(conn: psycopg.Connection) -> None:
    from permitnav.corpus.store import ingest_page

    other_form = FIXTURE.replace(f'hreflang="sv" href="{SV}"', f'hreflang="sv" href="{SV}?x=1"')
    ingest_page(conn, raw_page(other_form, 20), parse_page(other_form, SV))
    rows = conn.execute(
        "SELECT language, translation FROM page_translation WHERE document = %s", (SV,)
    ).fetchall()
    assert rows == [("sv", EN)]
