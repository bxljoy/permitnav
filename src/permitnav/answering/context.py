"""Assembling the sources the model sees (D11, D4, D12; docs/DESIGN.md).

For each retrieved unit:
- a short wording (under ~800 tokens) is sent whole; a longer one as its best-matching chunk,
  with the heading;
- transitional provisions linked to that wording are added, and the older wording each of
  them points back to ("the older wording still applies to…"), so the model can say who is
  covered by which rule.

For a retrieved law section, the sections it refers to ("2 § andra stycket", "7 kap. 7 e §")
are added too, in the wording that applies on the date, so the model can say what the
reference requires: at most MAX_REFERENCES_PER_HIT per section and MAX_REFERENCES in all.

Transitional links are broad (every point of an amending act links to every section the act
changed). A point that names specific sections ("6 kap. 2 §") is only added for those
sections; a point that names none is kept, up to a limit.

The links are followed the other way too (phase 4, slice 4): a retrieved transitional point
that names sections ("… gäller 6 kap. 2 § första stycket 1 i den äldre lydelsen") brings
those sections, in the wording that applies on the date and in the older wording it keeps
alive. Without them the model sees "the older wording applies" with neither wording to
compare: asked about an extension registered too late for the transition, it said the
new rule applied but could not say what it requires.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql

from permitnav.corpus.chunking import estimate_tokens
from permitnav.parsing.references import cited_units, ordered_section_refs, section_refs
from permitnav.retrieval.retriever import APPLIES_ON, Hit

WHOLE_UNIT_TOKENS = 800
MAX_PROVISIONS_PER_HIT = 3
MAX_REFERENCES_PER_HIT = 2
MAX_REFERENCES = 4
MAX_NAMED_PER_PROVISION = 2  # sections a retrieved transitional point brings
MAX_NAMED = 4  # sources all retrieved points together bring (current + older wordings)
STATUTE_TYPES = ("statute",)
STOCKHOLM = ZoneInfo("Europe/Stockholm")
UNIT_SECTION = re.compile(r"^(sfs:\d{4}:\d+)/(\d+[a-z]?)kap/(\d+[a-z]?)§$")


@dataclass(frozen=True)
class Source:
    marker: str  # "S1"
    unit_id: str
    version_id: int
    role: str  # retrieved | transitional | older_wording | referenced
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
    references_left = MAX_REFERENCES
    named_left = MAX_NAMED
    hit_versions = {hit.version_id for hit in hits}  # a reference never takes a hit's place

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
        # a named section that is itself a hit comes in whole, as that hit, not shortened here
        for named in _named_sections(conn, hit, text, as_of, seen_versions | hit_versions):
            if named_left <= 0:
                break
            add(*named)
            named_left -= 1
        if references_left > 0:
            limit = min(MAX_REFERENCES_PER_HIT, references_left)
            # Only what the model sees: a long section is shown as its matching chunk.
            skip = seen_versions | hit_versions
            for reference in _references(conn, hit, text, as_of, skip)[:limit]:
                add(*reference)
                references_left -= 1
    return context


def _body(hit: Hit) -> str:
    """The matched chunk without the heading line the structure chunks start with."""
    return hit.text.split("\n\n", 1)[1] if hit.text.startswith(hit.heading) else hit.text


def _dates(authority: str, valid_from, valid_to, seen) -> str:
    if authority == "web_page":
        return f"Migrationsverket's web page as seen on {seen}"
    if authority == "ruling":
        return f"Migration Court of Appeal ruling, decided on {valid_from}"
    if authority == "position_paper":
        start = f"this part applies from {valid_from}" if valid_from else "in force"
        return f"Migrationsverket's legal position, {start}" + (
            f", until {valid_to}" if valid_to else ""
        )
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


def _named_sections(
    conn: psycopg.Connection, hit: Hit, text: str, as_of: date, skip: set[int]
) -> list[tuple]:
    """For a retrieved transitional point: the sections it names, in the order it names them,
    each in the wording that applies on `as_of` and in the older wording the point keeps
    alive, both shortened like references. Only sections the point is linked to (its act
    changed them); each is matched in its own statute's terms, so "5 kap. 8 a §
    utlänningsförordningen" counts for the ordinance. A point naming no section ("Äldre
    föreskrifter gäller …") brings nothing. Versions in `skip` (already in the context, or
    retrieved themselves) are left out. Chapterless statutes are not handled (none in scope)."""
    if "/övergång/" not in hit.unit_id:  # transitional points: 'sfs:2026:295/övergång/2'
        return []
    linked = conn.execute(
        """
        SELECT DISTINCT v.unit_id FROM unit_link l JOIN unit_version v ON v.id = l.to_version_id
        WHERE l.from_unit_id = %s AND l.kind LIKE 'transitional_%%'
        """,
        (hit.unit_id,),
    ).fetchall()
    by_ref: dict[tuple[str, str, str], str] = {}
    for (unit,) in linked:
        section = _section_of(unit)
        if section is not None:
            by_ref[section] = unit
    named_in_order: list[str] = []  # an act amends one statute: its refs come in text order
    for document in dict.fromkeys(s[0] for s in by_ref):
        for ref in ordered_section_refs(text, document):
            unit = by_ref.get((document, *ref))
            if unit and unit not in named_in_order:
                named_in_order.append(unit)
    named: list[tuple] = []
    for unit in named_in_order[:MAX_NAMED_PER_PROVISION]:
        row = conn.execute(
            sql.SQL(
                """
                SELECT v.id, u.authority, u.heading, u.url, v.text, v.valid_from, v.valid_to
                FROM unit_version v JOIN unit u ON u.id = v.unit_id
                WHERE v.unit_id = %(u)s AND {applies}
                ORDER BY v.valid_from DESC NULLS LAST, v.id DESC LIMIT 1
                """
            ).format(applies=sql.SQL(APPLIES_ON)),  # pyright: ignore[reportArgumentType]
            {"u": unit, "d": as_of},
        ).fetchone()
        if row is not None and row[0] not in skip:
            version_id, authority, heading, url, body, valid_from, valid_to = row
            dates = _dates(authority, valid_from, valid_to, None)
            named.append(
                (unit, version_id, "referenced", authority, heading, url, _shortened(body), dates)
            )
        for older in _older_wordings(conn, hit.unit_id, unit):
            if older[1] not in skip:
                named.append((*older[:6], _shortened(older[6]), older[7]))
    return named


def _references(
    conn: psycopg.Connection, hit: Hit, text: str, as_of: date, seen: set[int]
) -> list[tuple]:
    """The sections a retrieved statute section cites, in citing order, in the wording that
    applies on `as_of`; sections not stored, or already in the context, are skipped."""
    section = _section_of(hit.unit_id)
    if section is None:
        return []
    document, chapter, _ = section
    cited = [u for u in cited_units(text, document, chapter) if u != hit.unit_id]
    if not cited:
        return []
    rows = conn.execute(
        sql.SQL(
            """
            SELECT u.id, v.id, u.authority, u.heading, u.url, v.text, v.valid_from, v.valid_to
            FROM unit_version v JOIN unit u ON u.id = v.unit_id
            WHERE u.id = ANY(%(ids)s) AND u.source_type = ANY(%(types)s) AND {applies}
            ORDER BY u.id, v.valid_from DESC NULLS LAST, v.id DESC
            """
        ).format(applies=sql.SQL(APPLIES_ON)),  # pyright: ignore[reportArgumentType]
        {"ids": cited, "types": list(STATUTE_TYPES), "d": as_of},
    ).fetchall()
    by_unit: dict[str, tuple] = {}
    for row in rows:
        by_unit.setdefault(row[0], row)  # the newest wording that applies
    references = []
    for unit in cited:
        row = by_unit.get(unit)
        if row is None or row[1] in seen:
            continue
        unit_id, version_id, authority, heading, url, body, valid_from, valid_to = row
        references.append(
            (
                unit_id,
                version_id,
                "referenced",
                authority,
                heading,
                url,
                _shortened(body),
                _dates(authority, valid_from, valid_to, None),
            )
        )
    return references


def _shortened(text: str) -> str:
    """A referenced section is context, not the answer: long ones are cut at a paragraph."""
    if estimate_tokens(text) <= WHOLE_UNIT_TOKENS:
        return text
    kept: list[str] = []
    for paragraph in text.split("\n\n"):
        if kept and estimate_tokens("\n\n".join([*kept, paragraph])) > WHOLE_UNIT_TOKENS:
            break
        kept.append(paragraph)
    shortened = "\n\n".join(kept)
    limit = WHOLE_UNIT_TOKENS * 4
    if len(shortened) > limit:  # one huge paragraph: cut at a line or sentence end
        cut = max(shortened.rfind("\n", 0, limit), shortened.rfind(". ", 0, limit) + 1)
        shortened = shortened[: cut if cut > 0 else limit].rstrip()
    return shortened + " […]"


def _section_of(unit_id: str) -> tuple[str, str, str] | None:
    """("sfs:2005:716", "6", "2 a") for 'sfs:2005:716/6kap/2a§', in the written form."""
    match = UNIT_SECTION.search(unit_id)
    if not match:
        return None

    def spaced(n: str) -> str:
        return re.sub(r"(\d)([a-z])", r"\1 \2", n)

    return match[1], spaced(match[2]), spaced(match[3])
