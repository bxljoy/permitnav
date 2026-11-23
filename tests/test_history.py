"""Older wordings from the amending acts (phase 4, slice 3): the act parser, the register
scope, and the walk back through "Senaste lydelse"."""

from __future__ import annotations

from datetime import UTC, date, datetime

import psycopg
import pytest

from permitnav.corpus.history import Statute, needed_acts, rebuild_history
from permitnav.parsing.sfs_act import ActSection, ParsedAct, mend_split_words, parse_act
from permitnav.parsing.sfsr import parse_scope, scope_names

# Two pages laid out like svenskforfattningssamling.se and the printed SFS, with the quirks
# met in the real acts: page furniture, a header glued to a line, footnote numbers glued to
# "§" or set apart, a body line starting with the next footnote's number, a wrapped heading
# with a footnote mark, a word broken at the line end, a footnote mark inside the text.
PAGE_1 = """1
1 2 3 4 5 6 7 8 9 0 : ;
Svensk författningssamling
Lag
om ändring i utlänningslagen (2005:716)
Utfärdad den 5 december 2024
Enligt riksdagens beslut1 föreskrivs i fråga om utlänningslagen (2005:716)
dels att 6 a kap. 1 och 8 §§ ska ha följande lydelse.
6 a kap.
1 §2    En utlänning som har ett anställningsavtal ska beviljas ett EU-blåkort om
1. lönen är minst 1,25 gånger den genomsnittliga bruttoårslönen, och
2. anställningen är högkvali-
ficerad.
4 Det som sägs i första stycket gäller även den som följer av detta3.
Byte av
anställning4
1 Prop. 2024/25:18, bet. 2024/25:SfU6, rskr. 2024/25:65.
2 Senaste lydelse 2013:606.
3 Jfr direktiv (EU) 2021/1883.
SFS 2024:1220
Publicerad
den 10 december 2024"""

PAGE_2 = """2
SFS 2024:12208 § 5     Den som har ett EU-blåkort ska anmäla byte av anställning.
Denna lag träder i kraft den 1 januari 2025.
På regeringens vägnar
4 Rubriken har fått sin lydelse genom 2013:606.
5 Senaste lydelse av tidigare 9 § 2013:606."""


def test_an_act_gives_each_sections_new_wording_and_the_act_before() -> None:
    act = parse_act([PAGE_1, PAGE_2], "2024:1220")
    sections = {(s.chapter, s.section): s for s in act.sections}
    assert set(sections) == {("6 a", "1"), ("6 a", "8")}
    first = sections["6 a", "1"]
    assert first.previous == "2013:606" and not first.renumbered
    assert first.text.split("\n\n")[:3] == [
        "En utlänning som har ett anställningsavtal ska beviljas ett EU-blåkort om",
        "1. lönen är minst 1,25 gånger den genomsnittliga bruttoårslönen, och",
        "2. anställningen är högkvalificerad.",
    ]
    assert first.text.endswith("gäller även den som följer av detta.")  # mark dropped
    assert "Byte av" not in first.text and "Prop." not in first.text  # heading, footnotes
    moved = sections["6 a", "8"]  # header glued on, footnote number set apart
    assert moved.text == "Den som har ett EU-blåkort ska anmäla byte av anställning."
    assert (moved.previous, moved.old_number, moved.renumbered) == ("2013:606", "9", True)


def test_an_act_changing_one_chapter_may_print_no_chapter_heading() -> None:
    page = (
        "Regeringen föreskriver att 3 kap. 1 § utlänningsförordningen (2006:97) ska\n"
        "ha följande lydelse.\n1 §1    Utöver vad som föreskrivs är dessa undantagna.\n"
        "Denna förordning träder i kraft den 1 november 2023.\n"
        "1 Senaste lydelse 2021:1213."
    )
    (section,) = parse_act([page], "2023:588").sections
    assert (section.chapter, section.section, section.previous) == ("3", "1", "2021:1213")


