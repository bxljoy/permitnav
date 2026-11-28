"""Which part of a Migration Court of Appeal ruling a passage comes from.

A reported ruling (MIG) is one text with several voices in it: the headnote written for the
report, the account of the application and Migrationsverket's decision, the lower court's
judgment and reasoning, what the parties claimed on appeal, and then the Migration Court of
Appeal's own text, its assessment and its holding. The lower court often reasoned the
opposite of what the Migration Court of Appeal then held. A passage cut from the middle of
the ruling carries none of that: every passage is headed "MIG … (Migrationsöverdomstolen)",
and a model reading it has attributed the lower court's reasoning to the higher court.

The parts are found in the whole ruling, never guessed from a passage, from the fixed
phrases the reports use. Finding them is deliberately strict: where a phrase is missing or
occurs more than once, that boundary is not drawn, and a stretch that cannot be told apart
is called "not classified further", never something more specific. A ruling without the
Migration Court of Appeal's opening phrase gets no parts at all, and is shown as before.

The ruling's text is never changed. `labelled` gives a copy of a passage for the model to
read, with a note on its own line where a part begins. The notes are marked as not part of
the ruling and are fenced by characters no ruling contains, so they cannot pass for the
court's words: a quotation is checked against the ruling's own text (answering.answerer),
a note copied whole into an answer is removed (`without_notes`), and a reply with a piece
of one left in it is not used (`mentions_a_note`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

OPEN, CLOSE = "⟦", "⟧"  # in no ruling: tests check the corpus
NOTE = "Editorial note, not part of the ruling"
# A whole note as copied, between its fences.
_NOTE_SPAN = re.compile(rf"\s*{OPEN}[^{OPEN}{CLOSE}\n]*{CLOSE}\s*")
# What is left of a note that was not copied whole: how `mentions_a_note` knows one.
_NOTE_WORDS = "editorial note"

HEADNOTE = "headnote"
BACKGROUND = "background"
EARLIER = "earlier"  # before the Migration Court of Appeal's own text, not told apart
LOWER_COURT = "lower_court"
LOWER_COURT_AND_APPEAL = "lower_court_and_appeal"
APPEAL = "appeal"
APPELLATE = "appellate"  # the court's own text before its assessment
APPELLATE_WHOLE = "appellate_whole"  # the court's own text, its assessment not told apart
ASSESSMENT = "assessment"
HOLDING = "holding"
ENDING = "ending"  # from where the holding is announced, when it is not a plain holding
DISSENT = "dissent"

LABELS = {
    # The headnote is where the holding is stated: the note keeps its authority, and says
    # only that it is not the court's reasoning word for word.
    HEADNOTE: (
        "Official case headnote: summary of the Migration Court of Appeal's decision. Not a"
        " verbatim excerpt from the court's reasoning"
    ),
    # Migrationsverket's reasons are quoted at length here, and were read as the court's.
    BACKGROUND: (
        "background as the ruling reports it: the application, and Migrationsverket's decision"
        " with its reasons. These are NOT the Migration Court of Appeal's findings, which may"
        " differ"
    ),
    EARLIER: (
        "what came before the Migration Court of Appeal's own text: earlier decisions and"
        " the parties' claims, not classified further. Not the Migration Court of Appeal's"
        " reasoning"
    ),
    LOWER_COURT: (
        "the lower court ({court}): its judgment and reasoning. This is NOT the Migration"
        " Court of Appeal's holding, which may differ"
    ),
    LOWER_COURT_AND_APPEAL: (
        "the lower court ({court}) and the appeal against its judgment. NOT the Migration"
        " Court of Appeal's holding, which may differ"
    ),
    APPEAL: (
        "the appeal to the Migration Court of Appeal: what the parties claimed and argued."
        " Submissions, not the court's findings"
    ),
    APPELLATE: (
        "the Migration Court of Appeal's own text: the question in the case, and the rules"
        " and earlier practice it sets out"
    ),
    # Nothing here that could make the court's own text sound uncertain.
    APPELLATE_WHOLE: "Migration Court of Appeal: the court's own text",
    ASSESSMENT: "the Migration Court of Appeal's assessment: its reasoning in this case",
    HOLDING: "the Migration Court of Appeal's final holding: the outcome of the case",
    ENDING: "the end of the ruling, where the outcome is given; not classified further",
    DISSENT: (
        "a separate or dissenting opinion by a member of the court, given after the holding."
        " NOT the court's reasoning or decision"
    ),
}

_DATE = r"\(\d{4}-\d{2}-\d{2}"
_APPELLATE = re.compile(rf"Kammarrätten i Stockholm, Migrationsöverdomstolen {_DATE}")
_LOWER = re.compile(rf"((?:Förvaltningsrätten|Länsrätten) i [^,\n]+, migrationsdomstolen) {_DATE}")
_PROVISIONS = re.compile(r"^Lagrum:", re.MULTILINE)
_NUMBER = r"(?:\d+(?:\.\d+)*\.?[ \t]+)?"
# Where the court's assessment begins: a heading, or the sentence some reports use instead.
_ASSESSMENT = re.compile(
    rf"^(?:{_NUMBER}Migrationsöverdomstolens bedömning[ \t]*$"
    r"|Migrationsöverdomstolen gör följande bedömning\.)",
    re.MULTILINE,
)
# Where the outcome is announced: in the line itself, or as a heading before it.
_HOLDING = re.compile(
    rf"{_NUMBER}Migrationsöverdomstolens? avgörande(?:[.:] |[.:]?[ \t]*(?:\n\n|$))"
)
_DISSENT = re.compile(r"[^\n]{0,160}\bskiljaktig")
MIN_SHARE = 0.8  # of the rulings in a corpus: fewer with parts means the parser is broken
MAX_HOLDING = 1200  # characters: longer than any plain holding (the longest known is 522)
# The paragraph that opens the appeal: who appealed the lower court's judgment.
_APPEALED = re.compile(
    r"[^\n]{0,200}?\böverklagade"
    r" (?:migrationsdomstolens|förvaltningsrättens|länsrättens|domstolens|domen)\b"
)
_BREAK = "\n\n"


@dataclass(frozen=True)
class Part:
    kind: str
    start: int  # offsets in the ruling's own text
    end: int
    court: str = ""  # the lower court's name, where the part is its

    @property
    def label(self) -> str:
        return LABELS[self.kind].format(court=self.court)


def _paragraph_start(text: str, position: int) -> int:
    found = text.rfind(_BREAK, 0, position)
    return 0 if found < 0 else found + len(_BREAK)


def _paragraph_starts(text: str, start: int, end: int) -> list[int]:
    """Where the paragraphs in text[start:end] begin (start itself included)."""
    starts, position = [start], text.find(_BREAK, start, end)
    while position >= 0:
        starts.append(position + len(_BREAK))
        position = text.find(_BREAK, position + len(_BREAK), end)
    return [s for s in starts if s < end]


def parts(text: str) -> list[Part] | None:
    """The parts of a whole ruling, in order and covering all of it; None when the ruling
    does not have the shape the reports have (then nothing is said about it)."""
    if not text.startswith("Sammanfattning:"):
        return None
    provisions = _PROVISIONS.findall(text)
    appellate = list(_APPELLATE.finditer(text))
    if len(provisions) != 1 or len(appellate) != 1:
        return None
    after_provisions = text.find(_BREAK, _PROVISIONS.search(text).start())  # type: ignore[union-attr]
    court_text = _paragraph_start(text, appellate[0].start())
    if court_text != appellate[0].start():
        return None  # the court's opening phrase shares its paragraph with something else
    if after_provisions < 0 or after_provisions + len(_BREAK) > court_text:
        return None
    body = after_provisions + len(_BREAK)

    marks: list[tuple[int, str, str]] = [(0, HEADNOTE, "")]
    lower = [m for m in _LOWER.finditer(text) if m.start() < court_text]
    lower_start = _paragraph_start(text, lower[0].start()) if len(lower) == 1 else -1
    if lower_start >= body:
        if lower_start > body:
            marks.append((body, BACKGROUND, ""))
        # The appeal opens with a paragraph of its own, after the lower court's first one.
        opened = [
            start
            for start in _paragraph_starts(text, lower_start, court_text)[1:]
            if _APPEALED.match(text, start)
        ]
        if opened:
            marks.append((lower_start, LOWER_COURT, lower[0][1]))
            marks.append((opened[0], APPEAL, ""))
        else:
            marks.append((lower_start, LOWER_COURT_AND_APPEAL, lower[0][1]))
    elif body < court_text:  # no lower court named, or more than one: not told apart
        marks.append((body, EARLIER, ""))
    paragraphs = _paragraph_starts(text, court_text, len(text))[1:]
    holding = [s for s in paragraphs if _HOLDING.match(text, s)]
    # The assessment is looked for before the holding only: what follows the holding (a
    # separate opinion) may use the same words and is never the court's assessment.
    before = holding[0] if len(holding) == 1 else len(text)
    assessment = [m.start() for m in _ASSESSMENT.finditer(text, court_text)]
    assessment = [a for a in assessment if a in paragraphs and a < before]
    told_apart = len(assessment) == 1
    marks.append((court_text, APPELLATE if told_apart else APPELLATE_WHOLE, ""))
    if told_apart:
        marks.append((assessment[0], ASSESSMENT, ""))
    if len(holding) == 1 and holding[0] > marks[-1][0]:
        # A separate opinion follows the holding, and only there is it looked for: the word
        # earlier (a lower court's lay judges, a rule about dissents) draws no boundary, so
        # the court's own reasoning is never put under the dissent's note.
        dissent = next((s for s in paragraphs if s > holding[0] and _DISSENT.match(text, s)), None)
        plain = (dissent or len(text)) - holding[0] <= MAX_HOLDING
        marks.append((holding[0], HOLDING if plain else ENDING, ""))
        if dissent is not None:
            marks.append((dissent, DISSENT, ""))

    found = [
        Part(kind, start, end, court)
        for (start, kind, court), end in zip(
            marks, [m[0] for m in marks[1:]] + [len(text)], strict=True
        )
    ]
    if any(part.start >= part.end for part in found):
        return None  # out of order: not the shape expected
    return found


# The parts that are not the Migration Court of Appeal's own: only a passage holding one of
# these gets notes. A passage of headnote and court text alone is shown as it is, so most
# prompts do not change at all (the wider design, notes on every passage, made a related
# confusion more frequent on one question and is measured in docs/DESIGN.md).
NOT_THE_COURT = frozenset(
    {BACKGROUND, EARLIER, LOWER_COURT, LOWER_COURT_AND_APPEAL, APPEAL, DISSENT}
)

# Labels that are a statement of their own, not the end of "What follows is …".
STATEMENTS = {HEADNOTE, APPELLATE_WHOLE}


def note(part: Part, continued: bool = False) -> str:
    more = " (continued)" if continued else ""
    lead = "" if part.kind in STATEMENTS else "What follows is "
    return f"{OPEN}{NOTE}. {lead}{part.label}{more}.{CLOSE}"


def labelled(ruling: str, passage: str) -> str | None:
    """The passage with a note where each part of the ruling begins in it. None, and the
    passage is then shown as it is, when the ruling has no parts, when the passage is not
    exactly one stretch of the ruling's text, or when everything in it is the headnote or
    the Migration Court of Appeal's own text (nothing there can be mistaken for another
    voice). Nothing of the passage is changed or left out."""
    if not passage or OPEN in ruling or CLOSE in ruling:
        return None
    offset = ruling.find(passage)
    if offset < 0 or ruling.find(passage, offset + 1) >= 0:
        return None
    found = parts(ruling)
    if found is None:
        return None
    end = offset + len(passage)
    pieces: list[str] = []
    other_voice = False
    for part in found:
        if part.end <= offset or part.start >= end:
            continue
        start = max(part.start, offset)
        text = ruling[start : min(part.end, end)]
        if not text.strip():
            continue
        other_voice = other_voice or part.kind in NOT_THE_COURT
        pieces.append(note(part, continued=part.start < offset))
        pieces.append(text.strip("\n"))
    return _BREAK.join(pieces) if pieces and other_voice else None


def _any_note() -> re.Pattern[str]:
    """A whole note's wording, whatever stands around it (a model may drop the fences or
    write other brackets). The lower court's name is the one part that varies."""
    labels = "|".join(
        re.escape(label).replace(re.escape("{court}"), rf"[^{OPEN}{CLOSE}\n]{{1,150}}?")
        for label in LABELS.values()
    )
    wording = rf"{re.escape(NOTE)}\. (?:What follows is )?(?:{labels})(?: \(continued\))?\."
    return re.compile(rf"\s*[\[(]?{wording}[\])]?\s*")


