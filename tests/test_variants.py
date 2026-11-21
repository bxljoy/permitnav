"""Near-duplicate page variants (phase 2, slice 5)."""

from __future__ import annotations

from datetime import UTC, datetime

from permitnav.retrieval.retriever import Hit
from permitnav.retrieval.variants import collapse_variants, named_variants, variant_key

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