def test_footnotes_referenced_with_a_space_are_still_footnotes() -> None:
    page = (
        "Enligt riksdagens beslut 1 föreskrivs att 4 kap. 2 § ska ha följande lydelse.\n"
        "4 kap.\n2§2 Med alternativt skyddsbehövande avses en utlänning.\n"
        "Denna lag träder i kraft den 1 januari 2015.\n"
        "1 Prop. 2013/14:248, bet. 2014/15:SfU5.\n2 Senaste lydelse 2009:1542."
    )
    (section,) = parse_act([page], "2014:1400").sections
    assert section.text == "Med alternativt skyddsbehövande avses en utlänning."
    assert section.previous == "2009:1542"


def test_a_missed_footnote_does_not_let_the_later_ones_leak_into_the_text() -> None:
    page = (
        "Enligt riksdagens beslut1 föreskrivs att 21 kap. 2 § ska ha följande lydelse.\n"
        "21 kap.\n2 §3   Uppehållstillstånd ges enligt 5 kap. 1 §.\n"
        "Denna lag träder i kraft den 1 januari 2010.\n"
        "1 Prop. 2008/09:156.\n3 Senaste lydelse 2006:219.\n4 Senaste lydelse 2009:16."
    )  # footnote 2 is not where it should be: 3 and 4 must still be read as footnotes
    (section,) = parse_act([page], "2009:1542").sections
    assert section.text == "Uppehållstillstånd ges enligt 5 kap. 1 §."
    assert section.previous == "2006:219"


def test_split_words_are_mended_only_into_words_the_act_uses() -> None:
    vocabulary = {"arbetstillstånd", "inresan", "en", "san", "och"}
    assert mend_split_words("om arbetstillstå nd före inre san", vocabulary) == (
        "om arbetstillstånd före inresan"
    )
    assert mend_split_words("en och", vocabulary) == "en och"  # both halves are words


def test_the_register_scope_names_sections_ranges_and_whole_chapters() -> None:
    items = parse_scope(
        "upph. 4 kap., 1 kap. 3 §, rubr. närmast före 1 kap. 3 §; ändr. 5 kap 16 a–18 §§;"
        " nya 9 a kap."
    )
    assert [(i.op, i.chapter, i.first, i.last) for i in items] == [
        ("upph", "4", None, None),
        ("upph", "1", "3", "3"),
        ("ändr", "5", "16 a", "18"),
        ("ny", "9 a", None, None),
    ]
    assert scope_names(items[2], "5", "17") and not scope_names(items[2], "5", "19")
    assert scope_names(items[0], "4", "3")  # a whole chapter names every section in it


# --- the walk back ----------------------------------------------------------------------

STATUTE = Statute("2005:716", date(2006, 3, 31), "Lag")
SEEN = datetime(2026, 11, 24, tzinfo=UTC)


def section(chapter: str, number: str, text: str, previous: str | None = None, **kw) -> ActSection:
    footnote = f"Senaste lydelse {previous}." if previous else kw.pop("footnote", "")
    return ActSection(chapter, number, text, footnote=footnote, previous=previous, **kw)


def act(sfs: str, *sections: ActSection) -> ParsedAct:
    return ParsedAct(sfs, list(sections))


ACTS = {
    "2005:716": act("2005:716", section("4", "3", "Lydelse 2006."), section("6", "2", "Ur 2006.")),
    "2026:295": act(
        "2026:295",
        section("6", "2", "Lydelse 2026.", "2024:100"),
        section("6", "2 c", "Helt ny paragraf."),
    ),
    "2024:100": act("2024:100", section("6", "2", "Lydelse 2024.", "2021:765")),
    "2021:765": act("2021:765", section("6", "2", "Lydelse 2021.", "2019:462")),
    "2026:1142": act("2026:1142", section("4", "3", "Nytt kapitel 2026.")),
    "2014:1400": act("2014:1400", section("4", "3", "Lydelse 2015.")),
    "2024:1220": act(
        "2024:1220",
        ActSection(
            "6 a",
            "8",
            "Flyttad 2025.",
            footnote="Senaste lydelse av tidigare 9 § 2013:606.",
            previous="2013:606",
            renumbered=True,
            old_number="9",
        ),
    ),  # fmt: skip
    "2013:606": act("2013:606", section("6 a", "9", "Lydelse 2013.")),
}
IN_FORCE = {
    "2026:295": date(2026, 6, 1),
    "2024:100": date(2024, 3, 1),
    "2021:765": date(2021, 7, 1),
    "2019:462": date(2019, 8, 1),
    "2026:1142": date(2026, 7, 12),
    "2014:1400": date(2015, 1, 1),
    "2024:1220": date(2025, 1, 1),
    "2013:606": date(2013, 8, 1),
}
SCOPES = {
    "2026:1142": parse_scope("upph. 4 kap.; nya 4 kap."),  # the chapter replaced: no footnotes
    "2014:1400": parse_scope("ändr. 4 kap. 1–3 §§"),
    "2026:295": parse_scope("ändr. 6 kap. 2 §; ny 6 kap. 2 c §"),
}


