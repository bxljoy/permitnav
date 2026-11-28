"""Which part of a ruling a passage is (parsing.ruling_parts): the parts are found in the
whole ruling from its fixed phrases, strictly; a passage's labelled copy adds notes and
changes nothing; and a note can never pass for the court's words."""

from __future__ import annotations

import dataclasses
import json
import os
from datetime import date
from pathlib import Path

import psycopg
import pytest

from permitnav import versions
from permitnav.answering.answerer import (
    AnswerConfig,
    Answerer,
    answer_ruling_notes,
    parse_answer,
)
from permitnav.answering.context import Context, Source, build_context
from permitnav.corpus.indexing import build_chunks, embed_chunks
from permitnav.llm.fake import ScriptedLLM
from permitnav.llm.port import Completion
from permitnav.parsing import ruling_parts as rp
from permitnav.retrieval.retriever import Hit, Retriever
from tests.test_ingest import FIXTURE, ingest
from tests.test_retriever import FAKE

RULING = (Path(__file__).parent / "fixtures" / "mig_2024_12.txt").read_text(encoding="utf-8")


def kinds(text: str) -> list[str] | None:
    made = rp.parts(text)
    return None if made is None else [part.kind for part in made]


def part(kind: str, text: str = RULING) -> str:
    (found,) = [p for p in rp.parts(text) or [] if p.kind == kind]
    return text[found.start : found.end]


def test_the_parts_of_a_real_ruling() -> None:
    assert kinds(RULING) == [
        rp.HEADNOTE,
        rp.BACKGROUND,
        rp.LOWER_COURT,
        rp.APPEAL,
        rp.APPELLATE,
        rp.ASSESSMENT,
        rp.HOLDING,
    ]
    assert part(rp.HEADNOTE).startswith("Sammanfattning: Vid prövningen")
    assert "Lagrum:" in part(rp.HEADNOTE)  # the provisions listed belong to the headnote
    assert part(rp.BACKGROUND).startswith("CM ansökte om uppehålls- och arbetstillstånd")
    lower = part(rp.LOWER_COURT)
    assert lower.startswith("CM överklagade beslutet till Förvaltningsrätten i Malmö")
    # What the lower court said, and the higher court did not: it stays in the lower part.
    assert "inte i sig skäl för att avslå ansökan" in lower
    assert "inte i sig skäl för att avslå ansökan" not in part(rp.ASSESSMENT)
    assert part(rp.APPEAL).startswith("Migrationsverket överklagade migrationsdomstolens dom")
    assert part(rp.APPELLATE).startswith("Kammarrätten i Stockholm, Migrationsöverdomstolen (")
    assert part(rp.ASSESSMENT).startswith("3. Migrationsöverdomstolens bedömning")
    assert "Migrationsverket har därmed haft fog för sitt beslut" in part(rp.ASSESSMENT)
    assert part(rp.HOLDING) == (
        "Migrationsöverdomstolens avgörande. Migrationsöverdomstolen skriver av målet."
    )
    made = rp.parts(RULING) or []
    assert made[0].start == 0 and made[-1].end == len(RULING)
    assert all(a.end == b.start for a, b in zip(made, made[1:], strict=False))
    assert [p.court for p in made if p.court] == ["Förvaltningsrätten i Malmö, migrationsdomstolen"]


def test_the_headnote_is_called_a_summary_and_the_lower_court_not_the_holding() -> None:
    made = {p.kind: p for p in rp.parts(RULING) or []}
    # The headnote keeps its authority: a summary of the decision, only not word for word.
    assert rp.note(made[rp.HEADNOTE]) == (
        f"{rp.OPEN}Editorial note, not part of the ruling. Official case headnote: summary of"
        " the Migration Court of Appeal's decision. Not a verbatim excerpt from the court's"
        f" reasoning.{rp.CLOSE}"
    )
    assert "Förvaltningsrätten i Malmö" in made[rp.LOWER_COURT].label
    assert "NOT the Migration Court of Appeal's holding" in made[rp.LOWER_COURT].label
    assert "Submissions, not the court's findings" in made[rp.APPEAL].label
    assert "final holding" in made[rp.HOLDING].label


