"""Migration Court of Appeal rulings (MIG) from the courts' open data API (D2, D7).

One ruling is one citable unit, `mig:2017:24`. It is dated by its decision date (a ruling
applies from the day it was decided; it does not expire, but it interpreted the wording of
the law on that date, D7) and links to the sections of utlänningslagen and
utlänningsförordningen it cites.

Only rulings in the service's scope (D1) are kept: those citing the sections on work
permits, family ties, permanent residence or long-term residents, and none on protection,
exceptional grounds, track change after asylum (spårbyte), enforcement or studies. Rulings
decided before the 2009 labour-migration reform are left out: they interpret work-permit
rules that no longer exist.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from datetime import date

from permitnav.parsing.references import cited_units
from permitnav.parsing.types import ParsedUnit

DETAIL_URL = "https://rattspraxis.etjanst.domstol.se/sok/detaljsida/{id}"
EARLIEST = date(2009, 1, 1)  # the 2008 labour-migration reform took effect 2008-12-15

STATUTES = {
    "2005:716": ("sfs:2005:716", "utlänningslagen"),
    "2006:97": ("sfs:2006:97", "utlänningsförordningen"),
}
STATUTE_NAME = re.compile(
    r"\s*\b(?:utlänningslagen|utlänningsförordningen)\b\s*(?:\(\d{4}:\d+\))?", re.I
)

IN_SCOPE = re.compile(
    r"^sfs:2005:716/("
    r"5kap/(3[a-l]?|5|7|7a|8|8a|10|10a|17|17a|18)§"  # family ties, PUT, work-based permits
    r"|5akap/|6kap/|6akap/|6bkap/"  # long-term residents, work permits, EU Blue Card, ICT
    r"|7kap/(3|7|7[a-f])§"  # revocation
    r")"
    r"|^sfs:2006:97/(4kap/4[a-f]?§|5kap/|6kap/)"  # maintenance, work-permit exemptions
)
OUT_OF_SCOPE = re.compile(
    r"^sfs:2005:716/(4kap/|5kap/(1|1[a-c]|6|15a)§|12kap/)"  # protection, ömmande, spårbyte
    r"|^sfs:2006:97/4kap/5a?§"  # studies
)
STUDIES_ONLY = re.compile(r"^sfs:2005:716/5kap/10§$")  # temporary permits; work only with 6 kap.


@dataclass
class Rulings:
    units: list[ParsedUnit] = field(default_factory=list)
    out_of_scope: int = 0
    too_old: int = 0
    no_reference: int = 0  # no MIG number to cite it by
    duplicates: int = 0  # a MIG number already taken by a later publication
    no_text: list[str] = field(default_factory=list)  # kept with the summary only


def parse_rulings(content: bytes) -> Rulings:
    """Parse the API's list of publications (a JSON array) into in-scope ruling units."""
    result = Rulings()
    records = json.loads(content.decode("utf-8"))
    # One record per MIG number: a re-publication (or a repeated page) keeps the latest.
    latest: dict[str, dict] = {}
    for record in records:
        for number in record.get("referatNummerLista") or []:
            if number in latest and (latest[number].get("publiceringstid") or "") > (
                record.get("publiceringstid") or ""
            ):
                continue
            latest[number] = record
    seen: set[str] = set()
    for record in records:
        references = record.get("referatNummerLista") or []
        number = next((r for r in references if re.fullmatch(r"MIG \d{4}:\d+", r)), None)
        if number is None:
            result.no_reference += 1
            continue
        decided = date.fromisoformat(record["avgorandedatum"])
        cited = cited_sections(record.get("lagrumLista") or [])
        if not in_scope(cited):
            result.out_of_scope += 1
            continue
        if decided < EARLIEST:
            result.too_old += 1
            continue
        if latest.get(number) is not record or number in seen:
            result.duplicates += 1
            continue
        seen.add(number)
        unit_id = "mig:" + number.removeprefix("MIG ")
        paragraphs = html_paragraphs(record.get("innehall") or "")
        if not paragraphs:
            result.no_text.append(number)
        result.units.append(
            ParsedUnit(
                unit_id=unit_id,
                source_type="ruling",
                authority="ruling",
                language="sv",
                document=unit_id,
                heading=f"{number} (Migrationsöverdomstolen, avgjort {decided})",
                url=DETAIL_URL.format(id=record["id"]),
                text=ruling_text(record, paragraphs),
                valid_from=decided,
                links=tuple(("cites", u) for u in cited),
            )
        )
    return result


def cited_sections(lagrum: list[dict]) -> list[str]:
    """Unit ids of the Aliens Act and Ordinance sections a ruling's lagrum list cites.

    Each entry names one statute by its SFS number, so its references are read inside that
    statute, with the statute's name removed: "6 kap. 1 § och 2 § första stycket
    utlänningslagen" cites 6 kap. 1 § and 6 kap. 2 §."""
    units: list[str] = []
    for entry in lagrum:
        statute = STATUTES.get(entry.get("sfsNummer") or "")
        if statute is None:
            continue
        document = statute[0]
        text = STATUTE_NAME.sub(" ", entry.get("referens") or "")
        for unit in cited_units(text, document, chapter=""):
            # A chapterless section with no chapter before it cannot be placed.
            if unit.startswith(document + "/") and "/kap/" not in unit and unit not in units:
                units.append(unit)
    return units


def in_scope(cited: list[str]) -> bool:
    if any(OUT_OF_SCOPE.match(u) for u in cited):
        return False
    relevant = [u for u in cited if IN_SCOPE.match(u)]
    # 5 kap. 10 § alone is about temporary permits in general, mostly studies.
    return bool(relevant) and not all(STUDIES_ONLY.match(u) for u in relevant)


def html_paragraphs(fragment: str) -> list[str]:
    """The ruling's blocks as plain text. The API gives `<p>` paragraphs, `<h3>`/`<h4>`
    headings and line breaks; inline tags (`<strong>`, `<a>`, `<i>`) are removed without
    adding space, so a word split by markup stays one word."""
    parts = re.split(r"(?i)</(?:p|h[1-6]|div|li|tr)\s*>|<br\s*/?>", fragment)
    paragraphs = []
    for part in parts:
        text = html.unescape(re.sub(r"<[^>]+>", "", part))
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            paragraphs.append(text)
    return paragraphs


def ruling_text(record: dict, paragraphs: list[str]) -> str:
    lines = [f"Sammanfattning: {_clean(record.get('sammanfattning') or '')}"]
    lagrum = "; ".join(_clean(e.get("referens", "")) for e in record.get("lagrumLista") or [])
    if lagrum:
        lines.append(f"Lagrum: {lagrum}")
    if paragraphs:
        lines += paragraphs
    else:
        lines.append("(Domskälen finns inte i källan; endast sammanfattningen.)")
    return "\n\n".join(lines)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text)).strip()
