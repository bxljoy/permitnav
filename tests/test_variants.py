"""Near-duplicate page variants (phase 2, slice 5)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

from permitnav.retrieval.retriever import Hit
from permitnav.retrieval.variants import (
    SIBLING_MARGIN,
    collapse_variants,
    named_variants,
    preferred_siblings,
    variant_key,
    with_siblings,
)

EXT = "web:en/employers/extending-a-permit/"
PREP = "#applicationpreparationsforemployers"


def hit(unit: str) -> Hit:
    now = datetime(2026, 11, 20, tzinfo=UTC)
    return Hit(unit, 1, 1, "t", 0.5, "web_page", "h", "u", None, None, now, unit)


def test_the_same_section_on_sibling_pages_is_one_variant_group() -> None:
    assert variant_key(EXT + "employees" + PREP) == variant_key(EXT + "seasonal-workers" + PREP)
    # Numbered anchors are different sections (review finding 1).
    assert variant_key(EXT + "employees#steg-1") != variant_key(EXT + "employees#steg-2")
    # Only the known families: pages for a first application and an extension differ (2).
    assert variant_key("web:sv/du-vantar-pa-beslut/arbeta/du-har-ansokt-om-forlangning#x") is None
    assert variant_key(EXT + "employees" + PREP) != variant_key(EXT + "employees#other")
    assert variant_key(EXT + "employees#intro") is None  # every page has its own intro
    assert variant_key("sfs:2005:716/6kap/2§") is None


def test_a_group_takes_one_slot_and_the_general_page_wins() -> None:
    hits = [
        hit(EXT + "seasonal-workers" + PREP),
        hit("sfs:2005:716/6kap/2§"),
        hit(EXT + "ict-permits" + PREP),
        hit(EXT + "employees" + PREP),
        hit(EXT + "employees#other"),
    ]
    out = [h.unit_id for h in collapse_variants(hits, "What must my employer prepare?")]
    assert out == [EXT + "employees" + PREP, "sfs:2005:716/6kap/2§", EXT + "employees#other"]


def test_the_variant_a_question_names_wins() -> None:
    hits = [hit(EXT + "employees" + PREP), hit(EXT + "seasonal-workers" + PREP)]
    out = collapse_variants(hits, "I'm a seasonal worker picking berries. What must be prepared?")
    assert [h.unit_id for h in out] == [EXT + "seasonal-workers" + PREP]
    sv = "web:sv/arbetsgivare/forlanga-tillstand/"
    hits = [hit(sv + "anstalld#x"), hit(sv + "artist#x")]
    assert [h.unit_id for h in collapse_variants(hits, "Jag är artist")] == [sv + "artist#x"]


def test_without_a_general_page_or_a_named_one_the_best_ranked_stays() -> None:
    hits = [hit(EXT + "ict-permits" + PREP), hit(EXT + "seasonal-workers" + PREP)]
    assert [h.unit_id for h in collapse_variants(hits, "What is needed?")] == [hits[0].unit_id]


def test_pages_outside_the_variant_families_are_never_collapsed() -> None:
    base = "web:sv/du-vantar-pa-beslut/arbeta/"
    hits = [
        hit(base + "du-har-ansokt-om-forlangning#x"),
        hit(base + "du-har-ansokt-for-forsta-gangen#x"),
    ]
    assert collapse_variants(hits, "Kan jag arbeta?") == hits


def test_the_shown_variant_carries_the_groups_best_score() -> None:
    import dataclasses

    best = dataclasses.replace(hit(EXT + "seasonal-workers" + PREP), score=0.70)
    general = dataclasses.replace(hit(EXT + "employees" + PREP), score=0.57)
    (shown,) = collapse_variants([best, general], "What must be prepared?")
    assert (shown.unit_id, shown.score) == (general.unit_id, 0.70)


def test_named_variants_are_matched_on_words_not_parts_of_words() -> None:
    assert named_variants("Can an athlete or a coach extend?") == {"athletes-and-coaches"}
    assert named_variants("Jag har ett EU-blåkort") == {"eu-blue-cards"}
    assert named_variants("My employer is self-employed?") == {"self-employed-people"}
    assert named_variants("What does the employer have to do?") == set()
    assert named_variants("Who is the article about?") == set()  # "art" is not "artist"
    assert named_variants("I work for an international company") == set()  # not "intern"
    assert named_variants("We hire interns and athletes") == {"trainees", "athletes-and-coaches"}
    assert named_variants("Jag är säsongsarbetare") == {"seasonal-workers"}
    # Everyday words are not names of a group (finding 4).
    assert named_variants("Vem bär ansvaret för att lönen är rätt i praktiken?") == set()
    assert named_variants("I'm the family member of a student. Can I work?") == set()
    assert named_variants("Can I do research on my rights? Ship my documents?") == set()


def test_a_group_keeps_its_best_cosine_score_when_the_reranker_put_another_first() -> None:
    import dataclasses

    artist = dataclasses.replace(hit(EXT + "artists" + PREP), score=0.55, rerank_score=3.0)
    general = dataclasses.replace(hit(EXT + "employees" + PREP), score=0.62, rerank_score=1.0)
    (shown,) = collapse_variants([artist, general], "What must be prepared?")
    assert (shown.unit_id, shown.score, shown.rerank_score) == (general.unit_id, 0.62, 3.0)


def test_the_page_to_show_is_asked_for_when_only_a_sibling_was_found() -> None:
    key = variant_key(EXT + "employees" + PREP)
    assert key is not None

    def preferred(hits: list[Hit], question: str, use_named: bool = True) -> dict:
        """The ids asked for directly under the group's path (they are also asked for under
        each category directory: see the last test)."""
        wanted = preferred_siblings(hits, question, use_named=use_named)
        return {k: [u for u in ids if "/" not in u[len(EXT) :]] for k, ids in wanted.items()}

    found = [hit(EXT + "seasonal-workers" + PREP), hit("sfs:2005:716/6kap/2§"), hit(EXT + "x#y")]
    general = sorted(
        EXT + slug + PREP for slug in ("anstalld", "anstallning", "employees", "employment")
    )
    # No variant named: the general page's section, under each general page name.
    assert preferred(found, "What must my employer prepare?") == {key: general}
    # The question names a variant that was not found: that one, and the general page in case
    # the named page has no such section (collapse_variants prefers the named one).
    artists = [EXT + slug + PREP for slug in ("artist", "artister", "artists")]
    asked = "What must the employer of an artist prepare?"
    assert preferred(found, asked) == {key: artists + general}
    assert preferred([*found, hit(EXT + "employees" + PREP)], asked) == {key: artists}
    # A variant named only to rule it out is not fetched.
    ruled_out = "An ordinary permit, not for an artist. What must my employer prepare?"
    with_general = [hit(EXT + "employees" + PREP), hit(EXT + "x#y")]
    assert preferred(with_general, ruled_out, use_named=False) == {}
    assert preferred(found, ruled_out, use_named=False) == {key: general}
    # Nothing to look up when the page to show is already there, or nothing has variants.
    assert preferred([*found, hit(EXT + "employees" + PREP)], "What to prepare?") == {}
    assert preferred(found, "What must a seasonal worker's employer prepare?") == {}
    assert preferred([hit("sfs:2005:716/6kap/2§")], "What to prepare?") == {}


def test_a_looked_up_sibling_joins_its_group_and_is_the_one_shown() -> None:
    law, seasonal = "sfs:2005:716/6kap/2§", EXT + "seasonal-workers" + PREP
    found = [hit(law), hit(seasonal), hit(EXT + "x#y")]
    general = dataclasses.replace(hit(EXT + "employees" + PREP), score=0.9)
    joined = with_siblings(found, [general, hit(EXT + "employees#unrelated")])
    assert [h.unit_id for h in joined] == [law, seasonal, EXT + "employees" + PREP, EXT + "x#y"]
    assert joined[2].score == 0.5  # at most what the search found, not the sibling's own
    shown = [h.unit_id for h in collapse_variants(joined, "What must my employer prepare?")]
    assert shown == [law, EXT + "employees" + PREP, EXT + "x#y"]  # in the group's place


def test_a_sibling_that_matches_clearly_worse_or_repeats_a_found_text_is_left_out() -> None:
    seasonal = hit(EXT + "seasonal-workers" + PREP)
    weaker = dataclasses.replace(hit(EXT + "employees" + PREP), score=0.5 - SIBLING_MARGIN - 0.01)
    close = dataclasses.replace(weaker, score=0.5 - SIBLING_MARGIN + 0.01)
    assert with_siblings([seasonal], [weaker]) == [seasonal]  # different content: found stays
    assert [h.unit_id for h in with_siblings([seasonal], [close])][1] == EXT + "employees" + PREP
    elsewhere = dataclasses.replace(hit("web:en/other/page#section"), text_hash="same text")
    copy = dataclasses.replace(hit(EXT + "employees" + PREP), text_hash="same text")
    assert with_siblings([seasonal, elsewhere], [copy]) == [seasonal, elsewhere]


EXTEND_SV = "web:sv/du-vill-forlanga/arbeta/"
WHEN = "#narkanjagansokaomforlangning"


def test_a_group_spans_the_ordinary_and_the_temporary_work_directories() -> None:
    # Migrationsverket files seasonal work under another directory than employees; the same
    # section on both took two of the four page slots (the validity question, 2026-11-27).
    employee = EXTEND_SV + "anstalld-eller-egen-foretagare/anstalld" + WHEN
    seasonal = EXTEND_SV + "tillfalligt-arbete-i-sverige/sasongsarbetare" + WHEN
    assert variant_key(employee) == variant_key(seasonal)
    hits = [hit(seasonal), hit(employee), hit(EXTEND_SV + "anstalld-eller-egen-foretagare/x#y")]
    out = [h.unit_id for h in collapse_variants(hits, "När måste jag ansöka om förlängning?")]
    assert out == [employee, EXTEND_SV + "anstalld-eller-egen-foretagare/x#y"]
    assert [h.unit_id for h in collapse_variants(hits[:2], "Jag är säsongsarbetare")] == [seasonal]
    # A directory inside the temporary-work one, and the English names.
    apply = "web:en/you-want-to-apply/work/"
    nested = "temporary-work-in-sweden/traineeships-internships/"
    assert variant_key(
        apply + nested + "traineeships-internships-within-the-context-of-higher-education#x"
    ) == variant_key(apply + "employee-or-self-employed/employees#x")
    # The pages for a first application and for an extension stay apart.
    assert variant_key(employee) != variant_key(
        "web:sv/du-vill-ansoka/arbeta/anstalld-eller-egen-foretagare/anstalld" + WHEN
    )


def test_the_pages_to_show_are_asked_for_in_every_directory_they_could_be_in() -> None:
    seasonal = EXTEND_SV + "tillfalligt-arbete-i-sverige/sasongsarbetare" + WHEN
    employee = EXTEND_SV + "anstalld-eller-egen-foretagare/anstalld" + WHEN
    wanted = preferred_siblings([hit(seasonal)], "När måste jag ansöka om förlängning?")
    (ids,) = wanted.values()
    directories = [
        "",
        "anstalld-eller-egen-foretagare/",
        "tillfalligt-arbete-i-sverige/",
        "tillfalligt-arbete-i-sverige/praktik/",
        "anstallning/",
        "tillfallig-anstallning/",
    ]
    assert ids == [
        EXTEND_SV + directory + slug + WHEN
        for slug in ("anstalld", "anstallning", "employees", "employment")
        for directory in directories
    ]
    # A named variant in the other directory than the section that was found.
    wanted = preferred_siblings([hit(employee)], "Jag är säsongsarbetare. När ansöker jag?")
    (ids,) = wanted.values()
    assert seasonal in ids and employee not in ids
    # Pages directly under the group's path (the employers' extension pages) are still asked for.
    wanted = preferred_siblings([hit(EXT + "seasonal-workers" + PREP)], "What must be prepared?")
    (ids,) = wanted.values()
    assert EXT + "employees" + PREP in ids


def scored(unit: str, score: float) -> Hit:
    return dataclasses.replace(hit(unit), score=score)


def test_temporary_work_described_without_its_name_keeps_its_own_page() -> None:
    # "Hired to pick wild berries this summer" names no variant (review, 2026-11-27): the
    # seasonal page clearly matches better, and its section can be thirty times the employees'.
    seasonal = EXTEND_SV + "tillfalligt-arbete-i-sverige/sasongsarbetare#krav"
    employee = EXTEND_SV + "anstalld-eller-egen-foretagare/anstalld#krav"
    question = "Jag ska plocka bär i sommar. Vilka krav gäller för förlängning?"
    far = [scored(seasonal, 0.80), scored(employee, 0.80 - SIBLING_MARGIN - 0.01)]
    assert [h.unit_id for h in collapse_variants(far, question)] == [seasonal]
    # About as good a match: which of them ranked first is chance, and the general page wins.
    near = [scored(seasonal, 0.80), scored(employee, 0.80 - SIBLING_MARGIN)]
    assert [h.unit_id for h in collapse_variants(near, question)] == [employee]
    # In one directory the general page wins whatever the scores, as before.
    artist = EXTEND_SV + "anstalld-eller-egen-foretagare/artist#krav"
    same = [scored(artist, 0.80), scored(employee, 0.60)]
    assert [h.unit_id for h in collapse_variants(same, question)] == [employee]


def test_a_variant_named_only_to_rule_it_out_is_not_the_one_shown() -> None:
    seasonal = EXTEND_SV + "tillfalligt-arbete-i-sverige/sasongsarbetare" + WHEN
    employee = EXTEND_SV + "anstalld-eller-egen-foretagare/anstalld" + WHEN
    question = "Jag har ett vanligt arbetstillstånd, inte säsongsarbete. När ansöker jag?"
    hits = [hit(employee), hit(seasonal)]
    assert [h.unit_id for h in collapse_variants(hits, question)] == [seasonal]
    assert [h.unit_id for h in collapse_variants(hits, question, use_named=False)] == [employee]


def test_a_categorys_own_page_is_not_a_variant_of_the_pages_in_it() -> None:
    above = "web:en/employers/you-want-to-employ/employing-someone-from-outside-the-eu-eea/"
    assert variant_key(above + "employment#x") is None  # the list of pages
    assert variant_key(above + "employment/employment#x") == variant_key(
        above + "temporary-employment/seasonal-workers#x"
    )


def test_swedish_forms_of_a_variants_name_are_recognised() -> None:
    for text in ("Säsongsarbetaren vill förlänga", "säsongsarbetarna", "Jag är säsongsanställd"):
        assert named_variants(text) == {"seasonal-workers"}, text
