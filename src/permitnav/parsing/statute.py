"""Parser for consolidated Swedish statutes in the riksdagen open data text format.

Input is the `.text` rendering from https://data.riksdagen.se/dokument/sfs-YYYY-NNN.text:
a header, then chapters ("6 kap. Arbetstillstånd") and sections ("2 § ..."), separated by
blank lines, and at the end the transitional provisions grouped by amending act.

What the format looks like, and how it is handled:

- Each section ends with the act that last changed it: "Lag (2026:295)." → amended_by.
- A section about to change appears twice, marked "/Upphör att gälla U:2028-01-01/" and
  "/Träder i kraft I:2028-01-01/" → two units with the same id and adjoining dates.
- Repealed sections read "N § Har upphävts genom lag (…)." → listed in `repealed`, no unit.
- Subheadings sit on their own between sections → added to the next section's heading.
- Lines inside a section may start with a reference like "2 § första stycket"; a block only
  starts a new section if its number comes after the current one in the chapter.
- Transitional provisions: a line with an SFS number ("2026:295") starts a group; numbered
  points ("2. ...") are units. Point 1 usually gives the act's in-force date, which becomes
  valid_from for the sections that act amended.

Pure function, no I/O: `parse_statute(text, url)`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date

from permitnav.parsing.types import ParsedUnit

MONTHS = {
    "januari": 1,
    "februari": 2,
    "mars": 3,
    "april": 4,
    "maj": 5,
    "juni": 6,
    "juli": 7,
    "augusti": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "december": 12,
}

# "Utlänningslag (2005:716)", or "Lag (2016:752) om tillfälliga begränsningar …" (may wrap)
TITLE = re.compile(r"^(?P<before>[^(]*)\((?P<sfs>\d{4}:\d+)\)", re.S)
AMENDED_THROUGH = re.compile(r"^Ändrad:\s*t\.o\.m\.\s*SFS\s*(?P<sfs>\d{4}:\d+)")
CHAPTER = re.compile(r"^(?P<num>\d+(?: [a-z])?) kap\. (?P<title>\D.*)$", re.S)
SECTION = re.compile(r"^(?P<num>\d+(?: [a-z])?) § (?P<rest>.*)$", re.S)
# "/Upphör att gälla U:2028-01-01/", "/Träder i kraft I:den dag som regeringen bestämmer/",
# "/Rubriken upphör att gälla U:2028-01-01/" (on subheadings), "... genom lag (2026:1687)./"
# "/Ny beteckning 10 kap. 4 § U:2027-12-01/": the section moves to a new number on that date
# (the source once spells it "betekning").
MARKER = re.compile(
    r"^/(?P<heading>Rubriken )?"
    r"(?:(?P<kind>[Uu]pphör att gälla|[Tt]räder i kraft) ?[UI]?|"
    r"Ny bete(?:c)?kning (?P<renumbered>.+?) U):"
    r"(?P<when>\d{4}-\d{2}-\d{2}|den dag som regeringen bestämmer)(?: genom [^/]*)?/\s*"
)
GOVERNMENT_DECIDES = "den dag som regeringen bestämmer"
REPEALED = re.compile(r"^(?:Har upphävts|Upphävd) genom (?:lag|förordning) \((?P<sfs>\d{4}:\d+)\)")
RENUMBERED = re.compile(r"^Ny beteckning .* genom (?:lag|förordning) \((?P<sfs>\d{4}:\d+)\)")
AMENDMENT = re.compile(r"(?:Lag|Förordning) \((?P<sfs>\d{4}:\d+)\)\.?\s*$")
TRANSITIONAL_HEADER = "Övergångsbestämmelser"
SFS_LINE = re.compile(r"^(?P<sfs>\d{4}:\d+)$")
POINT = re.compile(r"^(?P<num>\d+)\. (?P<rest>.*)$", re.S)
IN_FORCE = re.compile(
    r"^Denna (?:lag|förordning) träder i kraft den (?P<day>\d{1,2}) (?P<month>[a-zåäö]+) "
    r"(?P<year>\d{4})\.(?:\s|$)"  # a plain date; split or undecided commencements don't match
)
END_PUNCTUATION = (".", ":", ";", ",", ")")


@dataclass
class Statute:
    document: str  # "sfs:2005:716"
    title: str  # "Utlänningslag (2005:716)"
    authority: str  # law | ordinance
    amended_through: str | None  # last amending act included in the consolidation
    units: list[ParsedUnit] = field(default_factory=list)
    repealed: dict[str, str] = field(default_factory=dict)  # unit_id → repealing act
    in_force: dict[str, date] = field(default_factory=dict)  # amending act → in-force date
    warnings: list[str] = field(default_factory=list)  # text the parser could not place
    # Wordings dropped because the source gives the same unit two wordings from the same
    # day; the later one in the text is kept. Reported, not fatal.
    conflicts: list[str] = field(default_factory=list)


def parse_statute(text: str, url: str) -> Statute:
    blocks = _blocks(text)
    if not blocks:
        raise ValueError("empty statute text")
    title_match = TITLE.match(blocks[0])
    if not title_match:
        raise ValueError(f"first line is not a statute title: {blocks[0][:80]!r}")
    sfs = title_match["sfs"]
    title = _one_line(blocks[0])
    # The kind of statute is the word before the number: "Utlänningsförordning (2006:97)".
    # A law "… med kompletterande bestämmelser till EU:s förordning …" is still a law.
    authority = (
        "ordinance" if title_match["before"].strip().lower().endswith("förordning") else "law"
    )
    statute = Statute(
        document=f"sfs:{sfs}",
        title=title,
        authority=authority,
        amended_through=_amended_through(blocks),
    )
    body = _skip_header(blocks)
    try:
        split = body.index(TRANSITIONAL_HEADER)
    except ValueError:
        split = len(body)
    _parse_sections(statute, body[:split], url)
    _parse_transitional(statute, body[split + 1 :], url)
    _apply_in_force_dates(statute)
    _drop_same_day_duplicates(statute)
    live = {unit.unit_id for unit in statute.units}
    statute.repealed = {k: v for k, v in statute.repealed.items() if k not in live}
    return statute


# --- blocks -----------------------------------------------------------------------------


def _blocks(text: str) -> list[str]:
    """Split into blank-line separated blocks, with trailing spaces stripped per line."""
    blocks, current = [], []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line:
            current.append(line)
        elif current:
            blocks.append("\n".join(current))
            current = []
    if current:
        blocks.append("\n".join(current))
    return blocks


def _amended_through(blocks: list[str]) -> str | None:
    for block in blocks[:3]:
        for line in block.splitlines():
            if match := AMENDED_THROUGH.match(line):
                return match["sfs"]
    return None


def _skip_header(blocks: list[str]) -> list[str]:
    """Drop the title and metadata blocks: everything before the first chapter or section."""
    for i, block in enumerate(blocks[1:], start=1):
        if CHAPTER.match(block) or SECTION.match(block):
            return blocks[i:]
    return []


def _one_line(block: str) -> str:
    return " ".join(block.split())


# --- sections ---------------------------------------------------------------------------


@dataclass
class _Section:
    chapter: str | None
    chapter_title: str | None
    subheading: str | None
    number: str
    paragraphs: list[str]
    valid_from: date | None = None
    valid_to: date | None = None
    pending: str | None = None


@dataclass(frozen=True)
class _Marker:
    ends: bool  # True: "upphör att gälla", False: "träder i kraft"
    when: date | None  # None: on a day the government decides
    on_heading: bool
    end: int  # where the marked text starts


def _marker(text: str) -> _Marker | None:
    match = MARKER.match(text)
    if not match:
        return None
    when = None if match["when"] == GOVERNMENT_DECIDES else date.fromisoformat(match["when"])
    return _Marker(
        ends=bool(match["renumbered"]) or match["kind"].lower().startswith("upphör"),
        when=when,
        on_heading=bool(match["heading"]),
        end=match.end(),
    )


def _section_key(number: str) -> tuple[int, str]:
    digits, _, letter = number.partition(" ")
    return int(digits), letter


def _compact(number: str) -> str:
    return number.replace(" ", "")


def _parse_sections(statute: Statute, blocks: list[str], url: str) -> None:
    chapter: str | None = None
    chapter_title: str | None = None
    pending_subheading: list[str] = []
    current: _Section | None = None
    last_key: tuple[int, str] | None = None
    last_was_repealed = False
    last_subheading: list[str] = []  # shared by the expiring and upcoming wordings

    def finish() -> None:
        nonlocal current, last_was_repealed
        if current is not None:
            last_was_repealed = _is_repealed(current)
            _emit_section(statute, current, url)
        current = None

    for i, block in enumerate(blocks):
        if (chapter_match := CHAPTER.match(block)) and _starts_new_chapter(chapter_match, chapter):
            finish()
            chapter = chapter_match["num"]
            chapter_title = _one_line(chapter_match["title"])
            pending_subheading = []
            last_subheading = []
            last_key = None
            continue

        section_match = SECTION.match(block)
        if section_match and _starts_new_section(
            section_match, last_key, previous_repealed=_is_repealed(current) or last_was_repealed
        ):
            finish()
            number = section_match["num"]
            rest = section_match["rest"]
            valid_from = valid_to = None
            upcoming = start_undated = end_undated = False
            while marker := _marker(rest):  # a section can carry more than one marker
                rest = rest[marker.end :]
                if marker.ends:
                    valid_to = marker.when
                    end_undated = marker.when is None
                else:
                    upcoming = True
                    valid_from = marker.when
                    start_undated = marker.when is None
            # Not in force until the government decides wins over any end date.
            pending = "starts" if start_undated else "ends" if end_undated else None
            if not pending_subheading and last_key == _section_key(number):
                pending_subheading = last_subheading  # second wording of the same section
            current = _Section(
                chapter=chapter,
                chapter_title=chapter_title,
                subheading=_subheading(pending_subheading, upcoming),
                number=number,
                paragraphs=[rest],
                valid_from=valid_from,
                valid_to=valid_to,
                pending=pending,
            )
            last_subheading = pending_subheading
            pending_subheading = []
            last_key = _section_key(number)
            last_was_repealed = False
            continue

        if _is_subheading(blocks, i, last_key, last_was_repealed or _is_repealed(current)):
            finish()
            pending_subheading.append(block)
            continue

        if current is None:
            statute.warnings.append(f"text outside any section: {block[:60]!r}")
            continue
        current.paragraphs.append(block)

    finish()


def _starts_new_section(
    match: re.Match[str], last_key: tuple[int, str] | None, previous_repealed: bool
) -> bool:
    """Is a block starting "N §" a new section, or a paragraph that begins with a reference?

    - Real sections start with a capital letter or a marker ("2 § Arbetstillstånd får …",
      "4 § /Träder i kraft …/"); references continue in lower case ("4 § gäller också …").
    - The number must come after the current one. The same number again is allowed for
      another marked wording of the section (in either order), and after a repealed section
      of that number (a later act re-used the number).
    """
    rest = match["rest"]
    if not rest or not (rest[0].isupper() or rest.startswith("/")):
        return False
    if last_key is None:
        return True
    key = _section_key(match["num"])
    if key > last_key:
        return True
    return key == last_key and (_marker(rest) is not None or previous_repealed)


def _starts_new_chapter(match: re.Match[str], current: str | None) -> bool:
    """Chapter titles start with a capital ("8 kap. Avvisning …"), and chapter numbers rise;
    a paragraph like "8 kap. gäller också …" is a reference."""
    if not match["title"][0].isupper():
        return False
    return current is None or _section_key(match["num"]) > _section_key(current)


def _is_repealed(section: _Section | None) -> bool:
    if section is None:
        return False
    first = _one_line(section.paragraphs[0])
    return bool(REPEALED.match(first) or RENUMBERED.match(first))


def _is_subheading(
    blocks: list[str], i: int, last_key: tuple[int, str] | None, previous_repealed: bool
) -> bool:
    """A subheading is one or more unpunctuated lines that lead straight into a new section.

    Paragraphs such as "Första stycket gäller inte om" also end without punctuation, but
    they lead into a list ("1. ...", "- ...", "a) ..."), not into a section or chapter.
    Block i itself has already failed the section and chapter checks.
    """
    for j, block in enumerate(blocks[i:], start=i):
        if j > i:
            if (match := CHAPTER.match(block)) and match["title"][0].isupper():
                return True
            if (match := SECTION.match(block)) and _starts_new_section(
                match, last_key, previous_repealed
            ):
                return True
        if block.startswith("/Rubriken"):
            continue
        if (
            block.endswith(END_PUNCTUATION)
            or not block[0].isupper()
            or block.startswith("-")
            or POINT.match(block)
        ):
            return False
    return False


def _emit_section(statute: Statute, section: _Section, url: str) -> None:
    unit_id = _unit_id(statute.document, section.chapter, section.number)
    if section.valid_from and section.valid_to and section.valid_from >= section.valid_to:
        return  # starts and ends on the same day: never in force (an artefact of the text)
    text = "\n\n".join(_one_line_paragraph(p) for p in section.paragraphs).strip()

    if repealed := (REPEALED.match(text) or RENUMBERED.match(text)):
        statute.repealed[unit_id] = repealed["sfs"]
        return

    amendment = AMENDMENT.search(text)
    heading_parts = []
    if section.chapter is not None:
        heading_parts.append(f"{section.chapter} kap. {section.chapter_title}")
    if section.subheading:
        heading_parts.append(section.subheading)
    heading_parts.append(f"{section.number} §")

    statute.units.append(
        ParsedUnit(
            unit_id=unit_id,
            source_type="statute",
            authority=statute.authority,
            language="sv",
            document=statute.document,
            heading=", ".join(heading_parts),
            url=url,
            text=text,
            valid_from=section.valid_from,
            valid_to=section.valid_to,
            amended_by=amendment["sfs"] if amendment else None,
            pending=section.pending,
        )
    )


def _subheading(lines: list[str], upcoming: bool) -> str | None:
    """Join the subheading lines before a section, choosing the matching marked variant.

    A subheading about to change appears twice ("/Rubriken upphör att gälla …/" and
    "/Rubriken träder i kraft …/"); the upcoming wording of a section gets the new one.
    """
    plain, ending, starting = [], [], []
    for line in lines:
        marker = _marker(line)
        if marker is None:
            plain.append(line)
        elif marker.ends:
            ending.append(line[marker.end :])
        else:
            starting.append(line[marker.end :])
    chosen = plain + (starting if upcoming else ending)  # a removed subheading stays removed
    return _one_line(" ".join(chosen)) if chosen else None


def _one_line_paragraph(paragraph: str) -> str:
    """Join the hard-wrapped lines of one paragraph, keeping list items on their own lines."""
    lines: list[str] = []
    for line in paragraph.splitlines():
        if line.startswith("- ") or not lines:
            lines.append(line.strip())
        else:
            lines[-1] = f"{lines[-1]} {line.strip()}"
    return "\n".join(lines)


def _unit_id(document: str, chapter: str | None, number: str) -> str:
    if chapter is None:
        return f"{document}/{_compact(number)}§"
    return f"{document}/{_compact(chapter)}kap/{_compact(number)}§"


# --- transitional provisions --------------------------------------------------------------


def _parse_transitional(statute: Statute, blocks: list[str], url: str) -> None:
    act: str | None = None
    points: list[tuple[str, list[str]]] = []

    def finish() -> None:
        if act is None:
            return
        for number, paragraphs in points:
            first = paragraphs[0]
            valid_from = valid_to = None
            while marker := _marker(first):  # a point can have expiring and upcoming wordings
                first = first[marker.end :]
                if marker.ends:
                    valid_to = marker.when
                else:
                    valid_from = marker.when
            paragraphs = [first, *paragraphs[1:]]
            text = "\n\n".join(_one_line_paragraph(p) for p in paragraphs).strip()
            if number == "1" and (match := IN_FORCE.match(_one_line(paragraphs[0]))):
                month = MONTHS.get(match["month"])
                if month:
                    statute.in_force[act] = date(int(match["year"]), month, int(match["day"]))
            statute.units.append(
                ParsedUnit(
                    unit_id=f"sfs:{act}/övergång/{number}",
                    source_type="transitional",
                    authority=statute.authority,
                    language="sv",
                    document=f"sfs:{act}",
                    heading=f"Övergångsbestämmelser till SFS {act}, punkt {number}",
                    url=url,
                    text=text,
                    valid_from=valid_from,
                    valid_to=valid_to,
                )
            )

    for block in blocks:
        if sfs_match := SFS_LINE.match(block):
            finish()
            act = sfs_match["sfs"]
            points = []
        elif act is None:
            statute.warnings.append(f"transitional text before any act: {block[:60]!r}")
        elif (point := POINT.match(block)) and (
            not points
            or int(point["num"]) == int(points[-1][0]) + 1
            # the same point again, as another marked wording
            or (point["num"] == points[-1][0] and _marker(point["rest"]) is not None)
        ):
            points.append((point["num"], [point["rest"]]))  # the next point
        # otherwise a numbered list inside the current point: falls through to append
        elif points:
            points[-1][1].append(block)
        else:
            points.append(("1", [block]))  # an act with a single, unnumbered provision
    finish()


def _apply_in_force_dates(statute: Statute) -> None:
    """Fill valid_from from the amending act's in-force date, and link transitional points."""
    amended: dict[str, list[str]] = {}
    for i, unit in enumerate(statute.units):
        if unit.source_type != "statute" or unit.amended_by is None:
            continue
        amended.setdefault(unit.amended_by, []).append(unit.unit_id)
        start = statute.in_force.get(unit.amended_by)
        if (
            unit.pending != "starts"
            and unit.valid_from is None
            and start is not None
            and (unit.valid_to is None or start < unit.valid_to)
        ):
            statute.units[i] = replace(unit, valid_from=start)

    for i, unit in enumerate(statute.units):
        if unit.source_type != "transitional":
            continue
        act = unit.document.removeprefix("sfs:")
        targets = dict.fromkeys(amended.get(act, []))  # ordered, no duplicates
        start = unit.valid_from or statute.in_force.get(act)
        if unit.valid_to is not None and start is not None and start >= unit.valid_to:
            start = None
        statute.units[i] = replace(
            unit,
            valid_from=start,
            links=tuple(("transitional_for", target) for target in targets),
        )


def _drop_same_day_duplicates(statute: Statute) -> None:
    """Keep only the last of several wordings of one unit that start on the same day."""
    last_index: dict[tuple[str, date | None, str | None], int] = {}
    for i, unit in enumerate(statute.units):
        if unit.source_type == "statute":
            last_index[(unit.unit_id, unit.valid_from, unit.pending)] = i
    kept = []
    for i, unit in enumerate(statute.units):
        key = (unit.unit_id, unit.valid_from, unit.pending)
        if unit.source_type == "statute" and last_index[key] != i:
            statute.conflicts.append(
                f"{unit.unit_id}: two wordings from {unit.valid_from or 'an unknown date'}; "
                "kept the later one in the text"
            )
            continue
        kept.append(unit)
    statute.units = kept