def test_a_passage_gets_a_note_where_each_part_begins_and_is_otherwise_untouched() -> None:
    made = {p.kind: p for p in rp.parts(RULING) or []}
    # From inside the lower court's reasoning to inside the higher court's own text.
    passage = RULING[made[rp.LOWER_COURT].start + 400 : made[rp.APPELLATE].start + 300]
    shown = rp.labelled(RULING, passage)
    assert shown is not None and shown.startswith(rp.OPEN)
    notes = [line for line in shown.split("\n\n") if line.startswith(rp.OPEN)]
    assert len(notes) == 3
    assert "the lower court (Förvaltningsrätten i Malmö" in notes[0] and "(continued)" in notes[0]
    assert "the appeal to the Migration Court of Appeal" in notes[1]
    assert "(continued)" not in notes[1]
    assert "the Migration Court of Appeal's own text" in notes[2]
    # Each note stands right before the part it names.
    assert (
        shown.index(notes[1]) < shown.index("Migrationsverket överklagade") < shown.index(notes[2])
    )
    assert shown.index(notes[2]) < shown.index("Kammarrätten i Stockholm, Migrationsöverdomstolen")
    # Nothing of the ruling is changed or lost: without the notes it is the passage.
    assert " ".join(rp.without_notes(shown).split()) == " ".join(passage.split())
    assert rp.problems(RULING, [RULING, passage]) == []


def test_a_passage_of_headnote_or_court_text_alone_is_shown_as_it_is() -> None:
    made = {p.kind: p for p in rp.parts(RULING) or []}
    headnote = RULING[made[rp.HEADNOTE].start : made[rp.HEADNOTE].end]
    court = RULING[made[rp.APPELLATE].start + 50 : made[rp.HOLDING].end]
    assert rp.labelled(RULING, headnote) is None
    assert rp.labelled(RULING, court) is None  # the court's text, its assessment, its holding
    # One paragraph of another voice in the passage, and every part in it is named.
    with_background = RULING[: made[rp.BACKGROUND].start + 200]
    shown = rp.labelled(RULING, with_background) or ""
    assert shown.count(rp.OPEN) == 2 and "Official case headnote" in shown
    assert "These are NOT the Migration Court of Appeal's findings" in shown


def test_the_whole_ruling_labelled() -> None:
    shown = rp.labelled(RULING, RULING)
    assert shown is not None and shown.count(rp.OPEN) == 7 and "(continued)" not in shown


@pytest.mark.parametrize(
    "ruling",
    [
        RULING.replace("Sammanfattning:", "Referat:", 1),  # not a report's opening
        RULING.replace("Kammarrätten i Stockholm, Migrationsöverdomstolen (", "Domstolen ("),
        RULING + "\n\nKammarrätten i Stockholm, Migrationsöverdomstolen (2025-01-01, X), yttrade:",
        RULING.replace("Lagrum:", "Bestämmelser:", 1),
    ],
)
def test_a_ruling_without_the_expected_shape_gets_no_parts_at_all(ruling: str) -> None:
    assert rp.parts(ruling) is None
    assert rp.labelled(ruling, ruling[500:900]) is None  # shown as it is


def test_what_cannot_be_told_apart_is_not_classified() -> None:
    two_lower_courts = RULING.replace(
        "Migrationsverket överklagade migrationsdomstolens dom",
        "Målet prövades även av Förvaltningsrätten i Luleå, migrationsdomstolen (2024-05-01,"
        " ordförande X).\n\nMigrationsverket överklagade migrationsdomstolens dom",
    )
    assert kinds(two_lower_courts) == [
        rp.HEADNOTE,
        rp.EARLIER,
        rp.APPELLATE,
        rp.ASSESSMENT,
        rp.HOLDING,
    ]
    earlier = [p for p in rp.parts(two_lower_courts) or [] if p.kind == rp.EARLIER][0]
    assert "not classified further" in earlier.label

    no_appeal_paragraph = RULING.replace(
        "Migrationsverket överklagade migrationsdomstolens dom", "Migrationsverket klagade"
    )
    assert rp.LOWER_COURT_AND_APPEAL in (kinds(no_appeal_paragraph) or [])
    assert rp.APPEAL not in (kinds(no_appeal_paragraph) or [])

    two_assessments = RULING.replace(
        "Migrationsöverdomstolens avgörande.",
        "4. Migrationsöverdomstolens bedömning\n\nMer.\n\nMigrationsöverdomstolens avgörande.",
    )
    assert rp.ASSESSMENT not in (kinds(two_assessments) or [])
    # ... and then the court's text is not called "the rules it sets out" either.
    assert rp.APPELLATE_WHOLE in (kinds(two_assessments) or [])
    whole = [p for p in rp.parts(two_assessments) or [] if p.kind == rp.APPELLATE_WHOLE][0]
    # Said plainly, with nothing that makes the court's own text sound uncertain.
    assert rp.note(whole, continued=True) == (
        f"{rp.OPEN}Editorial note, not part of the ruling. Migration Court of Appeal: the"
        f" court's own text (continued).{rp.CLOSE}"
    )

    holding_not_last = RULING + "\n\nEtt tillägg efter avgörandet."
    assert rp.HOLDING in (kinds(holding_not_last) or [])  # a short tail is the holding's

    long_tail = RULING + "\n\n" + "Mer text efter avgörandet. " * 60
    assert (kinds(long_tail) or [])[-1] == rp.ENDING  # too long to be a plain holding


