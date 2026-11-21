"""References to statute sections in free text: "6 kap. 2 §", "6 kap. 2 och 3 §§",
"6 a kap. 1 §", "10 kap. 1–17 §§", "chapter 6, section 2 a".

Used for transitional provisions (which sections a point names) and for questions (which
sections a user names). Pure functions, no I/O.
"""

from __future__ import annotations

import re

_SECTION = r"\d+(?: [a-z])?"
# "6 kap. 2 §", "6 kap 2 §", "6 kap. 2 och 3 §§", "6 kap. 2, 2 a och 3 §§", "10 kap. 1–17 §§"
SECTION_LIST = re.compile(
    rf"\b({_SECTION}) kap\.? ((?:{_SECTION})(?:\s*(?:,|och|–|-)\s*{_SECTION})*)\s*(§§?)",
    re.IGNORECASE,
)
# "chapter 6, section 2 a", "chapter 6 section 2"
ENGLISH_SECTION = re.compile(
    rf"\bchapter ({_SECTION}),? sections? ({_SECTION})(?![a-z])", re.IGNORECASE
)
LIST_ITEM = re.compile(r"(\d+)(?: ([a-z])(?![a-zåäö]))?", re.IGNORECASE)  # "2 a", not "2 och"
# A reference followed by a statute's name is to that statute. Names include the SFS number
# and the English names Migrationsverket uses.
STATUTE_NAMES = {
    "sfs:2005:716": ("utlänningslagen", "2005:716", "aliens act"),
    "sfs:2006:97": ("utlänningsförordningen", "2006:97", "aliens ordinance"),
}
OTHER_STATUTE = re.compile(
    r"\s*(?:första|andra|tredje|fjärde|femte|stycket|punkt|i|enligt|av|in|of|the|under|\d|,|\s)*"
    r"((?:lagen|förordningen|[a-zåäö]+lagen|[a-zåäö]+förordningen)(?: \(\d{4}:\d+\))?"
    r"|\(\d{4}:\d+\)|aliens (?:act|ordinance))",
    re.IGNORECASE,
)


def ordered_section_refs(text: str, document: str) -> list[tuple[str, str]]:
    """(chapter, section) pairs a text names in `document`, in the order it names them, each
    once. "6 kap. 2 och 3 §§" gives [("6", "2"), ("6", "3")]. Ranges are expanded; references
    followed by another statute's name are left out."""
    own = STATUTE_NAMES.get(document, ())
    found: list[tuple[str, str]] = []
    matches = [(m.start(), m.end(), m[1], m[2]) for m in SECTION_LIST.finditer(text)]
    matches += [(m.start(), m.end(), m[1], m[2]) for m in ENGLISH_SECTION.finditer(text)]
    for _, end, chapter, listed in sorted(matches):
        after = OTHER_STATUTE.match(text, end)
        if after and not any(name in after.group(1).lower() for name in own):
            continue  # "8 kap. 2 § utlänningsförordningen" from a law's provision
        chapter = chapter.lower()
        items = LIST_ITEM.findall(listed)
        separators = re.findall(r"(,|och|–|-)", listed)
        sections: list[str] = []
        for i, (number, letter) in enumerate(items):
            if i > 0 and separators[i - 1] in ("–", "-") and not letter and items[i - 1][1] == "":
                start = int(items[i - 1][0])
                sections += [str(n) for n in range(start + 1, int(number) + 1)]
            else:
                sections.append(f"{number} {letter.lower()}" if letter else number)
        for section in sections:
            if (chapter, section) not in found:
                found.append((chapter, section))
    return found


def section_refs(text: str, document: str) -> set[tuple[str, str]]:
    """The pairs of `ordered_section_refs`, as a set."""
    return set(ordered_section_refs(text, document))


def unit_id(document: str, chapter: str, section: str) -> str:
    """'sfs:2005:716/6akap/2a§' for ("sfs:2005:716", "6 a", "2 a")."""
    return f"{document}/{chapter.replace(' ', '')}kap/{section.replace(' ', '')}§"


def named_units(text: str) -> list[str]:
    """Unit ids of the sections a text names, in the order it names them; a section in either
    statute unless the text says which (the law first). Ids that don't exist are the caller's
    to drop."""
    by_document = {doc: ordered_section_refs(text, doc) for doc in STATUTE_NAMES}
    refs = sorted(
        {ref for refs in by_document.values() for ref in refs},
        key=lambda ref: min(r.index(ref) for r in by_document.values() if ref in r),
    )
    return [unit_id(doc, *ref) for ref in refs for doc in STATUTE_NAMES if ref in by_document[doc]]