_NOTE_WORDING = _any_note()


def without_notes(text: str) -> str:
    """A text with every whole editorial note taken out (an answer must not repeat one),
    fenced or with the fences dropped, and any stray fence. A note that stood between two
    paragraphs leaves them two paragraphs. Nothing else is removed: what is left of a note
    copied only in part stays, and `mentions_a_note` says so."""
    if OPEN not in text and CLOSE not in text and _NOTE_WORDS not in text.lower():
        return text

    def gap(match: re.Match[str]) -> str:
        return _BREAK if "\n" in match[0] else " "

    text = _NOTE_WORDING.sub(gap, _NOTE_SPAN.sub(gap, text))
    return text.replace(OPEN, "").replace(CLOSE, "").strip()


def mentions_a_note(text: str) -> bool:
    """Whether a text still carries a piece of a note after `without_notes`: one cut off or
    reworded. Where it ends cannot be told, so nothing is cut out; the text is refused."""
    return _NOTE_WORDS in text.lower() or OPEN in text or CLOSE in text


_ORDER = re.compile(
    rf"{HEADNOTE}( {BACKGROUND})?"
    rf"( {LOWER_COURT} {APPEAL}| {LOWER_COURT_AND_APPEAL}| {EARLIER})?"
    rf"( {APPELLATE} {ASSESSMENT}| {APPELLATE_WHOLE})(( {HOLDING}| {ENDING})( {DISSENT})?)?"
)


