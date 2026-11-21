"""Assembling the sources the model sees (D11, D4, D12; docs/DESIGN.md).

For each retrieved unit:
- a short wording (under ~800 tokens) is sent whole; a longer one as its best-matching chunk,
  with the heading;
- transitional provisions linked to that wording are added, and the older wording each of
  them points back to ("the older wording still applies to…"), so the model can say who is
  covered by which rule.

Transitional links are broad (every point of an amending act links to every section the act
changed). A point that names specific sections ("6 kap. 2 §") is only added for those
sections; a point that names none is kept, up to a limit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql

from permitnav.corpus.chunking import estimate_tokens
from permitnav.retrieval.retriever import APPLIES_ON, Hit

WHOLE_UNIT_TOKENS = 800
MAX_PROVISIONS_PER_HIT = 3
STOCKHOLM = ZoneInfo("Europe/Stockholm")
# "6 kap. 2 §", "6 kap. 2 och 3 §§", "6 kap. 2, 2 a och 3 §§", "10 kap. 1–17 §§"
SECTION_LIST = re.compile(
    r"\b(\d+(?: [a-z])?) kap\. ((?:\d+(?: [a-z])?)(?:\s*(?:,|och|–|-)\s*\d+(?: [a-z])?)*)\s*(§§?)"
)
LIST_ITEM = re.compile(r"(\d+)(?: ([a-z])(?![a-zåäö]))?")  # "2 a", but not the "o" of "2 och"
# A reference followed by another statute's name is to that statute, not to this one.
STATUTE_NAMES = {
    "sfs:2005:716": ("utlänningslagen", "2005:716"),
    "sfs:2006:97": ("utlänningsförordningen", "2006:97"),
}
OTHER_STATUTE = re.compile(
    r"\s*(?:första|andra|tredje|fjärde|femte|stycket|punkt|\d|\s)*(lagen|förordningen|\(\d{4}:\d+\)|[a-zåäö]+lagen|[a-zåäö]+förordningen)"
)
UNIT_SECTION = re.compile(r"^(sfs:\d{4}:\d+)/(\d+[a-z]?)kap/(\d+[a-z]?)§$")


@dataclass(frozen=True)
class Source:
    marker: str  # "S1"
    unit_id: str
    version_id: int
    role: str  # retrieved | transitional | older_wording
    authority: str
    heading: str
    url: str
    text: str
    dates: str  # human-readable validity, e.g. "applies from 2026-06-01"


@dataclass
class Context:
    sources: list[Source] = field(default_factory=list)

    def by_marker(self) -> dict[str, Source]:
        return {s.marker: s for s in self.sources}

    def render(self) -> str:
        blocks = []
        for s in self.sources:
            blocks.append(
                f"[{s.marker}] {s.heading}\n"
                f"type: {s.authority} | {s.role} | {s.dates}\n"
                f"id: {s.unit_id}\n"
                f"{s.text}"
            )
        return "\n\n---\n\n".join(blocks)


def build_context(conn: psycopg.Connection, hits: list[Hit], as_of: date) -> Context:
    """Sources for the model. Transitional provisions must apply on `as_of` themselves; the
    older wordings they point to are, by definition, ones that ended, but never withdrawn
    or not-yet-dated ones."""
    context = Context()
    seen_versions: set[int] = set()

    def add(unit_id, version_id, role, authority, heading, url, text, dates) -> None:
        if version_id in seen_versions:
            return
        seen_versions.add(version_id)
        marker = f"S{len(context.sources) + 1}"
        context.sources.append(
            Source(marker, unit_id, version_id, role, authority, heading, url, text, dates)
        )

    for hit in hits:
        full = conn.execute(
            "SELECT text FROM unit_version WHERE id = %s", (hit.version_id,)
        ).fetchone()
        text = full[0] if full and estimate_tokens(full[0]) <= WHOLE_UNIT_TOKENS else _body(hit)
        add(
            hit.unit_id,
            hit.version_id,
            "retrieved",
            hit.authority,
            hit.heading,
            hit.url,
            text,
            _dates(hit.authority, hit.valid_from, hit.valid_to, _stockholm_date(hit.fetched_at)),
        )
        for provision in _provisions(conn, hit, as_of):
            add(*provision)
            for older in _older_wordings(conn, provision[0], hit.unit_id):
                add(*older)
    return context


def _body(hit: Hit) -> str:
    """The matched chunk without the heading line the structure chunks start with."""
    return hit.text.split("\n\n", 1)[1] if hit.text.startswith(hit.heading) else hit.text


def _dates(authority: str, valid_from, valid_to, seen) -> str:
    if authority == "web_page":
        return f"Migrationsverket's web page as seen on {seen}"
    start = f"applies from {valid_from}" if valid_from else "in force (start date not recorded)"
    return f"{start}, until {valid_to}" if valid_to else start


def _provisions(conn: psycopg.Connection, hit: Hit, as_of: date) -> list[tuple]:
    rows = conn.execute(
        sql.SQL(
            """
            SELECT DISTINCT l.from_unit_id, l.kind, v.id, u.authority, u.heading, u.url, v.text,
                   v.valid_from, v.valid_to, u.document
            FROM unit_link l
            JOIN unit_version v ON v.unit_id = l.from_unit_id
            JOIN unit u ON u.id = l.from_unit_id
            WHERE l.to_version_id = %(version)s AND l.kind LIKE 'transitional_%%' AND {applies}
            """
        ).format(applies=sql.SQL(APPLIES_ON)),  # pyright: ignore[reportArgumentType]
        {"version": hit.version_id, "d": as_of},
    ).fetchall()
    target = _section_of(hit.unit_id)
    named, general = [], []
    for unit_id, kind, version_id, authority, heading, url, text, valid_from, valid_to, act in rows:
        refs = section_refs(text, target[0] if target else "")
        if refs and (target is None or target[1:] not in refs):
            continue  # it names other sections, not this one
        dates = _dates(authority, valid_from, valid_to, None)
        record = (unit_id, version_id, "transitional", authority, heading, url, text, dates)
        order = (kind != "transitional_introduced", _act_order(act), _point_number(unit_id))
        (named if refs else general).append((order, record))
    chosen = sorted(named, key=lambda x: x[0]) + sorted(general, key=lambda x: x[0])
    return [record for _, record in chosen][:MAX_PROVISIONS_PER_HIT]


def section_refs(text: str, document: str) -> set[tuple[str, str]]:
    """(chapter, section) pairs a provision names in `document`, e.g. {("6", "2"), ("6", "3")}
    for "6 kap. 2 och 3 §§". Ranges are expanded; references followed by another statute's
    name are left out."""
    own = STATUTE_NAMES.get(document, ())
    found: set[tuple[str, str]] = set()
    for match in SECTION_LIST.finditer(text):
        after = OTHER_STATUTE.match(text, match.end())
        if after and not any(name in after.group(1) for name in own):
            continue  # "8 kap. 2 § utlänningsförordningen" from a law's provision
        chapter = match[1]
        items = LIST_ITEM.findall(match[2])
        separators = re.findall(r"(,|och|–|-)", match[2])
        sections: list[str] = []
        for i, (number, letter) in enumerate(items):
            if i > 0 and separators[i - 1] in ("–", "-") and not letter and items[i - 1][1] == "":
                start = int(items[i - 1][0])
                sections += [str(n) for n in range(start + 1, int(number) + 1)]
            else:
                sections.append(f"{number} {letter}" if letter else number)
        found |= {(chapter, section) for section in sections}
    return found


def _stockholm_date(moment: datetime) -> date:
    return moment.astimezone(STOCKHOLM).date()


def _act_order(document: str) -> tuple[int, int]:
    """Newer amending acts first."""
    match = re.search(r"(\d{4}):(\d+)", document)
    return (-int(match[1]), -int(match[2])) if match else (0, 0)


def _point_number(unit_id: str) -> int:
    match = re.search(r"/(\d+)$", unit_id)
    return int(match[1]) if match else 0


def _older_wordings(conn: psycopg.Connection, provision_unit: str, unit_id: str) -> list[tuple]:
    rows = conn.execute(
        """
        SELECT ov.id, u.authority, u.heading, u.url, ov.text, ov.valid_from, ov.valid_to
        FROM unit_link l
        JOIN unit_version ov ON ov.id = l.to_version_id
        JOIN unit u ON u.id = ov.unit_id
        WHERE l.from_unit_id = %s AND l.kind = 'transitional_ended' AND ov.unit_id = %s
          AND NOT ov.withdrawn AND ov.pending IS DISTINCT FROM 'starts'
        """,
        (provision_unit, unit_id),
    ).fetchall()
    return [
        (
            unit_id,
            vid,
            "older_wording",
            auth,
            f"{heading} (older wording)",
            url,
            text,
            _dates(auth, vf, vt, None),
        )
        for vid, auth, heading, url, text, vf, vt in rows
    ]


def _section_of(unit_id: str) -> tuple[str, str, str] | None:
    """("sfs:2005:716", "6", "2 a") for 'sfs:2005:716/6kap/2a§', in the written form."""
    match = UNIT_SECTION.search(unit_id)
    if not match:
        return None

    def spaced(n: str) -> str:
        return re.sub(r"(\d)([a-z])", r"\1 \2", n)

    return match[1], spaced(match[2]), spaced(match[3])