@pytest.fixture
def corpus(conn: psycopg.Connection) -> psycopg.Connection:
    """Today's wordings, as the consolidated-text ingest stores them."""
    text_download = _download(conn, "riksdagen", "https://data.riksdagen.se/x")
    for unit, act_sfs, start in (
        ("6kap/2§", "2026:295", date(2026, 6, 1)),
        ("6kap/2c§", "2026:295", date(2026, 6, 1)),
        ("4kap/3§", "2026:1142", date(2026, 7, 12)),
        ("6akap/8§", "2024:1220", date(2025, 1, 1)),
    ):
        unit_id = f"sfs:2005:716/{unit}"
        conn.execute(
            "INSERT INTO unit (id, source_type, authority, language, document, heading, url)"
            " VALUES (%s, 'statute', 'law', 'sv', 'sfs:2005:716', %s, 'u')",
            (unit_id, unit),
        )
        conn.execute(
            "INSERT INTO unit_version (unit_id, text, text_hash, time_basis, valid_from,"
            " amended_by, observed_from, download_id)"
            " VALUES (%s, 'idag', 'h', 'valid', %s, %s, %s, %s)",
            (unit_id, start, act_sfs, SEEN, text_download),
        )
    return conn


def _download(conn: psycopg.Connection, source: str, url: str) -> int:
    row = conn.execute(
        "INSERT INTO download (source, url, fetched_at, content_hash, storage_path, http_status)"
        " VALUES (%s, %s, now(), 'h', 'p', 200) RETURNING id",
        (source, url),
    ).fetchone()
    assert row is not None
    return row[0]


def rebuild(conn: psycopg.Connection, seen: datetime = SEEN):
    downloads = {sfs: _download(conn, "sfs-act", f"https://sfs/{sfs}") for sfs in ACTS}
    kinds = dict.fromkeys(IN_FORCE, "Lag")
    return rebuild_history(conn, STATUTE, ACTS, IN_FORCE, kinds, downloads, seen, SCOPES)


def wordings(conn: psycopg.Connection, unit: str) -> list[tuple]:
    return conn.execute(
        "SELECT valid_from, valid_to, amended_by, text FROM unit_version"
        " WHERE unit_id = %s AND text <> 'idag' AND NOT withdrawn ORDER BY valid_from",
        (f"sfs:2005:716/{unit}",),
    ).fetchall()


@pytest.mark.db
def test_the_walk_back_stores_each_earlier_wording_until_the_cutoff(corpus) -> None:
    report = rebuild(corpus)
    assert wordings(corpus, "6kap/2§") == [
        (date(2021, 7, 1), date(2024, 3, 1), "2021:765", "Lydelse 2021. Lag (2021:765)."),
        (date(2024, 3, 1), date(2026, 6, 1), "2024:100", "Lydelse 2024. Lag (2024:100)."),
    ]  # 2021:765 was in force on 2023-01-01: the walk stops there
    assert wordings(corpus, "6kap/2c§") == []  # new in 2026:295: nothing before
    assert report.stops["new section: nothing before"] == 1


@pytest.mark.db
def test_a_replaced_chapter_takes_the_wording_from_the_register_not_the_statute(corpus) -> None:
    rebuild(corpus)
    # no footnote in 2026:1142, but 2014:1400 changed 4 kap. 3 § after the statute was enacted
    assert wordings(corpus, "4kap/3§")[-1] == (
        date(2015, 1, 1), date(2026, 7, 12), "2014:1400", "Lydelse 2015. Lag (2014:1400).",
    )  # fmt: skip


