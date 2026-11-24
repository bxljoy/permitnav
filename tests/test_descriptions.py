"""Plain-language descriptions of law sections (phase 5 experiment): used only to find a
section, never shown as its text; ignored entirely unless retrieval asks for them."""

from __future__ import annotations

import dataclasses
from datetime import date

import psycopg
import pytest

from permitnav.answering.context import build_context
from permitnav.corpus.descriptions import Wording, describe, split, store, undescribed
from permitnav.corpus.indexing import build_chunks, embed_chunks
from permitnav.llm.fake import ScriptedLLM
from permitnav.llm.port import CompletionRequest
from permitnav.retrieval.retriever import Retriever
from tests.test_ingest import FIXTURE, ingest
from tests.test_retriever import FAKE, retriever  # noqa: F401  (fixture)

AS_OF = date(2026, 11, 20)
UNIT = "sfs:2005:716/6kap/4§"
WORDS = "flamingo zebra giraff"  # in no law text: findable only through the description


def current_version(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "SELECT id FROM unit_version WHERE unit_id = %s AND NOT withdrawn"
        " AND (valid_from IS NULL OR valid_from <= %s) AND (valid_to IS NULL OR valid_to > %s)"
        " ORDER BY valid_from DESC NULLS LAST LIMIT 1",
        (UNIT, AS_OF, AS_OF),
    ).fetchone()
    assert row is not None
    return row[0]


def describe_unit(conn: psycopg.Connection) -> None:
    w = Wording(current_version(conn), UNIT, "6 kap. Arbetstillstånd, 4 §", "text", ())
    store(conn, w, (f"Svenska {WORDS}.", f"English {WORDS}."), "m", "hash")
    embed_chunks(conn, FAKE)


@pytest.mark.db
def test_off_by_default_nothing_changes(conn, retriever) -> None:  # noqa: F811
    def ranked(query: str) -> list[tuple[str, float]]:
        return [(h.unit_id, round(h.score, 6)) for h in retriever.search(query, AS_OF, 8)]

    before = ranked("lönen medianlönen procent")
    describe_unit(conn)
    assert ranked("lönen medianlönen procent") == before  # keyword statistics included
    assert not any(h.via_description for h in retriever.search(WORDS, AS_OF, 20))


@pytest.mark.db
def test_a_section_found_through_its_description_keeps_its_own_text(conn, retriever) -> None:  # noqa: F811
    describe_unit(conn)
    on = Retriever(conn, FAKE, dataclasses.replace(retriever.config, description_weight=1.0))
    assert retriever.search(WORDS, AS_OF, 1)[0].unit_id != UNIT  # weight 0: no help
    hit = on.search(WORDS, AS_OF, 1)[0]
    assert hit.unit_id == UNIT  # lifted by its description's own ranking
    assert "flamingo" not in hit.text and hit.text.startswith("6 kap.")  # the law's own text
    assert "flamingo" not in build_context(conn, [hit], AS_OF).render()  # never shown


@pytest.mark.db
def test_a_described_wording_still_gets_its_text_chunks(conn) -> None:
    ingest(conn, FIXTURE, 20)
    vid = current_version(conn)
    store(conn, Wording(vid, UNIT, "h", "t", ()), ("sv", "en"), "m", "hash")  # before indexing
    build_chunks(conn, "structure")
    kinds = {k for (k,) in conn.execute("SELECT kind FROM chunk WHERE version_id = %s", (vid,))}
    assert kinds == {"text", "description"}


def test_only_a_clean_two_part_reply_is_indexed() -> None:
    assert split("Svenska.\n---\nEnglish.") == ("Svenska.", "English.")
    assert split("No separator") is None
    assert split("A\n---\nB\n---\nRevised: C") is None  # a second version
    assert split("A\n---\n") is None
    assert split("A" * 1000 + "\n---\nB") is None  # notes crept in


@pytest.mark.db
def test_describe_stores_usable_replies_and_reports_the_rest(conn) -> None:
    ingest(conn, FIXTURE, 20)
    wordings = undescribed(conn)[:6]
    bad = wordings[0].unit_id

    def reply(request: CompletionRequest) -> str:
        first = f"Provision: {wordings[0].heading}\n\n{wordings[0].text}\n\n"
        if request.messages[0]["content"].startswith(first):
            return "Hmm, let me think about this."
        return "Svenska.\n---\nEnglish."

    stored, unusable, _ = describe(conn, ScriptedLLM(reply), "m", wordings, workers=2)
    assert unusable == [bad] and stored == len(wordings) - 1
    left = {w.version_id for w in undescribed(conn)}
    assert wordings[0].version_id in left  # retried next run
    assert not left & {w.version_id for w in wordings[1:]}
