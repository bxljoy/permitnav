"""Near-duplicate page variants (phase 2, slice 5).

Migrationsverket publishes the same section on parallel pages for employees, artists,
athletes, ICT permits, seasonal workers and other groups, with a few words changed:
`…/extending-a-permit/{employees,seasonal-workers,ict-permits}#applicationpreparationsforemployers`.
They are not identical, so the identical-text filter keeps them all, and they fill the results
with one section several times. Embedding similarity cannot tell them apart from different
sections (variants 0.85–0.96, different sections on one page up to 0.95), but the structure
can: the same section anchor on sibling pages of the known variant families (general and
specialised pages, below). Other sibling pages that share a heading, such as the pages for a
first application and for an extension, are different content and stay apart.

A variant group takes one slot, where its best-ranked member was, with that member's score.
The member shown is the variant the question names ("I'm an artist"), else the general page
(employees), else the best-ranked one.
"""

from __future__ import annotations

import dataclasses
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from permitnav.retrieval.retriever import Hit

# The general variant: what most people asking about work permits are.
GENERAL = frozenset({"anstalld", "employees", "employment", "anstallning"})
# Each specialised variant (English page slug) and the words that name it in a question,
# matched as whole words, plural endings allowed ("athlete" matches "athletes", but "intern"
# not "international"). Only unambiguous words: not "praktik" ("i praktiken"), "bär" (a verb),
# "research", "ship" or "student" ("a family member of a student"). The Swedish slugs map to
# the same family.
FAMILIES: dict[str, tuple[str, ...]] = {
    "artists": ("artist", "artister", "musician", "musiker"),
    "athletes-and-coaches": ("athlete", "coach", "idrottare", "idrott", "tränare"),
    "ict-permits": ("ict", "intra-corporate", "koncernintern", "företagsintern"),
    "eu-blue-cards": ("blue card", "blåkort", "eu-blåkort"),
    "seasonal-workers": (
        "seasonal",
        "säsongsarbetare",
        "säsongsarbete",
        "berry picker",
        "bärplockare",
        "bärplockning",
    ),
    "seafarers": ("seafarer", "sjöman", "sjömän"),
    "au-pair": ("au pair", "au-pair"),
    "researchers": ("researcher", "forskare"),
    "self-employed-people": (
        "self-employed",
        "own business",
        "egen företagare",
        "egenföretagare",
        "eget företag",
        "driver ett företag",
    ),
    "trainees": ("trainee", "intern", "praktikant", "praktikplats"),
    "working-holiday": ("working holiday", "feriearbete"),
    "students": ("students who have found work", "studenter som har fått arbete"),
}
SLUG_FAMILY: dict[str, str] = {
    "artists": "artists",
    "artist": "artists",
    "artister": "artists",
    "athletes-and-coaches": "athletes-and-coaches",
    "idrottare-och-tranare": "athletes-and-coaches",
    "ict-permits": "ict-permits",
    "ict-tillstand": "ict-permits",
    "eu-blue-cards": "eu-blue-cards",
    "eu-blakort": "eu-blue-cards",
    "seasonal-workers": "seasonal-workers",
    "sasongsarbetare": "seasonal-workers",
    "seafarers": "seafarers",
    "sjoman": "seafarers",
    "au-pair": "au-pair",
    "au-pairs": "au-pair",
    "researchers": "researchers",
    "forskare": "researchers",
    "self-employed-people": "self-employed-people",
    "egen-foretagare": "self-employed-people",
    "praktik-genom-internationellt-utbyte": "trainees",
    "praktikant-genom-internationellt-utbyte": "trainees",
    "praktik-med-anknytning-till-hogre-utbildning": "trainees",
    "praktikant-med-anknytning-till-hogre-utbildning": "trainees",
    "traineeships-internships-conducted-via-an-international-exchange-programme": "trainees",
    "trainee-intern-through-international-exchange": "trainees",
    "trainee-intern-within-the-context-of-higher-education": "trainees",
    "traineeships-internships-within-the-context-of-higher-education": "trainees",
    "working-holiday-visa-for-young-people": "working-holiday",
    "feriearbete-for-unga-personer": "working-holiday",
    "students-who-have-found-work": "students",
    "studenter-som-har-fatt-arbete": "students",
}
PLURAL = "(?:s|es|er|ar|na|en|e)?"
UNIT = re.compile(r"^web:(sv|en)/(.+)/([^/#]+)#(.+)$")


def variant_key(unit_id: str) -> tuple[str, str, str] | None:
    """(language, parent path, section anchor) for a section of a variant-family page, or None
    when it has no variants: not a page section, a page's intro, or a page outside the known
    families. The anchor is compared whole: "steg-1" and "steg-2" are different sections."""
    match = UNIT.match(unit_id)
    if not match:
        return None
    language, parent, slug, anchor = match.groups()
    if anchor == "intro" or (slug not in GENERAL and slug not in SLUG_FAMILY):
        return None
    return language, parent, anchor


def _slug(unit_id: str) -> str:
    match = UNIT.match(unit_id)
    return match[3] if match else ""


def named_variants(question: str) -> set[str]:
    """The variant families a question names."""
    text = question.lower()
    return {
        family
        for family, words in FAMILIES.items()
        if any(
            re.search(rf"(?<![a-zåäö]){re.escape(word)}{PLURAL}(?![a-zåäö])", text)
            for word in words
        )
    }


def collapse_variants(hits: list[Hit], question: str) -> list[Hit]:
    """One slot per variant group, at its best-ranked member's position; the member shown is
    the named variant, else the general page, else the best-ranked."""
    named = named_variants(question)
    groups: dict[tuple[str, str, str], list[Hit]] = {}
    for hit in hits:
        key = variant_key(hit.unit_id)
        if key is not None:
            groups.setdefault(key, []).append(hit)

    def chosen(members: list[Hit]) -> Hit:
        for prefer in (
            lambda h: SLUG_FAMILY.get(_slug(h.unit_id)) in named,
            lambda h: _slug(h.unit_id) in GENERAL,
        ):
            match = next((h for h in members if prefer(h)), None)
            if match is not None:
                return match
        return members[0]

    out: list[Hit] = []
    placed: set[tuple[str, str, str]] = set()
    for hit in hits:
        key = variant_key(hit.unit_id)
        if key is None:
            out.append(hit)
        elif key not in placed:
            placed.add(key)
            # The group's best score (its first member's), so the relevance floor and the
            # ordering of source groups see what the search found, not the display choice.
            out.append(dataclasses.replace(chosen(groups[key]), score=hit.score))
    return out
