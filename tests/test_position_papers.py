"""Migrationsverket's legal position papers (phase 4, slice 5): the saved Lifos result pages,
the review file, the PDF parser and versioned storage."""

from __future__ import annotations

from datetime import UTC, date, datetime

import psycopg
import pytest

from permitnav.parsing.lifos import LifosHit, classify, curate, merge_review, parse_results
from permitnav.parsing.position_paper import parse_paper, section_start, units

# --- saved search results ---------------------------------------------------------------


def hit_html(doc_id: int, title: str, day: str) -> str:
    return (
        '<span class="hitsResults hitsResultHeader" id="h"><a \n\t\t   title="'
        + title
        + f'" href="https://lifos.migrationsverket.se/dokument?documentSummaryId={doc_id}"'
        ' tabindex="1">x</a></span><div id="hitsMetaData"><span>K&auml;lla: Migrationsverket'
        f"</span><span>Upphovsdatum: {day}</span><span>Dokumentnr: {doc_id}</span></div>"
    )


RESULTS = "".join(
    [
        hit_html(50159, "Rättsligt ställningstagande. Särskilda krav för att beviljas permanent"
                 " uppehållstillstånd - RS/084/2021 (version 8.0)", "2026-07-12"),
        hit_html(47120, "Rättsligt ställningstagande. Särskilda krav för att beviljas permanent"
                 " uppehållstillstånd - RS/084/2021 (Version 6.0)", "2023-12-22"),
        hit_html(45348, "Rättsligt ställningstagande. Brottsutvisade bevispersoner m.m."
                 " - RS/025/2021", "2021-03-12"),
        hit_html(50346, "Rättsligt ställningstagande. Sökningar i misstanke- och"
                 " belastningsregister (version 3.0)", "2026-10-01"),
    ]
)  # fmt: skip


def test_saved_result_pages_give_each_documents_rs_number_version_and_date() -> None:
    hits = parse_results(RESULTS)
    assert [(h.document_id, h.rs, h.version, h.date) for h in hits] == [
        (50159, "RS/084/2021", "8.0", date(2026, 7, 12)),
        (47120, "RS/084/2021", "6.0", date(2023, 12, 22)),
        (45348, "RS/025/2021", None, date(2021, 3, 12)),  # the first version has no suffix
        (50346, None, "3.0", date(2026, 10, 1)),  # no RS number in the title
    ]
    assert hits[0].url.endswith("documentSummaryId=50159")


def test_papers_are_deduplicated_by_rs_number_keeping_the_latest_version() -> None:
    papers = {p.key: p for p in curate(parse_results(RESULTS))}
    paper = papers["RS/084/2021"]
    assert paper.latest.version == "8.0"
    assert [h.version for h in paper.superseded] == ["6.0"]
    assert paper.suggestion == "include" and "permanent residence" in paper.topics
    assert papers["RS/025/2021"].suggestion == "exclude"  # expelled for crime
    # no RS number in the title: keyed by the title without its version
    assert "Rättsligt ställningstagande. Sökningar i misstanke- och belastningsregister" in papers


def test_relevance_sends_unclear_titles_to_a_person() -> None:
    assert classify("Förlängning av uppehållstillstånd på grund av anknytning")[1] == "include"
    assert classify("Dublinförordningens återtagandeförfarande")[1] == "exclude"
    assert classify("Uppehållstillstånd för besök")[1] == "review"  # neither list
    # both lists: a person decides
    assert classify("Permanent uppehållstillstånd enligt gymnasielagen")[1] == "review"


def test_the_review_file_keeps_decisions_and_follows_new_versions() -> None:
    papers = curate(parse_results(RESULTS))
    first = merge_review(papers, [])
    row = next(r for r in first if r["key"] == "RS/084/2021")
    assert row["decision"] == "include" and row["superseded"] == "47120"
    row["decision"] = "exclude"  # a person changed their mind
    newer = LifosHit(51000, row["title"].replace("8.0", "9.0"), "RS/084/2021", "9.0",
                     date(2026, 12, 1), "Migrationsverket")  # fmt: skip
    again = merge_review(curate([newer]), first)
    row = next(r for r in again if r["key"] == "RS/084/2021")
    assert (row["decision"], row["version"], row["document_id"]) == ("exclude", "9.0", "51000")
    assert "50159" in row["superseded"]
    assert any(r["key"] == "RS/025/2021" for r in again)  # papers not in new results stay


