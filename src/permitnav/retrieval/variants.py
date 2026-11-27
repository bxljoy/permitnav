"""Near-duplicate page variants (phase 2, slice 5).

Migrationsverket publishes the same section on parallel pages for employees, artists,
athletes, ICT permits, seasonal workers and other groups, with a few words changed:
`…/extending-a-permit/{employees,seasonal-workers,ict-permits}#applicationpreparationsforemployers`.
They are not identical, so the identical-text filter keeps them all, and they fill the results
with one section several times. Embedding similarity cannot tell them apart from different
sections (variants 0.85–0.96, different sections on one page up to 0.95), but the structure
can: the same section anchor on sibling pages of the known variant families (general and
specialised pages, below). Siblings share a directory, or sit in the two directories the site
sorts work into (ordinary and temporary work: CATEGORIES). Other sibling pages that share a
heading, such as the pages for a first application and for an extension, are different
content and stay apart.

A variant group takes one slot, where its best-ranked member was, with that member's score.
The member shown is the variant the question names ("I'm an artist"), else the general page
(employees), else the best-ranked one. A general page from the other directory than the
best-ranked member is shown only when it matches the question about as well (SIBLING_MARGIN):
a person who describes seasonal work without the word keeps the seasonal page.
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
        "säsongsarbetar",  # -e, -en, -na
        "säsongsarbete",
        "säsongsanställd",
        "säsongsanställning",
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


# The directories Migrationsverket sorts the variant pages into: ordinary work on one side,
# temporary work on the other (`…/work/employee-or-self-employed/employees` beside
# `…/work/temporary-work-in-sweden/seasonal-workers`). The pages in the two are variants of
# each other all the same (for employers extending a permit they share one directory), so a
# group spans both: its parent path is the path above them.
CATEGORIES: dict[str, tuple[str, ...]] = {
    "sv": (
        "anstalld-eller-egen-foretagare",
        "tillfalligt-arbete-i-sverige",
        "tillfalligt-arbete-i-sverige/praktik",
        "anstallning",
        "tillfallig-anstallning",
    ),
    "en": (
        "employee-or-self-employed",
        "temporary-work-in-sweden",
        "temporary-work-in-sweden/traineeships-internships",
        "employment",
        "temporary-employment",
    ),
}


# Longest first: "temporary-work-in-sweden/traineeships-internships" before its last part.
_LONGEST_FIRST = {
    language: sorted(names, key=len, reverse=True) for language, names in CATEGORIES.items()
}
_CATEGORY_NAMES = frozenset(name for names in CATEGORIES.values() for name in names)


def _category(language: str, parent: str) -> str:
    """The category directory a parent path ends in, or ""."""
    for category in _LONGEST_FIRST.get(language, ()):
        if parent.endswith("/" + category):
            return category
    return ""


def _above_category(language: str, parent: str) -> str:
    """The parent path without its category directory, when it ends in one."""
    category = _category(language, parent)
    return parent[: -len(category) - 1] if category else parent


def _directory(unit_id: str) -> str:
    """The category directory of a page ("" when it is in none)."""
    match = UNIT.match(unit_id)
    return _category(match[1], match[2]) if match else ""


def variant_key(unit_id: str) -> tuple[str, str, str] | None:
    """(language, parent path, section anchor) for a section of a variant-family page, or None
    when it has no variants: not a page section, a page's intro, or a page outside the known
    families. The parent path leaves out the category directory (CATEGORIES). The anchor is
    compared whole: "steg-1" and "steg-2" are different sections."""
    match = UNIT.match(unit_id)
    if not match:
        return None
    language, parent, slug, anchor = match.groups()
    if anchor == "intro" or (slug not in GENERAL and slug not in SLUG_FAMILY):
        return None
    above = _above_category(language, parent)
    if above == parent and slug in _CATEGORY_NAMES:
        # A category's own page (a list of its pages), named like the general page
        # ("…/employment" above "…/employment/employment"): not a variant of the pages in it.
        return None
    return language, above, anchor


def sibling_ids(key: tuple[str, str, str], slugs: list[str]) -> list[str]:
    """The unit ids the pages `slugs` could have for a group's section: directly under the
    group's path and under each category directory (most do not exist; the caller looks
    them up)."""
    language, parent, anchor = key
    parents = [parent] + [f"{parent}/{category}" for category in CATEGORIES.get(language, ())]
    return [f"web:{language}/{above}/{slug}#{anchor}" for slug in slugs for above in parents]


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


# A looked-up sibling is shown only when it matches the question about as well as the section
# that was found (cosine similarity, at most this much lower). Most variants of a section
# differ by a few words, but some are different content under the same heading (the athletes'
# "requirements to get an extended work permit" is thirty times the employees' text): there
# the found section stays.
SIBLING_MARGIN = 0.05


def preferred_siblings(
    hits: list[Hit], question: str, use_named: bool = True
) -> dict[tuple[str, str, str], list[str]]:
    """For each variant group among `hits` that lacks the page collapse_variants would show:
    the unit ids that page's section would have, for the caller to look up. The variant the
    question names is asked for when no member is of it, and the general page when the group
    has neither (the named variant's page may not have the section). `use_named=False` for a
    question that rules something out: a variant named only to set it aside is not fetched.

    The variants of a section score within a few hundredths of each other, so which of them
    made the candidate list is chance; without this, a question about an ordinary work permit
    was shown the researchers' or the self-employed page's section because the employees' one
    ranked a few places lower."""
    named = named_variants(question) if use_named else set()
    named_slugs = sorted(s for s, family in SLUG_FAMILY.items() if family in named)
    groups: dict[tuple[str, str, str], set[str]] = {}
    for hit in hits:
        key = variant_key(hit.unit_id)
        if key is not None:
            groups.setdefault(key, set()).add(_slug(hit.unit_id))
    wanted: dict[tuple[str, str, str], list[str]] = {}
    for key, slugs in groups.items():
        if slugs & set(named_slugs):
            continue  # the named variant was found: it is shown
        ask = list(named_slugs)
        if not slugs & GENERAL:
            ask += sorted(GENERAL)
        if ask:
            wanted[key] = sibling_ids(key, ask)
    return wanted


def with_siblings(hits: list[Hit], siblings: list[Hit]) -> list[Hit]:
    """`hits` with each looked-up sibling placed right after the first member of its group
    (the group keeps its position; collapse_variants then shows the sibling). A sibling is
    left out when it matches the question clearly worse than what was found (SIBLING_MARGIN),
    or when its text is already among the hits under another unit. It takes at most the
    found members' best score: the group's score says what the search found."""
    found: dict[tuple[str, str, str], float] = {}
    texts: dict[str, tuple[str, str, str] | None] = {}
    for hit in hits:
        key = variant_key(hit.unit_id)
        texts.setdefault(hit.text_hash, key)
        if key is not None:
            found[key] = max(found.get(key, hit.score), hit.score)
    by_key: dict[tuple[str, str, str], list[Hit]] = {}
    for sibling in siblings:
        key = variant_key(sibling.unit_id)
        if key is None or key not in found:
            continue
        if sibling.score < found[key] - SIBLING_MARGIN:
            continue
        if sibling.text_hash in texts and texts[sibling.text_hash] != key:
            continue
        by_key.setdefault(key, []).append(
            dataclasses.replace(sibling, score=min(sibling.score, found[key]))
        )
    out: list[Hit] = []
    for hit in hits:
        out.append(hit)
        key = variant_key(hit.unit_id)
        if key is not None and key in by_key:
            out.extend(by_key.pop(key))
    return out


def collapse_variants(hits: list[Hit], question: str, use_named: bool = True) -> list[Hit]:
    """One slot per variant group, at its best-ranked member's position; the member shown is
    the named variant, else the general page, else the best-ranked. `use_named=False` for a
    question that rules something out: a variant named only to set it aside is not preferred.

    A general page in another category directory than the best-ranked member (the employees'
    page for a section found on the seasonal workers' page) is shown only when it matches the
    question about as well: the question may describe temporary work without naming it, and
    there the pages can differ a lot under one heading."""
    named = named_variants(question) if use_named else set()
    groups: dict[tuple[str, str, str], list[Hit]] = {}
    for hit in hits:
        key = variant_key(hit.unit_id)
        if key is not None:
            groups.setdefault(key, []).append(hit)

    def chosen(members: list[Hit]) -> Hit:
        best = members[0]
        match = next((h for h in members if SLUG_FAMILY.get(_slug(h.unit_id)) in named), None)
        if match is not None:
            return match
        for hit in members:
            if _slug(hit.unit_id) not in GENERAL:
                continue
            same = _directory(hit.unit_id) == _directory(best.unit_id)
            if same or hit.score >= best.score - SIBLING_MARGIN:
                return hit
        return best

    out: list[Hit] = []
    placed: set[tuple[str, str, str]] = set()
    for hit in hits:
        key = variant_key(hit.unit_id)
        if key is None:
            out.append(hit)
        elif key not in placed:
            placed.add(key)
            # The group's best cosine score and its first member's reranker score, so the
            # relevance floor and the ordering of source groups see what the search found, not
            # which variant is displayed. (After reranking, the first member need not have the
            # best cosine score.)
            members = groups[key]
            out.append(
                dataclasses.replace(
                    chosen(members),
                    score=max(m.score for m in members),
                    rerank_score=hit.rerank_score,
                )
            )
    return out
