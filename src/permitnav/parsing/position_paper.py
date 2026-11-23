"""A Migrationsverket legal position paper (rättsligt ställningstagande) as a PDF (slice 5).

Layout (StyrD5 template), as read by pypdf:
    page 1: the decision ("Gäller från och med: 2021-07-20"), a revision table
            ("2026-07-12 8.0 2, 2.1, 2.2, 3.2 och 4 RA096/2026": date, version, the sections
            revised), the RS number and title;
    page 2: a table of contents ("2.1.3 Krav på tidsbegränsat … .......... 7");
    body:   numbered headings ("2.1 Permanent uppehållstillstånd där …", sometimes wrapped),
            text, footnotes at each page's foot; page furniture ("R Ä T T S L I G T …").

Units: one per numbered section, its own text up to the next numbered heading (D2):
'rs:RS/084/2021/2.1.3'. A section's wording applies from the last revision that changed it
or a section above it ("2" revised: 2.1.3 too, conservatively), else from the paper's own
start ("Gäller från och med"). Footnotes are left out: they hold references, not the position.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from permitnav.parsing.sfs_act import _join
from permitnav.parsing.types import ParsedUnit

NUMBER = r"\d+(?:\.\d+)*"
FURNITURE = re.compile(
    r"^(\d{1,3}|(?:R Ä T T S L I G T\s+S T Ä L L N I N G S T A G A N D E)?"
    r"(?:\s*(?:I N S T R U K T I O N|Migrationsverket))*"
    r"|StyrD\d+ \d{4}-\d{2}-\d{2}|Sida \d+ av \d+)\s*$"
)
TOC_LINE = re.compile(rf"^(?:({NUMBER})\.?\s+)?(\S.+?)\s*\.{{2,}}\s*\d+\s*$")
TEMPLATE_ENTRIES = ("fakta om rättsliga ställningstaganden", "innehållsförteckning")
REVISION = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+(\d+\.\d+)\s+(.+?)\s+RA/?\s?\d+/\d{4}\s*$")


@dataclass
class Paper:
    rs: str  # "RS/084/2021"
    title: str  # "Särskilda krav för att beviljas permanent uppehållstillstånd"
    version: str  # "8.0"
    in_force: date | None  # "Gäller från och med", the first version's start
    revisions: list[tuple[date, str, tuple[str, ...]]] = field(default_factory=list)
    sections: list[tuple[str, str, str]] = field(default_factory=list)  # (number, title, text)
    warnings: list[str] = field(default_factory=list)


def parse_paper(pages: list[str], rs: str, title: str, version: str) -> Paper:
    """The paper's sections and dates from its PDF text; `rs`, `title` and `version` come from
    its Lifos page (the PDF repeats them, in a less regular form)."""
    paper = Paper(rs, title, version, None)
    first = "\n".join(pages[:2])
    start = re.search(r"Gäller från och med:\s*(\d{4}-\d{2}-\d{2})", first)
    paper.in_force = date.fromisoformat(start.group(1)) if start else None
    for line in first.splitlines():
        found = REVISION.match(line.strip())
        if found:
            when, number, revised = found.groups()
            parts = re.split(r",\s*|\s+och\s+", revised.strip())
            paper.revisions.append((date.fromisoformat(when), number, tuple(p for p in parts if p)))
    toc = _table_of_contents(pages)
    if not toc:
        paper.warnings.append("no table of contents found")
        return paper
    body = _body_lines(pages, toc)
    paper.sections = _split(body, toc, paper.warnings)
    return paper


def _table_of_contents(pages: list[str]) -> list[tuple[str, str]]:
    """(number, title) per heading in the contents. Titles can wrap over two lines; older
    papers have unnumbered headings ("Sammanfattning ...... 3"): they are numbered 1, 2, 3 …
    in contents order. The template's own box ("Fakta om …") is not a section."""
    entries: list[tuple[str | None, str]] = []
    pending = ""
    for page in pages[:4]:
        for raw in page.splitlines():
            line = raw.strip()
            if not line:
                continue
            found = TOC_LINE.match(f"{pending} {line}".strip() if pending else line)
            if found:
                title = re.sub(r"\s+", " ", found.group(2))
                if title.lower() not in TEMPLATE_ENTRIES:
                    entries.append((found.group(1), title))
                pending = ""
            elif re.match(rf"^{NUMBER}\.?\s+\S", line) and "...." not in line:
                pending = line  # a numbered title that continues on the next line
            elif pending:
                pending = ""
    if entries and all(number is None for number, _ in entries):
        return [(str(i), title) for i, (_, title) in enumerate(entries, start=1)]
    return [(number, title) for number, title in entries if number is not None]


def _body_lines(pages: list[str], toc: list[tuple[str, str]]) -> list[str]:
    """Body text without page furniture and footnotes, from the first heading after the
    contents (the contents repeat every heading, with dot leaders)."""
    lines: list[str] = []
    next_note = 1
    for page in pages:
        page_lines = [ln.rstrip() for ln in page.splitlines()]
        page_lines = [ln for ln in page_lines if ln.strip() and not FURNITURE.match(ln.strip())]
        cut, next_note = _footnote_block(page_lines, next_note, [t for _, t in toc])
        lines += page_lines[:cut]
    contents_end = max((i for i, ln in enumerate(lines) if "...." in ln), default=-1)
    _, first_title = toc[0]
    for i in range(contents_end + 1, len(lines)):
        if _heading_number(lines[i], first_title) is not None:
            return lines[i:]
    return []


# How far the footnote count may have to jump (a block missed on an earlier page): the
# glued mark is strong evidence, so the window can be generous.
RESYNC = 10


