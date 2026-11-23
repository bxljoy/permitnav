"""Statute parser, tested on real excerpts of utlänningslagen (2005:716) from the riksdagen API.

Fixture: tests/fixtures/utlanningslagen_excerpt.txt (header, 5 kap. 1–2 d §, 6 kap., and the
transitional provisions of SFS 2026:295 and 2026:1687), fetched 2026-11-20 from
https://data.riksdagen.se/dokument/sfs-2005-716.text
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from permitnav.parsing.statute import parse_statute
from permitnav.parsing.types import ParsedUnit

FIXTURE = Path(__file__).parent / "fixtures" / "utlanningslagen_excerpt.txt"
URL = "https://data.riksdagen.se/dokument/sfs-2005-716.text"


@pytest.fixture(scope="module")
def statute():
    return parse_statute(FIXTURE.read_text(encoding="utf-8"), url=URL)


def units(statute, unit_id: str) -> list[ParsedUnit]:
    return [u for u in statute.units if u.unit_id == unit_id]


def one(statute, unit_id: str) -> ParsedUnit:
    found = units(statute, unit_id)
    assert len(found) == 1, f"{unit_id}: expected 1 unit, found {len(found)}"
    return found[0]


# --- the document -------------------------------------------------------------------------


def test_reads_the_document_header(statute) -> None:
    assert statute.document == "sfs:2005:716"
    assert statute.title == "Utlänningslag (2005:716)"
    assert statute.authority == "law"
    assert statute.amended_through == "2026:1752"


def test_ordinance_header_gives_ordinance_authority() -> None:
    text = (
        "Utlänningsförordning (2006:97)\n\nSFS nr:     2006:97\n"
        "Ändrad:     t.o.m. SFS 2026:1746\n\n\n"
        "5 kap. Arbetstillstånd\n\n1 § En utlänning är undantagen. Förordning (2013:641).\n"
    )
    parsed = parse_statute(text, url="https://data.riksdagen.se/dokument/sfs-2006-97.text")
    assert parsed.document == "sfs:2006:97"
    assert parsed.authority == "ordinance"
    unit = one(parsed, "sfs:2006:97/5kap/1§")
    assert unit.authority == "ordinance"
    assert unit.amended_by == "2013:641"


# --- sections -----------------------------------------------------------------------------


def test_section_ids_heading_and_text(statute) -> None:
    unit = one(statute, "sfs:2005:716/6kap/2§")
    assert unit.source_type == "statute"
    assert unit.language == "sv"
    assert unit.url == URL
    assert unit.heading == "6 kap. Arbetstillstånd, 2 §"
    assert unit.text.startswith("Arbetstillstånd får ges till en utlänning som har ett")
    assert "90 procent av medianlönen" in unit.text
    assert unit.text.endswith("Lag (2026:295).")
    assert unit.time_basis == "valid"


def test_lettered_sections_and_chapters_get_compact_ids(statute) -> None:
    assert one(statute, "sfs:2005:716/6kap/2a§").text.startswith("Ett arbetstillstånd enligt")
    assert one(statute, "sfs:2005:716/5kap/2d§")


def test_amendment_marker_is_read(statute) -> None:
    assert one(statute, "sfs:2005:716/6kap/2a§").amended_by == "2022:303"
    assert one(statute, "sfs:2005:716/6kap/1§").amended_by == "2014:198"


def test_paragraphs_of_a_section_stay_together(statute) -> None:
    unit = one(statute, "sfs:2005:716/6kap/2§")
    assert "Arbetstillstånd får även ges till en utlänning som deltar" in unit.text
    assert "\n\n" in unit.text  # paragraph breaks are kept


def test_subheadings_go_into_the_next_sections_heading_not_the_previous_text(statute) -> None:
    unit = one(statute, "sfs:2005:716/5kap/2d§")
    assert unit.heading == (
        "5 kap. Uppehållstillstånd, Uppehållstillstånd för familjemedlemmar till utlänningar "
        "som har ställning som varaktigt bosatt i Sverige och som återtagits enligt "
        "5 a kap. 7 §, 2 d §"
    )
    # The subheading before 6 kap. must not leak into 2 d §.
    assert unit.text.endswith("Lag (2026:1221).")
    assert "medborgare" not in unit.text


def test_wrapped_chapter_titles_are_joined() -> None:
    text = (
        "Utlänningslag (2005:716)\n\nSFS nr:     2005:716\n\n\n"
        "1 kap. Lagens innehåll, vissa definitioner och allmänna\nbestämmelser\n\n"
        "1 § I denna lag finns föreskrifter. Lag (2014:198).\n"
    )
    unit = one(parse_statute(text, url=URL), "sfs:2005:716/1kap/1§")
    assert (
        unit.heading == "1 kap. Lagens innehåll, vissa definitioner och allmänna bestämmelser, 1 §"
    )


def test_a_chapter_title_on_a_line_of_its_own_still_starts_the_chapter() -> None:
    """The consolidated text prints chapter 22 as "22 kap. " / blank line / "Tribunalvittnen";
    read as text, the whole chapter ended up inside 21 kap. 10 §."""
    text = (
        "Utlänningslag (2005:716)\n\nSFS nr:     2005:716\n\n\n"
        "21 kap. Tillfälligt skydd\n\n"
        "10 § Beslut enligt detta kapitel meddelas av Migrationsverket.\n\n"
        "22 kap. \r\n\r\nTribunalvittnen\n\n"
        "1 § I detta kapitel finns bestämmelser om skydd för vittnen.\n\n"
        "2 § Ett tidsbegränsat uppehållstillstånd ska ges. Lag (2026:1142).\n"
    )
    statute = parse_statute(text, url=URL)
    assert one(statute, "sfs:2005:716/21kap/10§").text == (
        "Beslut enligt detta kapitel meddelas av Migrationsverket."
    )
    assert one(statute, "sfs:2005:716/22kap/1§").heading == "22 kap. Tribunalvittnen, 1 §"
    assert one(statute, "sfs:2005:716/22kap/2§").amended_by == "2026:1142"


def test_a_wrapped_title_on_its_own_lines_is_joined_and_a_bare_heading_warns() -> None:
    head = "Utlänningslag (2005:716)\n\nSFS nr:     2005:716\n\n\n"
    wrapped = (
        head + "22 kap. Tribunalvittnen\n\n7 § Beslut får inte överklagas.\n\n"
        "23 kap.\n\nSärskilda bemyndiganden och\nandra bestämmelser\n\n"
        "1 § Regeringen får meddela föreskrifter.\n"
    )
    statute = parse_statute(wrapped, url=URL)
    assert one(statute, "sfs:2005:716/23kap/1§").heading == (
        "23 kap. Särskilda bemyndiganden och andra bestämmelser, 1 §"
    )
    bare = head + "22 kap. Tribunalvittnen\n\n7 § Text.\n\n23 kap.\n\n1 § Regeringen får.\n"
    assert any(
        "chapter heading without a title" in w for w in parse_statute(bare, url=URL).warnings
    )


def test_repealed_sections_are_listed_not_returned_as_units(statute) -> None:
    assert units(statute, "sfs:2005:716/5kap/1c§") == []
    assert units(statute, "sfs:2005:716/5kap/2§") == []
    assert statute.repealed["sfs:2005:716/5kap/1c§"] == "2026:1142"
    assert statute.repealed["sfs:2005:716/5kap/2§"] == "2026:1142"


# --- versions and dates -------------------------------------------------------------------


def test_expiring_and_upcoming_wordings_become_two_versions(statute) -> None:
    current, upcoming = units(statute, "sfs:2005:716/6kap/4§")
    assert current.valid_to == date(2028, 1, 1)
    assert current.amended_by == "2026:1686"
    assert upcoming.valid_from == date(2028, 1, 1)
    assert upcoming.valid_to is None
    assert upcoming.amended_by == "2026:1687"
    for unit in (current, upcoming):
        assert "/" not in unit.text.splitlines()[0]  # the marker is not part of the text
        assert unit.text.startswith("En utlänning som vill ha arbetstillstånd")


def test_valid_from_comes_from_the_amending_acts_in_force_date(statute) -> None:
    # 6 kap. 2 § was last amended by 2026:295, which "träder i kraft den 1 juni 2026".
    assert one(statute, "sfs:2005:716/6kap/2§").valid_from == date(2026, 6, 1)


def test_valid_from_is_unknown_when_the_amending_act_is_not_in_the_text(statute) -> None:
    # 2022:303's transitional provisions are not in the fixture.
    assert one(statute, "sfs:2005:716/6kap/2a§").valid_from is None


# --- transitional provisions --------------------------------------------------------------


def test_transitional_provisions_are_units_per_point(statute) -> None:
    point = one(statute, "sfs:2026:295/övergång/2")
    assert point.source_type == "transitional"
    assert point.authority == "law"
    assert point.heading == "Övergångsbestämmelser till SFS 2026:295, punkt 2"
    assert point.text.startswith("För ansökningar om förlängning av ett arbetstillstånd")
    assert point.text.endswith("senast den 1 december 2026.")
    assert point.valid_from == date(2026, 6, 1)
    assert [u.unit_id for u in statute.units if u.unit_id.startswith("sfs:2026:1687/")] == [
        "sfs:2026:1687/övergång/1",
        "sfs:2026:1687/övergång/2",
    ]


def test_transitional_provisions_link_to_the_sections_their_act_amended(statute) -> None:
    point = one(statute, "sfs:2026:295/övergång/2")
    assert ("transitional_for", "sfs:2005:716/6kap/2§") in point.links
    later = one(statute, "sfs:2026:1687/övergång/2")
    assert ("transitional_for", "sfs:2005:716/6kap/4§") in later.links


def test_in_force_dates_per_amending_act(statute) -> None:
    assert statute.in_force["2026:295"] == date(2026, 6, 1)
    assert statute.in_force["2026:1687"] == date(2028, 1, 1)


# --- the whole law, when it has been downloaded ---------------------------------------------

FULL = Path(__file__).resolve().parents[1] / "data" / "raw"


@pytest.mark.parametrize("name", ["sfs-2005-716.txt", "sfs-2006-97.txt"])
def test_full_text_parses_cleanly(name: str) -> None:
    path = FULL / name
    if not path.exists():
        pytest.skip(f"{path} not downloaded")
    parsed = parse_statute(
        path.read_text(encoding="utf-8"), url=f"https://data.riksdagen.se/dokument/{name[:-4]}.text"
    )
    sections = [u for u in parsed.units if u.source_type == "statute"]
    assert len(sections) > 200
    assert parsed.warnings == []
    # Several wordings of one section must form a chain: each ends when the next starts
    # (or both ends are "on a day the government decides").
    by_id: dict[str, list[ParsedUnit]] = {}
    for unit in sections:
        by_id.setdefault(unit.unit_id, []).append(unit)
    for unit_id, versions in by_id.items():
        for first, second in zip(versions, versions[1:], strict=False):
            if first.pending == "ends":
                assert second.pending == "starts", unit_id
            else:
                assert first.valid_to is not None and first.valid_to == second.valid_from, unit_id
    assert all(u.text.strip() for u in parsed.units)


# --- format variants seen in the full laws (inline, so they also run in CI) -----------------

HEADER = "Utlänningslag (2005:716)\n\nSFS nr:     2005:716\n\n\n10 kap. Uppsikt och förvar\n\n"


def test_renumbering_marker_ends_the_old_number() -> None:
    text = HEADER + (
        "2 § /Ny beteckning 10 kap. 4 § U:2027-12-01/\n"
        "En utlänning får ställas under uppsikt. Lag (2026:1425).\n\n"
        "2 § /Träder i kraft I:2027-12-01/\nUppsikt innebär en skyldighet. Lag (2026:1425).\n"
    )
    old, new = units(parse_statute(text, url=URL), "sfs:2005:716/10kap/2§")
    assert old.valid_to == date(2027, 12, 1)
    assert old.text.startswith("En utlänning")
    assert new.valid_from == date(2027, 12, 1)


def test_the_ordinances_misspelt_renumbering_marker_is_handled() -> None:
    text = HEADER + "14 b § /Ny betekning 7 kap. 14 c § U:2027-12-01/\nText. Lag (2026:1).\n"
    (unit,) = units(parse_statute(text, url=URL), "sfs:2005:716/10kap/14b§")
    assert unit.valid_to == date(2027, 12, 1)


def test_changes_on_a_day_the_government_decides_are_pending() -> None:
    text = HEADER + (
        "1 § /Upphör att gälla U:den dag som regeringen bestämmer/\n"
        "Gammal lydelse. Lag (2014:1).\n\n"
        "1 § /Träder i kraft I:den dag som regeringen bestämmer/\nNy lydelse. Lag (2022:1847).\n"
    )
    old, new = units(parse_statute(text, url=URL), "sfs:2005:716/10kap/1§")
    assert (old.pending, old.valid_to) == ("ends", None)
    assert (new.pending, new.valid_from) == ("starts", None)


def test_a_wording_that_starts_and_ends_on_the_same_day_is_dropped() -> None:
    text = HEADER + (
        "14 c § /Träder i kraft I:2027-12-01/\n/Upphör att gälla U:2027-12-01/\n"
        "X. Lag (2026:1).\n\n"
        "14 c § /Träder i kraft I:2027-12-01/\nY. Lag (2026:2).\n"
    )
    (unit,) = units(parse_statute(text, url=URL), "sfs:2005:716/10kap/14c§")
    assert unit.text == "Y. Lag (2026:2)."


def test_marked_subheadings_follow_the_matching_wording() -> None:
    text = HEADER + (
        "/Rubriken upphör att gälla U:2028-01-01/\nAvvisning när handlingar saknas\n\n"
        "/Rubriken träder i kraft I:2028-01-01/\n"
        "Avvisning när handlingar eller tillstånd saknas\n\n"
        "8 § /Upphör att gälla U:2028-01-01/\nGammal. Lag (2014:1).\n\n"
        "8 § /Träder i kraft I:2028-01-01/\nNy. Lag (2026:1).\n"
    )
    old, new = units(parse_statute(text, url=URL), "sfs:2005:716/10kap/8§")
    assert old.heading == "10 kap. Uppsikt och förvar, Avvisning när handlingar saknas, 8 §"
    assert new.heading == (
        "10 kap. Uppsikt och förvar, Avvisning när handlingar eller tillstånd saknas, 8 §"
    )


def test_a_reused_section_number_after_a_repeal_is_a_new_section() -> None:
    text = HEADER + (
        "3 c § Har upphävts genom lag (2014:198).\n\n"
        "Det gemensamma europeiska systemet\n\n"
        "3 c § I denna lag avses med\n\n- skyddsgrundsförordningen: förordning (EU) 2024/1347.\n"
    )
    parsed = parse_statute(text, url=URL)
    (unit,) = units(parsed, "sfs:2005:716/10kap/3c§")
    assert unit.text.startswith("I denna lag avses med\n\n- skyddsgrundsförordningen")
    assert "sfs:2005:716/10kap/3c§" not in parsed.repealed
    assert parsed.warnings == []


# --- regressions from the code review (2026-11-20) -------------------------------------------


def test_titles_with_text_after_the_number_are_read() -> None:
    text = (
        "Lag (2016:752) om tillfälliga begränsningar av\nmöjligheten att få uppehållstillstånd\n\n"
    )
    text += "1 § Denna lag gäller. Lag (2019:481).\n"
    parsed = parse_statute(text, url=URL)
    assert parsed.document == "sfs:2016:752"
    assert parsed.title == (
        "Lag (2016:752) om tillfälliga begränsningar av möjligheten att få uppehållstillstånd"
    )
    assert parsed.authority == "law"


def test_a_law_about_an_eu_regulation_is_still_a_law() -> None:
    text = "Lag (2022:1155) med kompletterande bestämmelser till EU:s förordning om X\n\n"
    text += "1 § Text. Lag (2022:1155).\n"
    assert parse_statute(text, url=URL).authority == "law"


def test_a_paragraph_starting_with_a_later_section_reference_is_not_a_section() -> None:
    text = HEADER + (
        "2 § Första.\n\n4 § gäller också den som X. Lag (2014:1).\n\n"
        "3 § Tre. Lag (2014:2).\n\n4 § Fyra. Lag (2014:3).\n"
    )
    parsed = parse_statute(text, url=URL)
    assert [u.unit_id[-3:] for u in parsed.units] == ["/2§", "/3§", "/4§"]
    assert one(parsed, "sfs:2005:716/10kap/2§").amended_by == "2014:1"
    assert one(parsed, "sfs:2005:716/10kap/4§").text == "Fyra. Lag (2014:3)."


def test_a_paragraph_starting_with_the_same_section_number_stays_in_the_section() -> None:
    text = HEADER + (
        "2 § Första stycket.\n\n2 § första stycket gäller inte barn\n\n"
        "Tredje stycket. Lag (2014:1).\n\n3 § Nästa.\n"
    )
    parsed = parse_statute(text, url=URL)
    assert one(parsed, "sfs:2005:716/10kap/2§").text.endswith("Tredje stycket. Lag (2014:1).")
    assert one(parsed, "sfs:2005:716/10kap/3§").heading == "10 kap. Uppsikt och förvar, 3 §"
    assert parsed.warnings == []


def test_a_paragraph_starting_with_a_chapter_reference_is_not_a_chapter() -> None:
    text = HEADER + "2 § Första.\n\n8 kap. gäller också X. Lag (2014:1).\n\n3 § Tre.\n"
    parsed = parse_statute(text, url=URL)
    assert one(parsed, "sfs:2005:716/10kap/2§").amended_by == "2014:1"
    assert one(parsed, "sfs:2005:716/10kap/3§")


def test_an_undated_start_is_kept_even_when_an_end_date_follows() -> None:
    text = HEADER + (
        "8 § /Träder i kraft I:den dag som regeringen bestämmer/\n"
        "/Upphör att gälla U:2028-01-01/\nX. Lag (2026:1).\n"
    )
    unit = one(parse_statute(text, url=URL), "sfs:2005:716/10kap/8§")
    assert unit.pending == "starts"


def test_a_wording_the_government_will_start_gets_no_in_force_date() -> None:
    text = HEADER + (
        "8 § /Träder i kraft I:den dag som regeringen bestämmer/\nNy. Lag (2026:1).\n\n"
        "Övergångsbestämmelser\n\n2026:1\n\n1. Denna lag träder i kraft den 1 juli 2026.\n"
    )
    unit = one(parse_statute(text, url=URL), "sfs:2005:716/10kap/8§")
    assert (unit.pending, unit.valid_from) == ("starts", None)


def test_a_removed_subheading_does_not_carry_over_to_the_new_wording() -> None:
    text = HEADER + (
        "/Rubriken upphör att gälla U:2028-01-01/\nGammal rubrik\n\n"
        "8 § /Upphör att gälla U:2028-01-01/\nGammal. Lag (2014:1).\n\n"
        "8 § /Träder i kraft I:2028-01-01/\nNy. Lag (2026:1).\n"
    )
    old, new = units(parse_statute(text, url=URL), "sfs:2005:716/10kap/8§")
    assert old.heading == "10 kap. Uppsikt och förvar, Gammal rubrik, 8 §"
    assert new.heading == "10 kap. Uppsikt och förvar, 8 §"


def test_a_numbered_list_inside_a_transitional_point_stays_in_the_point() -> None:
    text = HEADER + (
        "1 § Text. Lag (2026:1).\n\nÖvergångsbestämmelser\n\n2026:1\n\n"
        "1. Denna lag träder i kraft den 1 juli 2026.\n\n2. Äldre föreskrifter gäller för\n\n"
        "1. ansökningar, och\n\n2. beslut.\n"
    )
    parsed = parse_statute(text, url=URL)
    ids = [u.unit_id for u in parsed.units if u.source_type == "transitional"]
    assert ids == ["sfs:2026:1/övergång/1", "sfs:2026:1/övergång/2"]
    assert one(parsed, "sfs:2026:1/övergång/2").text.endswith("2. beslut.")


def test_an_upcoming_wording_listed_before_the_expiring_one_is_still_split() -> None:
    text = HEADER + (
        "8 § /Träder i kraft I:2028-01-01/\nNy. Lag (2026:1).\n\n"
        "8 § /Upphör att gälla U:2028-01-01/\nGammal. Lag (2014:1).\n"
    )
    new, old = units(parse_statute(text, url=URL), "sfs:2005:716/10kap/8§")
    assert (new.valid_from, new.amended_by, new.text) == (
        date(2028, 1, 1),
        "2026:1",
        "Ny. Lag (2026:1).",
    )
    assert (old.valid_to, old.amended_by) == (date(2028, 1, 1), "2014:1")


def test_a_transitional_point_with_two_wordings_becomes_two_dated_versions() -> None:
    # Real case: SFS 2026:1425 punkt 4.
    text = HEADER + (
        "1 § Text. Lag (2026:1425).\n\nÖvergångsbestämmelser\n\n2026:1425\n\n"
        "1. Denna lag träder i kraft den 21 juli 2026.\n\n"
        "2. Äldre.\n\n3. Annat.\n\n"
        "4. /Upphör att gälla U:2027-12-01/\nRäknas enligt 10 kap. 12 §.\n\n"
        "4. /Träder i kraft I:2027-12-01/\nRäknas enligt 10 kap. 15 §.\nLag (2026:1427).\n\n"
        "5. Sist.\n"
    )
    parsed = parse_statute(text, url=URL)
    old, new = units(parsed, "sfs:2026:1425/övergång/4")
    assert (old.valid_from, old.valid_to) == (date(2026, 7, 21), date(2027, 12, 1))
    assert old.text == "Räknas enligt 10 kap. 12 §."
    assert (new.valid_from, new.valid_to) == (date(2027, 12, 1), None)
    assert one(parsed, "sfs:2026:1425/övergång/5").text == "Sist."