def _squeezed(text: str) -> str:
    return " ".join(text.split())


def problems(ruling: str, passages: list[str]) -> list[str]:
    """What is wrong with the parts found in a ruling and with the labelled copies of its
    passages; empty when nothing is. The checks a corpus has to pass: the fence characters
    occur in no ruling, the parts cover the ruling in a known order, and a labelled passage
    is the passage itself with notes added and nothing changed."""
    found: list[str] = []
    if OPEN in ruling or CLOSE in ruling or NOTE in ruling:
        found.append("the ruling's own text contains the notes' fence or wording")
    made = parts(ruling)
    if made is not None:
        covers = made[0].start == 0 and made[-1].end == len(ruling)
        joined = all(a.end == b.start for a, b in zip(made, made[1:], strict=False))
        if not (covers and joined):
            found.append("the parts do not cover the ruling end to end")
        if not _ORDER.fullmatch(" ".join(part.kind for part in made)):
            found.append("the parts are in no known order: " + " ".join(p.kind for p in made))
    for number, passage in enumerate(passages):
        shown = labelled(ruling, passage)
        if shown is None:
            continue
        if made is None:
            found.append(f"passage {number} is labelled though the ruling has no parts")
            continue
        if _squeezed(without_notes(shown)) != _squeezed(passage):
            found.append(f"passage {number}: the labelled copy is not the passage with notes added")
        known = {note(part, continued) for part in made for continued in (False, True)}
        notes = re.findall(rf"{OPEN}[^{CLOSE}]*{CLOSE}", shown)
        if not notes or not shown.startswith(OPEN) or any(n not in known for n in notes):
            found.append(f"passage {number}: a note that is none of the ruling's own")
    return found