# --- the paper's PDF ----------------------------------------------------------------------

COVER = """ \nStyrD5 2020-06-02 \nFastställelsebeslut: RA/187/2021 Version 1.0
Gäller från och med: 2021-07-20
Datum för
revidering Version Avsnitt som reviderats Beslutsbeteckning
2026-07-12 8.0 2, 2.1 och 3 RA096/2026
2023-02-03 5.0 1 RA/006/2023
Fakta om rättsliga ställningstaganden
RS/084/2021
RÄTTSLIGT STÄLLNINGSTAGANDE"""
CONTENTS = """2
R Ä T T S L I G T  S T Ä L L N I N G S T A G A N D E  Migrationsverket
Innehållsförteckning
Fakta om rättsliga ställningstaganden ..................... 1
1. Bakgrund och syfte ......................................... 3
2. Allmänna förutsättningar för att beviljas permanent uppehållstillstånd .. 3
2.1 Försörjning ................................................ 4
3. Överklagan ................................................. 5"""
BODY = """3
R Ä T T S L I G T  S T Ä L L N I N G S T A G A N D E
I N S T R U K T I O N
Migrationsverket
1. Bakgrund och syfte
Uppehållstillstånd är ett tillstånd att vistas i Sverige.1 Syftet är att ge vägledning.
2. Allmänna förutsättningar för att
beviljas permanent uppehållstillstånd
Huvudregeln gäller enligt MIG 2018:5 alla utlänningar.2
2.1 Försörjning
Inkomsten ska vara beskattad, se (1999:1229).3
1 Prop. 2020/21:191 s. 62.
2 Se avsnitt 3.
3 5 kap. 3 g § utlänningslagen."""
BODY_2 = """4
Migrationsverket
3. Överklagan
Beslutet kan överklagas:4
4 14 kap. 3 § utlänningslagen."""


def paper_from(*pages: str):
    return parse_paper(list(pages), "RS/084/2021", "Särskilda krav", "8.0")


def test_a_papers_sections_come_without_furniture_contents_or_footnotes() -> None:
    paper = paper_from(COVER, CONTENTS, BODY, BODY_2)
    assert paper.warnings == []
    sections = {number: text for number, _, text in paper.sections}
    assert list(sections) == ["1", "2", "2.1", "3"]
    assert (
        sections["1"]
        == "Uppehållstillstånd är ett tillstånd att vistas i Sverige. Syftet är att ge vägledning."
    )
    assert sections["2"] == "Huvudregeln gäller enligt MIG 2018:5 alla utlänningar."  # 2018:5 kept
    assert sections["2.1"] == "Inkomsten ska vara beskattad, se (1999:1229)."  # mark after ")."
    assert sections["3"] == "Beslutet kan överklagas:"  # "4 14 kap. 3 § …" is a footnote
    assert not any("Prop." in t or "R Ä T T" in t for t in sections.values())


def test_each_section_starts_at_the_last_revision_of_it_or_a_section_above() -> None:
    paper = paper_from(COVER, CONTENTS, BODY, BODY_2)
    assert paper.in_force == date(2021, 7, 20)
    assert section_start(paper, "1") == date(2023, 2, 3)
    assert section_start(paper, "2.1") == date(2026, 7, 12)
    assert section_start(paper, "2.1.4") == date(2026, 7, 12)  # "2" revised: conservative
    assert section_start(paper, "4") == date(2021, 7, 20)  # never revised
    unit = units(paper, "https://lifos/x")[0]
    assert (unit.unit_id, unit.authority) == ("rs:RS/084/2021/1", "position_paper")


def test_a_stale_contents_list_loses_to_the_bodys_own_numbers() -> None:
    contents = CONTENTS.replace("2.1 Försörjning", "2.4 Försörjning")  # not updated
    paper = paper_from(COVER, contents, BODY, BODY_2)
    assert [n for n, _, _ in paper.sections] == ["1", "2", "2.1", "3"]