@pytest.mark.db
def test_a_renumbered_section_keeps_its_earlier_wording_under_its_former_number(corpus) -> None:
    report = rebuild(corpus)
    assert wordings(corpus, "6akap/9§") == [
        (date(2013, 8, 1), date(2025, 1, 1), "2013:606", "Lydelse 2013. Lag (2013:606)."),
    ]
    assert report.units_created == 1
    assert wordings(corpus, "6akap/8§") == []


@pytest.mark.db
def test_a_second_run_changes_nothing(corpus) -> None:
    first = rebuild(corpus)
    second = rebuild(corpus)
    assert first.wordings > 0 and second.wordings == 0
    assert second.unchanged == first.wordings + first.unchanged


@pytest.mark.db
def test_a_wording_read_differently_later_replaces_the_rebuilt_one(corpus) -> None:
    rebuild(corpus)
    better = ACTS["2024:100"].sections[0]
    ACTS["2024:100"].sections[0] = section("6", "2", "Lydelse 2024, rättad.", "2021:765")
    try:
        report = rebuild(corpus, datetime(2026, 11, 25, tzinfo=UTC))  # a later run
    finally:
        ACTS["2024:100"].sections[0] = better
    assert report.replaced == 1
    assert wordings(corpus, "6kap/2§")[1][3] == "Lydelse 2024, rättad. Lag (2024:100)."
    withdrawn = corpus.execute(
        "SELECT count(*) FROM unit_version WHERE unit_id = 'sfs:2005:716/6kap/2§' AND withdrawn"
    ).fetchone()
    assert withdrawn == (1,)


@pytest.mark.db
def test_a_section_repealed_before_the_act_had_no_wording_in_between(conn) -> None:
    scopes = {"2026:1": parse_scope("ny 5 kap. 9 §"), "2024:5": parse_scope("upph. 5 kap. 9 §")}
    acts = {"2005:716": act("2005:716", section("5", "9", "Ur 2006.")),
            "2026:1": act("2026:1", section("5", "9", "Återinförd."))}  # fmt: skip
    in_force = {"2026:1": date(2026, 1, 1), "2024:5": date(2024, 5, 1)}
    download = _download(conn, "riksdagen", "https://data.riksdagen.se/x")
    conn.execute(
        "INSERT INTO unit (id, source_type, authority, language, document, heading, url)"
        " VALUES ('sfs:2005:716/5kap/9§', 'statute', 'law', 'sv', 'sfs:2005:716', 'h', 'u')"
    )
    conn.execute(
        "INSERT INTO unit_version (unit_id, text, text_hash, time_basis, valid_from, amended_by,"
        " observed_from, download_id) VALUES ('sfs:2005:716/5kap/9§', 'idag', 'h', 'valid',"
        " '2026-01-01', '2026:1', now(), %s)",
        (download,),
    )
    downloads = {sfs: _download(conn, "sfs-act", f"https://sfs/{sfs}") for sfs in acts}
    report = rebuild_history(conn, STATUTE, acts, in_force, {}, downloads, SEEN, scopes)
    assert report.stops["repealed before this act: nothing before"] == 1
    assert wordings(conn, "5kap/9§") == []  # not the 2006 wording


def test_the_acts_a_rebuild_reads_are_planned_before_fetching() -> None:
    have = {sfs: ACTS[sfs] for sfs in ("2005:716", "2026:295", "2026:1142")}
    missing = needed_acts("2005:716", have, IN_FORCE, SCOPES)
    assert {"2024:100", "2014:1400"} <= missing  # a footnote and the register name them


# --- review fixes ----------------------------------------------------------------------