def test_a_dissenting_opinion_is_never_the_courts_reasoning() -> None:
    dissent = (
        "\n\nDahlman (referent) var skiljaktig och anförde följande:\n\nJag anser att"
        " överklagandet ska bifallas.\n\nNär det gäller frågan gör jag följande bedömning."
    )
    text = RULING + dissent
    made = rp.parts(text) or []
    assert [p.kind for p in made][-2:] == [rp.HOLDING, rp.DISSENT]
    assert text[made[-1].start :].startswith("Dahlman (referent) var skiljaktig")
    assert text[made[-2].start : made[-2].end].strip().endswith("skriver av målet.")
    assert "NOT the court's reasoning or decision" in made[-1].label
    shown = rp.labelled(text, text[made[-1].start + 40 :]) or ""
    assert "dissenting opinion" in shown and "assessment" not in shown


def test_the_word_before_the_holding_is_no_dissent() -> None:
    # The word in the court's own reasoning (a rule about dissents, a lower court's lay
    # judges) draws no boundary: the reasoning and the holding stay the court's.
    held = "\n\nMigrationsöverdomstolens avgörande."
    assert RULING.count(held) == 1
    for paragraph in (
        "En ledamot som är skiljaktig ska enligt lagen ange sin mening.",
        "Migrationsdomstolen (nämndemännen var skiljaktiga) avslog överklagandet.",
    ):
        text = RULING.replace(held, f"\n\n{paragraph}{held}")
        assert kinds(text) == kinds(RULING) and rp.DISSENT not in (kinds(text) or [])
        assert paragraph in part(rp.ASSESSMENT, text)
    # Without a holding found, nothing after the court's text is called a dissent.
    no_holding = RULING.replace(held, "\n\nMålet skrivs av.")
    dissent = "\n\nDahlman var skiljaktig och anförde följande."
    assert rp.DISSENT not in (kinds(no_holding + dissent) or [])
    assert rp._ORDER.fullmatch(f"{rp.HEADNOTE} {rp.APPELLATE} {rp.ASSESSMENT} {rp.DISSENT}") is None


def test_the_assessment_is_looked_for_before_the_holding_only() -> None:
    # A ruling whose court text has no assessment heading, and a dissent that uses the
    # words: the dissent stays a dissent, and is never "the court's assessment".
    heading = "3. Migrationsöverdomstolens bedömning"
    assert RULING.count(heading) == 1
    text = RULING.replace(heading, "3. Bedömning") + (
        "\n\nDahlman var skiljaktig och anförde följande:"
        "\n\nMigrationsöverdomstolen gör följande bedömning.\n\nJag anser annat."
    )
    assert (kinds(text) or [])[-3:] == [rp.APPELLATE_WHOLE, rp.HOLDING, rp.DISSENT]
    assert rp.ASSESSMENT not in (kinds(text) or [])


def test_the_newer_form_of_the_holding_and_the_other_form_of_the_assessment() -> None:
    newer = RULING.replace(
        "Migrationsöverdomstolens avgörande. Migrationsöverdomstolen skriver av målet.",
        "Migrationsöverdomstolens avgörande\n\nMigrationsöverdomstolen skriver av målet.",
    )
    assert (kinds(newer) or [])[-1] == rp.HOLDING
    assert part(rp.HOLDING, newer).endswith("skriver av målet.")
    sentence = RULING.replace(
        "3. Migrationsöverdomstolens bedömning", "Migrationsöverdomstolen gör följande bedömning."
    )
    assert rp.ASSESSMENT in (kinds(sentence) or [])