def test_unnumbered_contents_are_numbered_in_order() -> None:
    contents = "Innehållsförteckning\nSammanfattning ........ 3\nGällande rätt ....... 4"
    body = "3\nSammanfattning\nEn familjemedlem kan få tillstånd.\nGällande rätt\nSe bilaga."
    paper = paper_from(COVER, contents, body)
    assert [(n, t) for n, t, _ in paper.sections] == [
        ("1", "Sammanfattning"),
        ("2", "Gällande rätt"),
    ]


# --- stored, versioned ------------------------------------------------------------------


@pytest.mark.db
def test_a_new_version_replaces_only_the_sections_it_revised(conn: psycopg.Connection) -> None:
    from permitnav.corpus.store import ingest_position_paper
    from permitnav.sources.raw import RawFetch

    def raw(doc: int, day: int) -> RawFetch:
        return RawFetch("lifos", f"https://lifos/dokument?documentAttachmentId={doc}",
                        datetime(2026, 11, day, tzinfo=UTC), b"%PDF", f"h{doc}", 200)  # fmt: skip

    old = paper_from(COVER, CONTENTS, BODY, BODY_2)
    ingest_position_paper(conn, raw(1, 20), units(old, "u1"), "rs:RS/084/2021")
    revised_body = BODY_2.replace("Beslutet kan överklagas:4", "Beslutet kan inte överklagas.4")
    revised_cover = COVER.replace(
        "2026-07-12 8.0 2, 2.1 och 3", "2026-12-01 9.0 3 RA/1/2026\n2026-07-12 8.0 2, 2.1 och 3"
    )
    new = parse_paper(
        [revised_cover, CONTENTS, BODY, revised_body], "RS/084/2021", "Särskilda krav", "9.0"
    )
    report = ingest_position_paper(conn, raw(2, 25), units(new, "u2"), "rs:RS/084/2021")
    assert report.new_versions == 1 and report.unchanged_versions == 3
    rows = conn.execute(
        "SELECT valid_from, valid_to, text FROM unit_version WHERE unit_id = 'rs:RS/084/2021/3'"
        " AND NOT withdrawn ORDER BY valid_from"
    ).fetchall()
    assert rows == [
        (date(2026, 7, 12), date(2026, 12, 1), "Beslutet kan överklagas:"),
        (date(2026, 12, 1), None, "Beslutet kan inte överklagas."),
    ]


# --- review fixes ------------------------------------------------------------------------


def test_the_refresh_fetches_every_source_the_ingest_reads() -> None:
    from permitnav.cli import SOURCES, refresh_steps

    fetched = {args.source for name, _, args in refresh_steps() if name.startswith("fetch")}
    assert fetched == set(SOURCES) and "lifos" in fetched


def test_older_saved_results_never_move_a_row_back_to_an_older_version() -> None:
    papers = curate(parse_results(RESULTS))
    rows = merge_review(papers, [])
    row = next(r for r in rows if r["key"] == "RS/084/2021")
    row.update(version="10.0", document_id="50400")  # the file already names a newer version
    again = next(r for r in merge_review(papers, rows) if r["key"] == "RS/084/2021")
    assert (again["version"], again["document_id"]) == ("10.0", "50400")
    assert "50159" in again["superseded"]


class FakeHttp:
    def __init__(self, pages: dict[str, bytes]) -> None:
        self.pages = pages

    def get(self, url: str):
        from permitnav.sources.http import HttpResponse

        return HttpResponse(200, self.pages[url]) if url in self.pages else HttpResponse(404, b"")


DOC = "https://lifos.migrationsverket.se/dokument?documentSummaryId={}"


def doc_page(title: str, attachment: str = "") -> bytes:
    return f"<h1>Rättsligt ställningstagande. {title}</h1> Huvuddokument: {attachment}".encode()