def test_a_footnote_glued_to_its_number_does_not_cut_the_page() -> None:
    page = (
        "Enligt riksdagens beslut1 föreskrivs att 8 kap. 20 och 21 §§ ska ha följande lydelse.\n"
        "8 kap.\n20 §2    En utlänning som har förbjudits att\n"
        "återvända får inte resa in.\n21 §    Beslut meddelas av Migrationsverket.\n"
        "Denna lag träder i kraft den 1 januari 2010.\n"
        "1 Prop. 2008/09:156.\n2Ändringen innebär att andra stycket upphävs."
    )
    sections = parse_act([page], "2009:1542").sections
    assert [s.section for s in sections] == ["20", "21"]
    assert sections[0].text == "En utlänning som har förbjudits att återvända får inte resa in."
    assert sections[0].footnote.startswith("Ändringen innebär")


def test_a_heading_item_in_the_scope_does_not_hide_the_sections_after_it() -> None:
    items = parse_scope(
        "ändr. 8 kap. 21, 22 §§, rubr. närmast före 21 §, 12 kap. 14 b, 15 §§, 16 kap. 6 §"
    )
    assert [(i.chapter, i.first) for i in items] == [
        ("8", "21"), ("8", "22"), ("12", "14 b"), ("12", "15"), ("16", "6"),
    ]  # fmt: skip


def test_den_nya_names_one_section_not_the_whole_chapter() -> None:
    items = parse_scope("ändr. 5 kap. den nya 2 c §")
    assert [(i.chapter, i.first) for i in items] == [("5", "2 c")]


def test_an_act_named_by_the_register_for_a_note_without_senaste_lydelse_is_fetched() -> None:
    have = {
        "2005:716": ACTS["2005:716"],
        "2026:1142": act(
            "2026:1142", section("4", "3", "Ny.", footnote="Ändringen innebär att X tas bort.")
        ),
    }
    assert "2014:1400" in needed_acts("2005:716", have, IN_FORCE, SCOPES)


@pytest.mark.db
def test_a_corrected_in_force_date_replaces_the_earlier_rebuild(corpus) -> None:
    rebuild(corpus)
    IN_FORCE["2024:100"] = date(2024, 4, 1)  # the register corrects the act's date
    try:
        downloads = {sfs: _download(corpus, "sfs-act", f"https://sfs/{sfs}/2") for sfs in ACTS}
        later = datetime(2026, 11, 25, tzinfo=UTC)
        kinds = dict.fromkeys(IN_FORCE, "Lag")
        report = rebuild_history(corpus, STATUTE, ACTS, IN_FORCE, kinds, downloads, later, SCOPES)
    finally:
        IN_FORCE["2024:100"] = date(2024, 3, 1)
    assert report.stops["period already covered by another rebuilt wording"] == 0
    assert [w[:2] for w in wordings(corpus, "6kap/2§")] == [
        (date(2021, 7, 1), date(2024, 4, 1)),
        (date(2024, 4, 1), date(2026, 6, 1)),
    ]


@pytest.mark.db
def test_a_wording_ending_on_a_government_decision_is_walked_too(corpus) -> None:
    corpus.execute(
        "UPDATE unit_version SET pending = 'ends' WHERE unit_id = 'sfs:2005:716/6kap/2§'"
    )
    rebuild(corpus)
    assert len(wordings(corpus, "6kap/2§")) == 2


@pytest.mark.db
def test_a_walk_that_stops_leaves_no_empty_unit_behind(corpus) -> None:
    saved = ACTS.pop("2013:606")
    try:
        report = rebuild(corpus)
    finally:
        ACTS["2013:606"] = saved
    assert report.units_created == 0
    assert corpus.execute(
        "SELECT count(*) FROM unit WHERE id = 'sfs:2005:716/6akap/9§'"
    ).fetchone() == (0,)


def test_an_unreadable_act_pdf_is_reported_not_fatal(tmp_path, monkeypatch) -> None:
    from permitnav.cli import _parsed_acts
    from permitnav.sources.raw import save_raw

    monkeypatch.setenv("RAW_DIR", str(tmp_path))
    folder = tmp_path / "sfs-acts" / "sfs-2024-1"
    save_raw(folder, "sfs-act", "u", b"%PDF-1.4 truncated", 200, "pdf", SEEN)
    unreadable: list[str] = []
    assert _parsed_acts(unreadable) == {}
    assert unreadable and unreadable[0].startswith("2024:1")
