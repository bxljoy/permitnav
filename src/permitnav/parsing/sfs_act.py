"""An amending act's PDF as printed: the new wording of each section it touches (slice 3).

An act prints the full new wording of every section it changes or adds, chapter by chapter,
then "Denna lag träder i kraft …". Each changed section carries a footnote naming the act
whose wording it replaces ("Senaste lydelse 2018:319"); a section without one is new, or
replaces the statute's original wording. That is what lets older wordings be rebuilt: the
wording before act A is the text of the same section in the act its footnote names.

The layout this reads (svenskforfattningssamling.se from 2018, printed SFS before):
    page number / "SFS 2024:1220" header lines, then body lines;
    "5 kap."                     a chapter heading;
    "18 a §4    Det som …"       a section: number, footnote number glued to "§", text;
    "4 Senaste lydelse 2018:319." the page's footnotes, numbered in order through the act.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field

import pypdf
from pdfminer.high_level import extract_text

logging.getLogger("pypdf").setLevel(logging.ERROR)  # font warnings; the text is unaffected
logging.getLogger("pdfminer").setLevel(logging.ERROR)

WORD = re.compile(r"[A-Za-zÅÄÖåäöÉé]+")
SFS = r"\d{4}:\d+"
JUNK = re.compile(
    r"^(\d{1,3}|SFS(?: \d{4}:\d+)?|\d{4}:\d+|Publicerad|den \d{1,2} \w+ \d{4}|(?:\d \d \d.*: ;)"
    r"|Svensk författningssamling|Utkom från trycket"
    r"|(?:Wolters Kluwer|Elanders|Norstedts|Fritzes)[^,]*(?:, \d{4})?.*)$"  # printers' footers
)
RESYNC = 3  # how far ahead a missed footnote may leave the count
# How footnotes begin, for acts whose footnote references are not glued to a word
# ("beslut 1 föreskrivs 2" in some printed SFS).
NOTE_START = (
    r"(?:Prop\.|Senaste lydelse|Jfr|Lydelse|Ändringen|Paragrafen|Rubriken|Tidigare|Kapitlet"
    r"|Bilaga|Upphävd|EGT|EUT|Celex|Bet\.|Rättelse|Förordningen|Lagen)"
)
CHAPTER = re.compile(r"^(\d+(?: [a-z])?) kap\.?(?:\s+(\S.*))?$")  # "1 kap" also occurs
# The footnote number is glued to "§" ("18 a §4") or, in some acts, set apart ("8 c § 3").
SECTION = re.compile(
    r"^(\d+(?: ?[a-z])?) ?§(?:(\d{1,3})|\s?(\d{1,3})(?=\s+[A-ZÅÄÖ]))?(?:\s+(.*))?$"
)
BODY_END = re.compile(
    r"^(?:\d\. )?Denn[ae] (?:lag|förordning)|^På regeringens vägnar|^Övergångsbestämmelser"
)
ENDS_SENTENCE = (".", ":", ";", ",")


@dataclass
class ActSection:
    chapter: str  # "6 a"
    section: str  # "18 a"
    text: str  # paragraphs joined by a blank line, as the consolidated text stores them
    footnote: str = ""  # the section's footnote, if any
    previous: str | None = None  # the act whose wording this replaces ("Senaste lydelse")
    renumbered: bool = False  # "Senaste lydelse av tidigare 9 §": it had another number
    old_number: str | None = None  # that number: "9" (same chapter) or "8 a kap. 4"
    footnote_missing: bool = False  # it has a footnote number whose text was not found

    def unit_suffix(self) -> str:
        return f"{self.chapter.replace(' ', '')}kap/{self.section.replace(' ', '')}§"


@dataclass
class ParsedAct:
    sfs: str
    sections: list[ActSection] = field(default_factory=list)
    footnotes: dict[int, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def pdf_pages(content: bytes) -> list[str]:
    """The text of each page, from pypdf, with its split words mended.

    pypdf keeps every word in order, but in the printed SFS (before 2018) it splits about
    0.6% of words at kerning gaps ("arbetstillstå nd"). pdfminer keeps those words whole but
    can drop or reorder words, so it is used only as a dictionary: two fragments are joined
    when together they make a word pdfminer saw in the same act and one of them is not.
    """
    pages = [page.extract_text() or "" for page in pypdf.PdfReader(io.BytesIO(content)).pages]
    vocabulary = set(WORD.findall(extract_text(io.BytesIO(content))))
    return [mend_split_words(page, vocabulary) for page in pages]


def mend_split_words(text: str, vocabulary: set[str]) -> str:
    """Join "arbetstillstå nd" into "arbetstillstånd" when the joined word is one the act
    uses and one of the halves is not; each space is looked at once, left to right, so a
    word split twice ("a rbetstillstå nd") is mended in one pass."""
    parts = re.split(r"( )", text)  # words and the single spaces between them
    out: list[str] = []
    for part in parts:
        if out and out[-1] == " " and len(out) >= 2:
            left_word = re.search(r"([A-Za-zÅÄÖåäöÉé]+)$", out[-2])
            right_word = re.match(r"([a-zåäöé]+)", part)
            if left_word and right_word:
                joined = left_word.group(1) + right_word.group(1)
                if joined in vocabulary and (
                    left_word.group(1) not in vocabulary or right_word.group(1) not in vocabulary
                ):
                    out.pop()  # the space
                    out[-1] += part
                    continue
        out.append(part)
    return "".join(out)


def parse_act(pages: list[str], sfs: str) -> ParsedAct:
    act = ParsedAct(sfs)
    body: list[str] = []
    next_note = 1
    for page in pages:
        # The page header can share a line with what follows, even with no space:
        # "SFS 2013:606 6 a kap.", "SFS 2017:2212 kap." (12 kap.), "SFS 2012:1292§6" (2 §).
        header = re.compile(rf"^SFS {re.escape(sfs)}\s*")
        lines = [header.sub("", line.rstrip()) for line in page.splitlines()]
        lines = [line for line in lines if line.strip() and not JUNK.match(line.strip())]
        start = _footnotes_start(lines, next_note)
        body += lines[:start]
        for number, text in _footnotes(lines[start:], next_note):
            act.footnotes[number] = text
            next_note = number + 1
    _read_body(act, body)
    return act


def _footnotes_start(lines: list[str], next_note: int) -> int:
    """Where the page's footnotes begin: the first line that starts with the next footnote's
    number and either reads like a footnote ("Prop.", "Senaste lydelse") or follows a mark
    of that number earlier on the page ("§4", "beslut1"). A body line can start with the
    same number ("1 eller 3 a § …", "4 d §5 Kravet …"); footnote text starts with a capital,
    sometimes glued to its number ("14Ändringen innebär …")."""
    for i, line in enumerate(lines):
        if not re.match(rf"^{next_note}\s*(?![a-z]? ?§|[a-z] kap\.|kap\.)[A-ZÅÄÖ]", line):
            continue
        if re.match(rf"^{next_note}\s*{NOTE_START}", line):
            return i
        if re.search(rf"(?:[a-zåäö)]|§ ?){next_note}(?!\d)", "\n".join(lines[:i])):
            return i
    # A footnote missed earlier leaves the count behind and every later one would leak into
    # the text: a line that plainly starts a footnote a little further on resyncs it.
    for i, line in enumerate(lines):
        ahead = re.match(rf"^(\d{{1,3}})\s*{NOTE_START}", line)
        if ahead and next_note < int(ahead.group(1)) <= next_note + RESYNC:
            return i
    return len(lines)


def _footnotes(lines: list[str], next_note: int) -> list[tuple[int, str]]:
    notes: list[tuple[int, list[str]]] = []
    for line in lines:
        found = re.match(r"^(\d{1,3})\s*([A-ZÅÄÖ].*)$", line.strip())
        number = int(found.group(1)) if found else 0
        if found and (
            number == next_note
            or (next_note < number <= next_note + RESYNC and re.match(NOTE_START, found.group(2)))
        ):
            notes.append((number, [found.group(2)]))
            next_note = number + 1
        elif notes:
            notes[-1][1].append(line.strip())
    return [(n, " ".join(parts)) for n, parts in notes]


def _read_body(act: ParsedAct, lines: list[str]) -> None:
    chapter: str | None = None
    current: tuple[ActSection, list[str], int | None] | None = None
    started = False
    last_key: tuple[str, tuple[int, str]] | None = None
    clause: list[str] = []  # the enacting clause, before the first chapter heading

    def close() -> None:
        if current is None:
            return
        section, text_lines, note = current
        section.text = _strip_note_marks(
            _paragraphs(_drop_trailing_heading(text_lines)), act.footnotes
        )
        if note is not None:
            section.footnote = act.footnotes.get(note, "")
            section.footnote_missing = note not in act.footnotes
            # "Senaste lydelse 2018:319" or "… av tidigare 8 a kap. 4 § 2022:1015" (renumbered)
            former = r"(?: av tidigare ((?:\d+(?: [a-z])? kap\. )?\d+(?: [a-z])?) §)?"
            previous = re.search(rf"Senaste lydelse{former} ({SFS})", section.footnote)
            section.previous = previous.group(2) if previous else None
            section.old_number = previous.group(1) if previous else None
            section.renumbered = section.old_number is not None
        act.sections.append(section)

    for raw in lines:
        line = raw.strip()
        if started and BODY_END.match(line):
            break
        heading = CHAPTER.match(line)
        if heading and (heading.group(2) is None or heading.group(2)[0].isupper()):
            close()
            current, chapter, started = None, heading.group(1), True
            continue
        found = SECTION.match(line)
        rest = (found.group(4) or "") if found else ""
        if found and chapter is None and rest[:1].isupper():
            # An act changing sections of one chapter may print no chapter heading:
            # "… att 3 kap. 1 § utlänningsförordningen ska ha följande lydelse. / 1 § …"
            named = set(re.findall(r"(\d+(?: [a-z])?) kap\.", " ".join(clause)))
            if len(named) == 1:
                chapter, started = named.pop(), True
        if not started:
            clause.append(line)
        if found and chapter is not None and (not rest or rest[0].isupper()):
            key = (chapter, _order(found.group(1)))
            if last_key is None or key[0] != last_key[0] or key[1] > last_key[1]:
                close()
                digits = found.group(2) or found.group(3)
                note = int(digits) if digits else None
                number = re.sub(r"(\d)([a-z])", r"\1 \2", found.group(1))
                current = (ActSection(chapter, number, ""), [rest] if rest else [], note)
                last_key = key
                continue
        if current is not None:
            current[1].append(line)
    close()
    if not act.sections:
        act.warnings.append("no sections found")


def _strip_note_marks(text: str, footnotes: dict[int, str]) -> str:
    """Footnote numbers glued to a word inside the text ("bära följderna av detta4.")."""

    def drop(match: re.Match[str]) -> str:
        return "" if int(match.group(0)) in footnotes else match.group(0)

    return re.sub(r"(?<=[a-zåäö)])\d{1,3}(?=[.,;:]?(?:\s|$))", drop, text)


def _order(number: str) -> tuple[int, str]:
    found = re.match(r"(\d+) ?([a-z]?)", number)
    assert found is not None
    return int(found.group(1)), found.group(2)


def _drop_trailing_heading(lines: list[str]) -> list[str]:
    """A heading printed before the next section ("Arbetstillstånd", possibly over two or
    three lines and with a footnote number glued on) ends up after this section's text:
    final lines that end no sentence, after a line that ends one."""
    for cut in range(len(lines) - 1, max(len(lines) - 4, 0), -1):
        tail = lines[cut:]
        if lines[cut - 1].endswith((".", ":", ";")) and not any(
            re.sub(r"\d+$", "", line).endswith(ENDS_SENTENCE) for line in tail
        ):
            return lines[:cut]
    return lines


def _paragraphs(lines: list[str]) -> str:
    if not lines:
        return ""
    width = sorted(len(line) for line in lines)[int(len(lines) * 0.9) - 1 if len(lines) > 1 else 0]
    paragraphs: list[str] = []
    current = ""
    for i, line in enumerate(lines):
        starts_item = bool(re.match(r"^(\d+\.|[a-z]\)|–) ", line))
        if current and starts_item:
            paragraphs.append(current)
            current = ""
        current = _join(current, line)
        short = len(line) < 0.8 * width
        last = i == len(lines) - 1
        if not last and line.endswith((".", ":")) and short:
            paragraphs.append(current)
            current = ""
    if current:
        paragraphs.append(current)
    return "\n\n".join(re.sub(r"\s+", " ", p).strip() for p in paragraphs)


def _join(text: str, line: str) -> str:
    """Join a line, mending a word broken at the line end ("anknyt-" + "ningspersonen");
    a hyphen after a capital or digit is part of the word ("EU-" + "blåkort")."""
    if not text:
        return line
    if text.endswith("-") and len(text) > 1 and text[-2].islower() and line[:1].islower():
        return text[:-1] + line
    if text.endswith("-"):
        return text + line
    return f"{text} {line}"