def _footnote_block(lines: list[str], next_note: int, titles: list[str]) -> tuple[int, int]:
    """Where the page's footnotes begin, and the next footnote number after them.

    Footnotes are numbered through the paper. A footnote line starts with its number, its
    text sometimes glued on ("14Ändringen …") or starting with a digit ("8 5 kap. 3 g §");
    its mark comes earlier on the page, glued to a word or punctuation ("utlänningslagen.6",
    "skyddet,12", "utsatta:16"): after a letter, perhaps one punctuation mark between. After
    a digit it is part of a number, not a mark ("MIG 2018:5")."""
    for i, line in enumerate(lines):
        found = re.match(r"^(\d{1,3})\s*\S", line.strip())
        if not found:
            continue
        number = int(found.group(1))
        if not next_note - 2 <= number <= next_note + RESYNC:
            continue
        if any(_heading_number(line, title) is not None for title in titles):
            continue  # "2 Gällande rätt" is a heading, not footnote 2
        before = "\n".join(lines[:i])
        after_word = rf"[A-Za-zÅÄÖåäöé][.,;:)”’]?{number}(?!\d)"
        after_number = rf"\d[.)]{{1,2}}{number}(?=\s|$)"  # "(1999:1229).35", "2028.21"
        if re.search(after_word, before) or re.search(after_number, before, re.M):
            last = number
            for later in lines[i + 1 :]:
                more = re.match(r"^(\d{1,3})\s*\S", later.strip())
                if more and int(more.group(1)) == last + 1:
                    last += 1
            return i, last + 1
    return len(lines), next_note


def _heading_number(line: str, title: str) -> str | None:
    """The line's own section number ("" if it has none) when it is the heading `title`,
    else None. A numbered line needs only the title's first words (it can wrap); an
    unnumbered one must be the title itself, so a sentence starting "Syfte …" is not one."""
    stripped = line.strip()
    found = re.match(rf"^({NUMBER})\.?\s+(.*)$", stripped)
    if found:
        words = title.split()[:3]
        rest = found.group(2)
        if rest.split()[: len(words)] == words or (len(rest) > 3 and title.startswith(rest)):
            return found.group(1)
        return None
    return "" if stripped == title or (len(stripped) > 12 and title.startswith(stripped)) else None


def _split(
    lines: list[str], toc: list[tuple[str, str]], warnings: list[str]
) -> list[tuple[str, str, str]]:
    """Each contents heading, found by its title in contents order; the body's number wins
    over the contents' (a contents list is sometimes not updated after a revision)."""
    sections: list[tuple[str, str, str]] = []
    position = 0
    starts: list[tuple[int, str, str]] = []
    for number, title in toc:
        for i in range(position, len(lines)):
            own = _heading_number(lines[i], title)
            if own is not None:
                starts.append((i, own or number, title))
                position = i + 1
                break
        else:
            warnings.append(f"heading not found in the body: {number} {title}")
    numbers = [n for _, n, _ in starts]
    if len(set(numbers)) != len(numbers):
        warnings.append(f"a section number occurs twice: {numbers}")
    for k, (i, number, title) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
        body = lines[i + 1 : end]
        while body and not _title_done(lines[i], title) and body[0].strip() in title:
            body = body[1:]  # the rest of a heading wrapped over two lines
        text = _paragraphs(body)
        if text:
            sections.append((number, title, text))
    return sections


def _title_done(heading_line: str, title: str) -> bool:
    return heading_line.strip().endswith(title.split()[-1])


def _paragraphs(lines: list[str]) -> str:
    """Joined lines, a paragraph break after a line that ends a sentence well short of the
    width (as in the act parser), footnote marks glued to words left as they are."""
    if not lines:
        return ""
    width = max(len(ln.rstrip()) for ln in lines)
    paragraphs: list[str] = []
    current = ""
    for line in lines:
        current = _join(current, line.strip())
        if line.rstrip().endswith((".", ":", "?")) and len(line.rstrip()) < 0.85 * width:
            paragraphs.append(current)
            current = ""
    if current:
        paragraphs.append(current)
    text = "\n\n".join(re.sub(r"\s+", " ", p).strip() for p in paragraphs)
    # Footnote marks: after a letter, maybe one punctuation mark between ("skyddet,12",
    # "överklagas:4"), or after ")." ("(1999:1229).35"); "MIG 2018:5" keeps its number.
    text = re.sub(r"(?<=[a-zåäöé])([.,;:)”’]?)\d{1,3}(?=[\s.,;:)]|$)", r"\1", text)
    return re.sub(r"(?<=\d\)\.)\d{1,3}(?=\s|$)", "", text)


def section_start(paper: Paper, number: str) -> date | None:
    """The last revision that changed this section or one above it, else the paper's start."""
    lineage = {".".join(number.split(".")[: i + 1]) for i in range(number.count(".") + 1)}
    revised = [when for when, _, sections in paper.revisions if lineage & set(sections)]
    return max(revised) if revised else paper.in_force


def units(paper: Paper, url: str) -> list[ParsedUnit]:
    document = f"rs:{paper.rs}"
    return [
        ParsedUnit(
            unit_id=f"{document}/{number}",
            source_type="position_paper",
            authority="position_paper",
            language="sv",
            document=document,
            heading=f"{paper.rs} {paper.title} (version {paper.version}), {number} {title}",
            url=url,
            text=text,
            valid_from=section_start(paper, number),
            amended_by=f"{paper.rs} version {paper.version}",
        )
        for number, title, text in paper.sections
    ]
