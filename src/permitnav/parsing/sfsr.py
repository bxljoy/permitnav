"""The amendment register (SFSR) and the SFS publication feed (phase 4, slice 2).

The Riksdag's consolidated text marks each amended section with its act ("Lag (2026:385)")
but gives in-force dates only for changes still to come. The register at rkrattsbaser.gov.se
(Regeringskansliet) lists every amending act of a statute with its scope and in-force date
("Ikraft"), so a wording's start date can be taken from an official source instead of being
unknown. The SFS feed (svenskforfattningssamling.se, the authentic publication) announces new
acts as they are issued, usually before the register and the consolidated text include them.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date

DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
PENDING = "den dag regeringen bestämmer"
STATUTES = ("2005:716", "2006:97")


@dataclass(frozen=True)
class Act:
    sfs: str  # "2026:385"
    title: str
    scope: str  # "ändr. 1 kap. 1, 7 §§, …; nya 9 a kap. …"
    in_force: date | None  # None: the register gives no date, or several
    partly_pending: bool  # some parts take effect "on a day the government decides"
    transitional: bool  # the act has transitional provisions ("överg.best.")


@dataclass
class Register:
    statute: str  # "2005:716"
    in_force: date | None  # the base act's own in-force date
    acts: list[Act] = field(default_factory=list)


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _fields(block: str) -> dict[str, str]:
    return {
        _text(m.group(1)): _text(m.group(2))
        for m in re.finditer(r'<span class="bold">([^<]+):</span>(.*?)</div>', block, re.S)
    }


def _in_force(value: str) -> tuple[date | None, bool]:
    """The act's one in-force date. Several dates ("2014-07-01, 2015-01-01 för 5 kap. 3 §")
    mean parts start at different times, and the register does not say which sections: no
    single date, so its wordings stay undated rather than get a wrong start."""
    dates = set(DATE.findall(value))
    single = date.fromisoformat(dates.pop()) if len(dates) == 1 else None
    return single, PENDING in value


def parse_register(content: str, statute: str) -> Register:
    """The register page of one statute: its own in-force date and every amending act."""
    head, *blocks = re.split(r'<div class="result-inner-sub-box-header">', content)
    base = re.search(r"Ikraft:\s*(?:</span>)?\s*(\d{4}-\d{2}-\d{2})", _text(head))
    register = Register(statute, date.fromisoformat(base.group(1)) if base else None)
    if not blocks:
        raise ValueError(f"register for {statute}: no amending acts found (format changed?)")
    for block in blocks:
        header = _text(block.split("</div>", 1)[0])
        number = re.search(r"SFS (\d{4}:\d+)", header)
        if number is None:
            continue
        fields = _fields(block)
        in_force, partly_pending = _in_force(fields.get("Ikraft", ""))
        register.acts.append(
            Act(
                sfs=number.group(1),
                title=fields.get("Rubrik", ""),
                scope=fields.get("Omfattning", ""),
                in_force=in_force,
                partly_pending=partly_pending,
                transitional="överg.best" in fields.get("Ikraft", ""),
            )
        )
    return register


@dataclass(frozen=True)
class FeedItem:
    sfs: str
    title: str
    published: date
    url: str
    statute: str  # which in-scope statute it concerns


SWEDISH_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "maj", "jun", "jul", "aug", "sep", "okt", "nov", "dec"],
        start=1,
    )
}


def _published(value: str) -> date:
    """'tis, 17 nov 2026 13:00:00 +0100' (Swedish month names) -> 2026-11-17."""
    found = re.search(r"(\d{1,2}) ([a-zåäö]{3})\w* (\d{4})", value.lower())
    if found is None or found.group(2) not in SWEDISH_MONTHS:
        raise ValueError(f"unreadable publication date: {value!r}")
    day, month, year = found.groups()
    return date(int(year), SWEDISH_MONTHS[month], int(day))


def parse_feed(content: str) -> list[FeedItem]:
    """Items of the SFS feed that amend (or bring into force amendments of) an in-scope
    statute: their titles name it, e.g. "Lag om ändring i utlänningslagen (2005:716)"."""
    items = []
    for item in re.findall(r"<item>(.*?)</item>", content, re.S):
        title = _text(_tag(item, "title"))
        statute = next((s for s in STATUTES if f"({s})" in title), None)
        sfs = _text(_tag(item, "dc:creator"))
        if statute is None or not re.fullmatch(r"\d{4}:\d+", sfs):
            continue
        items.append(
            FeedItem(
                sfs, title, _published(_tag(item, "pubDate")), _text(_tag(item, "link")), statute
            )
        )
    return items


def _tag(item: str, name: str) -> str:
    found = re.search(rf"<{name}>(.*?)</{name}>", item, re.S)
    return found.group(1) if found else ""


def describe(act: Act) -> str:
    when = act.in_force.isoformat() if act.in_force else "no single date in the register"
    return f"{act.sfs} ({when}{', partly pending' if act.partly_pending else ''})"


@dataclass(frozen=True)
class ScopeItem:
    op: str  # "upph" (repealed), "ändr" (changed), "ny" (added), "betecknas" (renumbered)
    chapter: str  # "6 a"
    first: str | None  # "16 a"; None: the whole chapter
    last: str | None  # end of a range ("16 a–18"), else the same as first


# "6 a kap." (a chapter, maybe whole) or "16 a", "16 a–18" (a section or range) before "," "och" "§"
NUM = r"\d+(?: ?[a-z])?"
SCOPE_PART = re.compile(
    rf"({NUM}) kap\.?(?=[\s,]|$)|({NUM})(?:\s*[–-]\s*({NUM}))?\s*(?=,|\s+och\s|\s*§)"
)
HEADING_ITEM = re.compile(
    r",?\s*rubr(?:iker|\.)\s+(?:närmast\s+)?(?:före|efter)\s+[^§;]*?§§?|,?\s*rubr\.[^,;]*"
)
OPS = {"upph": "upph", "ändr": "ändr", "ändring": "ändr", "ny": "ny", "nya": "ny"}


def parse_scope(scope: str) -> list[ScopeItem]:
    """The register's "Omfattning" line: which sections (or whole chapters) an act repealed,
    changed or added. "upph. 4 kap., 1 kap. 3, 6 a §§; ändr. 5 kap. 16 a–18 §§; nya 9 a kap."
    Headings ("rubr. närmast före 1 kap. 3 §") and renumberings are left out."""
    items: list[ScopeItem] = []
    for clause in scope.split(";"):
        clause = clause.strip()
        head = re.match(r"(upph|ändring|ändr|nya|ny)\.?\s+(.*)$", clause)
        if head is None:
            continue
        # drop only the heading items ("rubr. närmast före 8 kap. 21 §"); what follows counts
        op, rest = OPS[head.group(1)], HEADING_ITEM.sub(" ", head.group(2))
        if "betecknas" in rest:
            continue
        chapter: str | None = None
        for part in SCOPE_PART.finditer(rest):
            if part.group(1):
                chapter = _spaced(part.group(1))
                after = rest[part.end() : part.end() + 16]
                # "4 kap.," or "9 a kap." alone is the whole chapter; "5 kap. den nya 2 c §" not
                if not re.match(r"\s*(?:(?:den|de) nya\s+)?\d", after):
                    items.append(ScopeItem(op, chapter, None, None))
            elif chapter is not None:
                first = _spaced(part.group(2))
                items.append(ScopeItem(op, chapter, first, _spaced(part.group(3) or part.group(2))))
    return items


def _spaced(number: str) -> str:
    return re.sub(r"(\d)([a-z])", r"\1 \2", number)


def section_order(number: str) -> tuple[int, str]:
    found = re.match(r"(\d+) ?([a-z]?)", number)
    assert found is not None
    return int(found.group(1)), found.group(2)


def scope_names(item: ScopeItem, chapter: str, number: str) -> bool:
    """Whether this scope item covers that section (itself, a range, or its whole chapter)."""
    if item.chapter != chapter:
        return False
    if item.first is None:
        return True
    assert item.last is not None
    return section_order(item.first) <= section_order(number) <= section_order(item.last)
