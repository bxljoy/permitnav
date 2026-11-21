"""References to statute sections in free text: "6 kap. 2 §", "6 kap. 2 och 3 §§",
"6 a kap. 1 §", "10 kap. 1–17 §§", "chapter 6, section 2 a", and chapterless ones inside a
statute ("2 § andra stycket", meaning the same chapter).

Used for transitional provisions (which sections a point names), questions (which sections a
user names) and statute text (which sections a section refers to). Pure functions, no I/O.
"""

from __future__ import annotations

import re

_SECTION = r"\d+(?: [a-z])?"
_SEPARATOR = r"\s*(?:,|och|eller|–|-)\s*"
_LIST = rf"((?:{_SECTION})(?:{_SEPARATOR}{_SECTION})*)\s*(§§?)"
# "6 kap. 2 §", "6 kap 2 §", "6 kap. 2 och 3 §§", "6 kap. 2, 2 a och 3 §§", "10 kap. 1–17 §§"
SECTION_LIST = re.compile(rf"\b({_SECTION}) kap\.? {_LIST}", re.IGNORECASE)
# "2 § andra stycket", "3 eller 3 a §", "9-11 §§": in a statute, the citing section's chapter
BARE_LIST = re.compile(rf"\b{_LIST}", re.IGNORECASE)
# "chapter 6, section 2 a", "chapter 6 section 2"
ENGLISH_SECTION = re.compile(
    rf"\bchapter ({_SECTION}),? sections? ({_SECTION})(?![a-z])", re.IGNORECASE
)
# "2 § i 5 kap.": a chapterless reference that names its chapter after it
CHAPTER_AFTER = re.compile(
    rf"\s*(?:första|andra|tredje|fjärde|femte|stycket|punkt|\d|,|\s)*\bi ({_SECTION}) kap\.?",
    re.IGNORECASE,
)
# "6 kap. 2 § och 3 §": a chapterless list right after a chaptered one shares its chapter
JOINED = re.compile(r"\s*(?:,|och|eller|samt)\s*$", re.IGNORECASE)
# Common words that end like statute names ("i förslagen", "enligt underlagen").
NOT_STATUTES = frozenset(
    {"förslagen", "underlagen", "anslagen", "beslagen", "avslagen", "utslagen", "inslagen"}
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
    r"((?:lagen|förordningen|[a-zåäö]+lagen|[a-zåäö]+förordningen|[a-zåäö]+balken)"
    r"(?: \(\d{4}:\d+\))?"
    r"|\(\d{4}:\d+\)|aliens (?:act|ordinance))",
    re.IGNORECASE,
)


def _statute_after(text: str, end: int) -> str | None:
    """The statute named right after a reference ending at `end`: a document id from
    STATUTE_NAMES, "" for any other statute, None when none is named."""
    return _statute_and_end(text, end)[0]


def _statute_and_end(text: str, end: int) -> tuple[str | None, int]:
    """`_statute_after`, and where the statute's name ends (`end` when none is named)."""
    after = OTHER_STATUTE.match(text, end)
    if not after or after.group(1).lower() in NOT_STATUTES:
        return None, end
    name = after.group(1).lower()
    statute = next(
        (doc for doc, names in STATUTE_NAMES.items() if any(n in name for n in names)), ""
    )
    return statute, after.end()


def _sections(listed: str) -> list[str]:
    """ "2, 2 a och 3" -> ["2", "2 a", "3"]; "1–3" -> ["1", "2", "3"]."""
    items = LIST_ITEM.findall(listed)
    separators = re.findall(r"(,|och|eller|–|-)", listed)
    sections: list[str] = []
    for i, (number, letter) in enumerate(items):
        if i > 0 and separators[i - 1] in ("–", "-") and not letter and items[i - 1][1] == "":
            start = int(items[i - 1][0])
            sections += [str(n) for n in range(start + 1, int(number) + 1)]
        elif (
            i > 0
            and separators[i - 1] in ("–", "-")
            and letter
            and items[i - 1][0] == number
            and items[i - 1][1]
        ):  # "1 a–1 c": the letters in between
            first, last = items[i - 1][1].lower(), letter.lower()
            sections += [f"{number} {chr(c)}" for c in range(ord(first) + 1, ord(last) + 1)]
        else:
            sections.append(f"{number} {letter.lower()}" if letter else number)
    return sections


def ordered_section_refs(text: str, document: str) -> list[tuple[str, str]]:
    """(chapter, section) pairs a text names in `document`, in the order it names them, each
    once. "6 kap. 2 och 3 §§" gives [("6", "2"), ("6", "3")]. Ranges are expanded; references
    followed by another statute's name are left out."""
    found: list[tuple[str, str]] = []
    matches = [(m.start(), m.end(), m[1], m[2]) for m in SECTION_LIST.finditer(text)]
    matches += [(m.start(), m.end(), m[1], m[2]) for m in ENGLISH_SECTION.finditer(text)]
    for _, end, chapter, listed in sorted(matches):
        statute = _statute_after(text, end)
        if statute is not None and (statute == "" or statute != document):
            continue  # "8 kap. 2 § utlänningsförordningen" from a law's provision
        for section in _sections(listed):
            if (chapter.lower(), section) not in found:
                found.append((chapter.lower(), section))
    return found


def cited_units(text: str, document: str, chapter: str) -> list[str]:
    """Unit ids a statute section's text refers to, in the order it cites them, each once:
    "7 kap. 7 e §" in the same statute (or in the other one when named), and chapterless
    "2 § andra stycket" in `chapter`, the citing section's own. References to any other
    statute ("brottsbalken", "lagen (2016:752)") are left out."""
    cited: list[tuple[int, str]] = []
    spans: list[tuple[int, int, str, str | None]] = []  # start, end, chapter, statute
    for m in SECTION_LIST.finditer(text):
        statute, end = _statute_and_end(text, m.end())
        spans.append((m.start(), end, m[1].lower(), statute))
        if statute != "":
            doc = statute or document
            cited += [(m.start(), unit_id(doc, m[1].lower(), s)) for s in _sections(m[2])]
    for m in BARE_LIST.finditer(text):
        if any(a <= m.start() < b for a, b, _, _ in spans):
            continue  # the section part of "6 kap. 2 §"
        if _statute_after(text, m.end()) is not None:
            continue  # a section of a named statute needs its chapter
        own_chapter, doc = chapter, document
        if named := CHAPTER_AFTER.match(text, m.end()):
            own_chapter = named[1].lower()  # "2 § i 5 kap."
        else:
            before = [
                s for s in spans if s[1] <= m.start() and JOINED.match(text[s[1] : m.start()])
            ]
            if before:  # "6 kap. 2 § och 3 §"
                _, _, own_chapter, statute = before[-1]
                if statute == "":
                    continue
                doc = statute or document
        cited += [(m.start(), unit_id(doc, own_chapter, s)) for s in _sections(m[1])]
    ordered: list[str] = []
    for _, unit in sorted(cited, key=lambda c: c[0]):
        if unit not in ordered:
            ordered.append(unit)
    return ordered


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