def test_the_courts_opening_phrase_must_open_its_paragraph() -> None:
    shared = RULING.replace(
        "\n\nKammarrätten i Stockholm, Migrationsöverdomstolen (",
        " Kammarrätten i Stockholm, Migrationsöverdomstolen (",
    )
    assert rp.parts(shared) is None


def test_a_passage_that_is_not_one_stretch_of_the_ruling_is_left_alone() -> None:
    assert rp.labelled(RULING, "Text som inte finns i avgörandet.") is None
    assert rp.labelled(RULING, "") is None
    twice = RULING + "\n\n" + RULING[600:700]
    assert rp.labelled(twice, RULING[600:700]) is None  # which of the two it is cannot be told
    fenced = RULING.replace("CM ansökte", f"{rp.OPEN}CM{rp.CLOSE} ansökte", 1)
    assert rp.labelled(fenced, fenced[0:300]) is None  # a ruling using the fence gets no notes


def test_notes_are_taken_out_of_a_text() -> None:
    a_note = rp.note((rp.parts(RULING) or [])[2])
    assert rp.without_notes(f"Before. {a_note} After.") == "Before. After."
    assert rp.without_notes("Nothing to remove [S1].") == "Nothing to remove [S1]."
    # Between two paragraphs, they stay two paragraphs.
    assert rp.without_notes(f"Conclusion [S1].\n\n{a_note}\n\nDetails [S1].") == (
        "Conclusion [S1].\n\nDetails [S1]."
    )
    # A whole note goes whatever stands around it: no fences, or other brackets.
    bare = a_note.strip(rp.OPEN + rp.CLOSE)
    assert rp.without_notes(f"A {bare} B") == "A B"
    assert rp.without_notes(f"A [{bare}] B") == "A B"
    for every in rp.parts(RULING) or []:
        for continued in (False, True):
            whole = rp.note(every, continued).strip(rp.OPEN + rp.CLOSE)
            assert rp.without_notes(f"A ({whole}) B") == "A B"
    assert rp.OPEN not in rp.without_notes(f"Stray {rp.CLOSE} fence {rp.OPEN}")
    assert not rp.mentions_a_note(rp.without_notes(f"A {a_note} B [{bare}] C"))
    # A note cut off or reworded takes nothing with it: where it ends cannot be told. The
    # text keeps its words, and is known to carry a piece of a note.
    cut = a_note[:60]
    kept = rp.without_notes(f"Marked {cut} and the court held Y [S1]. Tail [S1].")
    assert "and the court held Y [S1]. Tail [S1]." in kept and rp.mentions_a_note(kept)
    assert rp.mentions_a_note(rp.without_notes(f"A {a_note[:30]}{a_note} B"))
    assert not rp.mentions_a_note("An ordinary answer about the ruling [S1].")


def ruling_source(passage: str) -> Source:
    return Source(
        "S1",
        "mig:2024:12",
        1,
        "retrieved",
        "ruling",
        "MIG 2024:12 (Migrationsöverdomstolen, avgjort 2024-10-22)",
        "https://x",
        passage,
        "Migration Court of Appeal ruling, decided on 2024-10-22",
        rp.labelled(RULING, passage),
    )


def test_the_model_reads_the_notes_and_everything_else_gets_the_rulings_own_text() -> None:
    passage = RULING[900:2600]
    source = ruling_source(passage)
    assert source.text == passage and rp.OPEN not in source.text  # the ruling's own words
    rendered = Context(sources=[source]).render()
    assert rp.NOTE in rendered and "NOT the Migration Court of Appeal's holding" in rendered
    assert "id: mig:2024:12" in rendered
    plain = Source("S2", "sfs:x", 2, "retrieved", "law", "6 kap. 2 §", "https://x", "LAGTEXT", "d")
    assert plain.shown is None and "LAGTEXT" in Context(sources=[plain]).render()
    assert rp.NOTE not in Context(sources=[plain]).render()