def test_a_feed_announcement_leads_to_the_document_that_names_its_version() -> None:
    from permitnav.sources.lifos import document_id_of

    news = "https://lifos.migrationsverket.se/rattsavd-nyhetsarkiv/x.html"
    http = FakeHttp({
        news: b'<a href="/dokument?documentSummaryId=111">other</a>'
              b'<a href="/dokument?documentSummaryId=222">this</a>',
        DOC.format(111): doc_page("Något annat - RS/001/2020 (version 2.0)"),
        DOC.format(222): doc_page("Särskilda krav - RS/084/2021 (version 9.0)"),
    })  # fmt: skip
    assert document_id_of(http, news, "RS/084/2021", "9.0") == 222
    with pytest.raises(RuntimeError, match="no linked document"):
        document_id_of(http, news, "RS/084/2021", "10.0")


def test_feed_text_is_unescaped_and_a_page_without_its_pdf_is_an_error(tmp_path) -> None:
    from permitnav.sources.lifos import _xml_text, fetch_paper

    assert _xml_text("https://x/y?a=1&amp;b=2") == "https://x/y?a=1&b=2"
    assert _xml_text("<![CDATA[Titel & mer]]>") == "Titel & mer"
    http = FakeHttp({DOC.format(5): b"documentAttachmentId is not here as a link"})
    with pytest.raises(RuntimeError, match="not a Lifos document page|no main document"):
        fetch_paper(http, tmp_path, 5)


def test_answers_citing_a_paper_credit_migrationsverket() -> None:
    from permitnav.api import CREDITS

    assert CREDITS["position_paper"] == "Källa: Migrationsverket"


def _raw(doc: int, day: int):
    from permitnav.sources.raw import RawFetch

    return RawFetch("lifos", f"https://lifos/dokument?documentAttachmentId={doc}",
                    datetime(2026, 11, day, tzinfo=UTC), b"%PDF", f"h{doc}", 200)  # fmt: skip


def _section(conn: psycopg.Connection, number: str) -> list[tuple]:
    return conn.execute(
        "SELECT valid_from, valid_to, text FROM unit_version WHERE unit_id = %s"
        " AND NOT withdrawn ORDER BY valid_from",
        (f"rs:RS/084/2021/{number}",),
    ).fetchall()


@pytest.mark.db
def test_a_new_version_keeps_unchanged_sections_history_and_dates_its_changes(
    conn: psycopg.Connection,
) -> None:
    """v9 revises "2" (so 2.1 is dated 2026-12-01 by the table, unchanged though), changes
    section 1 without naming it, and drops section 3."""
    from permitnav.corpus.store import ingest_position_paper

    old = paper_from(COVER, CONTENTS, BODY, BODY_2)
    ingest_position_paper(conn, _raw(1, 20), units(old, "u1"), "rs:RS/084/2021", date(2026, 7, 12))
    cover = COVER.replace("2026-07-12 8.0", "2026-12-01 9.0 2 RA/1/2026\n2026-07-12 8.0")
    body = BODY.replace("Syftet är att ge vägledning.", "Syftet är att ge ny vägledning.")
    contents = CONTENTS.replace("3. Överklagan ......", "x")
    new = parse_paper([cover, contents, body], "RS/084/2021", "Särskilda krav", "9.0")
    ingest_position_paper(conn, _raw(2, 25), units(new, "u2"), "rs:RS/084/2021", date(2026, 12, 1))
    assert [r[:2] for r in _section(conn, "2.1")] == [(date(2026, 7, 12), None)]  # history kept
    assert [r[:2] for r in _section(conn, "1")] == [
        (date(2023, 2, 3), date(2026, 12, 1)),  # the old wording ends when v9 starts
        (date(2026, 12, 1), None),  # the changed one starts then, not backdated to 2023
    ]
    assert [r[:2] for r in _section(conn, "3")] == [(date(2026, 7, 12), date(2026, 12, 1))]


@pytest.mark.db
def test_papers_taken_out_of_scope_leave_the_corpus(conn: psycopg.Connection) -> None:
    from permitnav.corpus.store import ingest_position_paper, withdraw_papers_out_of_scope

    paper = paper_from(COVER, CONTENTS, BODY, BODY_2)
    ingest_position_paper(conn, _raw(1, 20), units(paper, "u1"), "rs:RS/084/2021")
    assert withdraw_papers_out_of_scope(conn, {"rs:RS/084/2021"}, datetime.now(UTC)) == 0
    assert withdraw_papers_out_of_scope(conn, set(), datetime.now(UTC)) == 4
    assert _section(conn, "1") == []