def test_a_note_cannot_be_quoted_or_repeated_as_the_rulings_words() -> None:
    passage = RULING[900:2600]
    source = ruling_source(passage)
    a_note = [line for line in (source.shown or "").split("\n\n") if line.startswith(rp.OPEN)][0]
    genuine = "ska arbetsgivarens ekonomiska förmåga"
    assert genuine not in passage  # (the headnote is not in this passage)
    real_quote = "Migrationsverket överklagade migrationsdomstolens dom"
    reply = {
        "kind": "answered",
        "answer": f"The lower court said so [S1]. {a_note} The appeal followed [S1].",
        "quotes": [
            {"source": "S1", "swedish": a_note, "translation": "a note"},
            {"source": "S1", "swedish": real_quote, "translation": "the agency appealed"},
        ],
    }
    answer = parse_answer(
        Completion(text=json.dumps(reply, ensure_ascii=False), model="m"),
        Context(sources=[source]),
    )
    assert rp.OPEN not in answer.text and rp.NOTE not in answer.text  # never in an answer
    assert answer.text == "The lower court said so [S1]. The appeal followed [S1]."
    assert answer.dropped_quotes == 1  # the note offered as a quotation is refused
    assert [c.quote for c in answer.citations] == [real_quote]  # the real one is kept
    # A quotation is checked against the ruling's own text, never the labelled copy.
    label_words = "its judgment and reasoning"
    fake = {
        "kind": "answered",
        "answer": "X [S1].",
        "quotes": [{"source": "S1", "swedish": label_words}],
    }
    refused = parse_answer(Completion(text=json.dumps(fake), model="m"), Context(sources=[source]))
    assert refused.dropped_quotes == 1 and refused.citations[0].quote == ""
    # A quotation with a note in it is refused, and a note in a translation is taken out.
    mixed = {
        "kind": "answered",
        "answer": "X [S1].",
        "quotes": [
            {"source": "S1", "swedish": f"{real_quote} {a_note}", "translation": "t"},
            {"source": "S1", "swedish": real_quote, "translation": f"{a_note} the agency appealed"},
        ],
    }
    cleaned = parse_answer(
        Completion(text=json.dumps(mixed, ensure_ascii=False), model="m"), Context(sources=[source])
    )
    assert cleaned.dropped_quotes == 1
    assert cleaned.citations[0].translation == "the agency appealed"
    # A reply that is nothing but a note is no answer at all.
    only_note = json.dumps({"kind": "answered", "answer": a_note}, ensure_ascii=False)
    nothing = parse_answer(Completion(text=only_note, model="m"), Context(sources=[source]))
    assert nothing.kind == "failed"
    # A note copied in part: nothing is cut out of the answer (the holding after it would
    # go too); the reply is not used, and is asked for again like any unreadable reply.
    partly = {
        "kind": "answered",
        "answer": f"The passage is marked {a_note[:50]} and the lower court held X [S1].",
        "quotes": [{"source": "S1", "swedish": real_quote, "translation": f"{a_note[:40]} t"}],
    }
    refused = parse_answer(
        Completion(text=json.dumps(partly, ensure_ascii=False), model="m"),
        Context(sources=[source]),
    )
    assert refused.kind == "failed" and "editorial note" in refused.problem
    partly["answer"] = "The lower court held X [S1]."
    kept = parse_answer(
        Completion(text=json.dumps(partly, ensure_ascii=False), model="m"),
        Context(sources=[source]),
    )
    assert kept.kind == "answered" and kept.citations[0].quote == real_quote
    assert kept.citations[0].translation == ""  # a translation with a piece of a note: none


def test_a_reply_is_read_as_before_when_no_note_was_shown() -> None:
    # With the setting off no source has a labelled copy, and nothing in a reply is taken
    # for a note: an answer or a quotation that happens to say "editorial note", or to
    # hold a fence character, is neither changed nor asked for again.
    passage = RULING[900:2600]
    plain = dataclasses.replace(ruling_source(passage), shown=None)
    real_quote = "Migrationsverket överklagade migrationsdomstolens dom"
    said = f"The editorial note {rp.OPEN}sic{rp.CLOSE} in the newspaper is no source [S1]."
    reply = {
        "kind": "answered",
        "answer": said,
        "quotes": [
            {"source": "S1", "swedish": real_quote, "translation": "an editorial note says so"}
        ],
    }
    answer = parse_answer(
        Completion(text=json.dumps(reply, ensure_ascii=False), model="m"),
        Context(sources=[plain]),
    )
    assert answer.kind == "answered" and answer.text == said and not answer.problem
    assert answer.dropped_quotes == 0
    assert answer.citations[0].quote == real_quote
    assert answer.citations[0].translation == "an editorial note says so"


def corpus_rulings() -> list[tuple[str, str, list[str]]]:
    """Every ruling in the development corpus with its passages; none when there is no
    corpus (the test job's database is empty; CI runs `permitnav ruling-parts --check` on
    the eval corpus instead)."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        return []
    try:
        with psycopg.connect(url, connect_timeout=3) as conn:
            found = conn.execute(
                "SELECT DISTINCT ON (u.id) u.id, u.heading, v.id, v.text FROM unit u"
                " JOIN unit_version v ON v.unit_id = u.id WHERE u.source_type = 'ruling'"
                " ORDER BY u.id, v.id DESC"
            ).fetchall()
            out = []
            for unit_id, heading, version_id, text in found:
                chunks = conn.execute(
                    "SELECT text FROM chunk WHERE version_id = %s", (version_id,)
                ).fetchall()
                bodies = [
                    c.split("\n\n", 1)[1] if c.startswith(heading) and "\n\n" in c else c
                    for (c,) in chunks
                ]
                out.append((unit_id, text, bodies))
            return out
    except psycopg.Error:
        return []


# --- the setting: off unless ANSWER_RULING_NOTES says otherwise -----------------------------------

AS_OF = date(2026, 11, 20)
QUESTION = "lönen medianlönen procent lönekravet"
PASSAGE = RULING[900:2600]  # more than one voice: notes when on
NOTES = (rp.labelled(RULING, PASSAGE) or "").count(rp.OPEN)


class Found(Retriever):
    """A retriever that finds the given hits, whatever is asked."""

    def __init__(self, hits: list[Hit]) -> None:
        self.hits = hits

    def search(self, query: str, as_of: date | None = None, k: int = 5) -> list[Hit]:
        return self.hits


@pytest.fixture
def ruling_hits(conn: psycopg.Connection) -> list[Hit]:
    """Two hits from the test corpus: a law section as it is, and one whose stored wording
    is made a real ruling, found through one of its passages."""
    ingest(conn, FIXTURE, 20)
    build_chunks(conn, "structure")
    embed_chunks(conn, FAKE)
    law, other = Retriever(conn, FAKE).search(QUESTION, as_of=AS_OF, k=2)
    conn.execute("UPDATE unit_version SET text = %s WHERE id = %s", (RULING, other.version_id))
    ruling = dataclasses.replace(
        other,
        authority="ruling",
        heading="MIG 2024:12",
        text=PASSAGE,
        valid_from=date(2024, 10, 22),
    )
    return [law, ruling]


def test_the_setting_is_off_unless_it_says_on(monkeypatch: pytest.MonkeyPatch) -> None:
    assert answer_ruling_notes() is False  # nothing set (conftest removes the shell's value)
    assert AnswerConfig(model="m").ruling_notes is False
    for off in ("", " ", "0", "false", "False"):
        monkeypatch.setenv("ANSWER_RULING_NOTES", off)
        assert AnswerConfig(model="m").ruling_notes is False
    for on in ("1", "true", " True "):
        monkeypatch.setenv("ANSWER_RULING_NOTES", on)
        assert AnswerConfig(model="m").ruling_notes is True
    # A mistyped value is an error, not "off" and not "on".
    monkeypatch.setenv("ANSWER_RULING_NOTES", "yes")
    with pytest.raises(ValueError, match="ANSWER_RULING_NOTES"):
        AnswerConfig(model="m")
    monkeypatch.setenv("ANSWER_RULING_NOTES", "0")
    assert AnswerConfig(model="m", ruling_notes=True).ruling_notes is True  # given: not read


def test_the_answer_version_is_what_it_was_unless_the_notes_are_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "anthropic/claude-haiku-5.5"
    before = versions.answer_version(model)
    # The name as it was made before the setting existed: off must not rename a version.
    assert before == versions._name(
        model,
        model,
        "",
        "",
        versions.prompt_hash("answer.md"),
        versions.prompt_hash("intake.md"),
        versions.prompt_hash("translate.md"),
    )
    for off in ("0", "false", ""):
        monkeypatch.setenv("ANSWER_RULING_NOTES", off)
        assert versions.answer_version(model) == before
    monkeypatch.setenv("ANSWER_RULING_NOTES", "1")
    assert versions.answer_version(model) != before
    monkeypatch.delenv("ANSWER_RULING_NOTES")
    assert versions.answer_version(model) == before


@pytest.mark.db
def test_with_the_setting_off_the_context_is_the_one_from_before_the_notes(
    conn: psycopg.Connection, ruling_hits: list[Hit], monkeypatch: pytest.MonkeyPatch
) -> None:
    on = build_context(conn, ruling_hits, AS_OF, ruling_notes=True)
    assert [s.shown is not None for s in on.sources if s.role == "retrieved"] == [False, True]
    assert on.render().count(rp.OPEN) == NOTES >= 3  # (the fixture does give notes when on)

    def never(ruling: str, passage: str) -> str | None:
        raise AssertionError("the notes are off: a ruling's parts are not even looked for")

    monkeypatch.setattr(rp, "labelled", never)
    off = build_context(conn, ruling_hits, AS_OF)  # the default
    assert all(s.shown is None for s in off.sources)
    # Rendered as before the notes existed: every block is the source's own stored text.
    assert off.render() == "\n\n---\n\n".join(
        f"[{s.marker}] {s.heading}\ntype: {s.authority} | {s.role} | {s.dates}\n"
        f"id: {s.unit_id}\n{s.text}"
        for s in off.sources
    )
    assert rp.OPEN not in off.render() and rp.NOTE not in off.render()
    assert PASSAGE in off.render()
    # And on or off, the sources themselves are the same: only what the model reads differs.
    assert [dataclasses.replace(s, shown=None) for s in on.sources] == off.sources


@pytest.mark.db
def test_switching_the_setting_off_again_gives_the_same_request_as_before(
    conn: psycopg.Connection, ruling_hits: list[Hit], monkeypatch: pytest.MonkeyPatch
) -> None:
    def request_sent():
        llm = ScriptedLLM(json.dumps({"kind": "answered", "answer": "Svar [S1] [S2]."}))
        answer = Answerer(conn, Found(ruling_hits), llm, AnswerConfig(model="m", k=2)).answer(
            QUESTION, as_of=AS_OF
        )
        (request,) = llm.requests
        return request, answer

    before, answer_before = request_sent()  # nothing set
    assert rp.OPEN not in before.system and PASSAGE in before.system
    monkeypatch.setenv("ANSWER_RULING_NOTES", "1")
    with_notes, answer_on = request_sent()
    assert with_notes.system.count(rp.OPEN) == NOTES >= 3 and with_notes != before
    assert with_notes.system.replace(rp.labelled(RULING, PASSAGE) or "", PASSAGE) == before.system
    for off in ("0", None):  # set to off, and removed
        if off is None:
            monkeypatch.delenv("ANSWER_RULING_NOTES")
        else:
            monkeypatch.setenv("ANSWER_RULING_NOTES", off)
        again, answer_again = request_sent()
        assert again == before  # the whole request: prompt, messages, model, settings
        assert answer_again.sources == answer_before.sources
    # What leaves with an answer (its sources' text, its citations) never carries a note.
    assert [s.text for s in answer_on.sources] == [s.text for s in answer_before.sources]
    assert answer_on.citations == answer_before.citations


def test_every_ruling_in_the_corpus_keeps_the_rules() -> None:
    rulings = corpus_rulings()
    if not rulings:
        pytest.skip("no rulings in this database (CI checks the eval corpus with the command)")
    wrong = [
        f"{unit_id}: {problem}"
        for unit_id, text, bodies in rulings
        for problem in rp.problems(text, [text, *bodies])
    ]
    assert wrong == []
    with_parts = sum(rp.parts(text) is not None for _, text, _ in rulings)
    # A parser that has stopped recognising the reports would label almost nothing.
    assert with_parts >= rp.MIN_SHARE * len(rulings)
    court = (rp.APPELLATE, rp.APPELLATE_WHOLE, rp.ASSESSMENT, rp.HOLDING)
    for _, text, _ in rulings:
        made = rp.parts(text) or []
        for p in made:  # nothing of a dissent is ever under a note about the court itself
            if p.kind in court:
                assert "var skiljaktig" not in text[p.start : p.end]
        if any(p.kind == rp.LOWER_COURT for p in made):
            lower = [p for p in made if p.kind == rp.LOWER_COURT][0]
            appellate = [p for p in made if p.kind in (rp.APPELLATE, rp.APPELLATE_WHOLE)][0]
            assert lower.end <= appellate.start  # the lower court never runs into the higher
            assert "migrationsdomstolen (" in text[lower.start : lower.end]
            assert text[appellate.start :].startswith(
                "Kammarrätten i Stockholm, Migrationsöverdomstolen ("
            )
