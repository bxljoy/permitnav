"""Retriever.search and the retrieval eval, against a real Postgres with the fake embedder."""

from __future__ import annotations

from datetime import date

import psycopg
import pytest

from permitnav.corpus.indexing import build_chunks, embed_chunks
from permitnav.embedding.fake import FakeEmbedder
from permitnav.evals.golden import GoldenItem
from permitnav.evals.run import run_retrieval, slices
from permitnav.retrieval.retriever import RetrievalConfig, Retriever, units_applying_on
from tests.test_ingest import FIXTURE, S4, ingest

pytestmark = pytest.mark.db

# The fake stands in for bge-m3 here, so the retriever reads the bge-m3 table.
FAKE = FakeEmbedder(dims=1024, model_id="BAAI/bge-m3")


@pytest.fixture
def retriever(conn: psycopg.Connection) -> Retriever:
    ingest(conn, FIXTURE, 20)
    build_chunks(conn, "structure")
    embed_chunks(conn, FAKE)
    return Retriever(conn, FAKE)


def test_search_returns_the_matching_section_first(retriever: Retriever) -> None:
    hits = retriever.search("lönen medianlönen procent lönekravet", as_of=date(2026, 11, 20))
    assert hits[0].unit_id == "sfs:2005:716/6kap/2§"
    assert hits[0].heading == "6 kap. Arbetstillstånd, 2 §"
    assert hits[0].valid_from == date(2026, 6, 1)


def test_search_returns_each_unit_once(retriever: Retriever) -> None:
    hits = retriever.search("arbetstillstånd", as_of=date(2026, 11, 20), k=20)
    ids = [h.unit_id for h in hits]
    assert len(ids) == len(set(ids))


def test_search_only_returns_the_wording_that_applies_on_the_date(retriever: Retriever) -> None:
    query = "utlänning ansökt beviljats tillstånd före inresan"
    today = {h.unit_id: h for h in retriever.search(query, as_of=date(2026, 11, 20), k=50)}
    later = {h.unit_id: h for h in retriever.search(query, as_of=date(2028, 2, 1), k=50)}
    assert today[S4].valid_to == date(2028, 1, 1)
    assert later[S4].valid_from == date(2028, 1, 1)


def test_pending_and_withdrawn_wordings_are_never_returned(
    conn: psycopg.Connection, retriever: Retriever
) -> None:
    conn.execute("UPDATE unit_version SET withdrawn = true WHERE unit_id = 'sfs:2005:716/6kap/2§'")
    hits = retriever.search("lönen medianlönen procent lönekravet", as_of=date(2026, 11, 20), k=50)
    assert "sfs:2005:716/6kap/2§" not in {h.unit_id for h in hits}


def test_only_vector_mode_exists_in_phase_1(conn: psycopg.Connection) -> None:
    with pytest.raises(ValueError, match="phase 2"):
        Retriever(conn, FAKE, RetrievalConfig(mode="hybrid"))


def item(item_id: str, question: str, expected, language: str = "en") -> GoldenItem:
    return GoldenItem(
        id=item_id,
        question=question,
        original_language=language,
        translation_of=None,
        type="lookup",
        as_of=date(2026, 11, 20),
        expected=tuple(tuple(g) for g in expected),
        review="draft",
        authority="law",
    )


def test_run_scores_indexed_items_and_explains_the_rest(
    conn: psycopg.Connection, retriever: Retriever
) -> None:
    existing = dict(conn.execute("SELECT id, document FROM unit").fetchall())
    items = [
        item("salary", "lönen medianlönen procent lönekravet", [["sfs:2005:716/6kap/2§"]]),
        item("oos", "asyl", []),
        item("web", "facket", [["web:tbd/union-statement"]]),
        item("put", "permanent", [["sfs:2005:716/5kap/5§"]]),  # not in the fixture excerpt
        item("typo", "x", [["sfs:2005:716/6kap/99§"]]),  # indexed law, no such section
    ]
    run = run_retrieval(items, retriever, existing, lambda d: units_applying_on(conn, d))
    assert [r.item.id for r in run.results] == ["salary"]
    assert run.results[0].ranks == (1,)
    assert set(run.skipped) == {
        "out of scope (no sources to retrieve)",
        "cites a Migrationsverket page not indexed yet",
    }
    assert run.unknown_units == {
        "put": ["sfs:2005:716/5kap/5§"],
        "typo": ["sfs:2005:716/6kap/99§"],
    }
    (all_items,) = [s for s in slices(run) if s.name == "all"]
    assert (all_items.n, all_items.recall_1) == (1, 1.0)


# --- regressions from the slice 4 code review (2026-11-22) -------------------------------------


def test_search_returns_k_units_even_when_many_chunks_belong_to_few_units(
    conn: psycopg.Connection,
) -> None:
    ingest(conn, FIXTURE, 20)
    build_chunks(conn, "fixed500")
    embed_chunks(conn, FAKE)
    retriever = Retriever(conn, FAKE, RetrievalConfig(strategy="fixed500"))
    applying = units_applying_on(conn, date(2026, 11, 20))
    hits = retriever.search("arbetstillstånd", as_of=date(2026, 11, 20), k=60)
    assert len(hits) == min(60, len(applying))


def test_pending_starts_are_hidden_and_pending_ends_still_apply(
    conn: psycopg.Connection, retriever: Retriever
) -> None:
    query = "lönen medianlönen procent lönekravet"
    s2 = "sfs:2005:716/6kap/2§"
    conn.execute("UPDATE unit_version SET pending = 'ends' WHERE unit_id = %s", (s2,))
    assert s2 in {h.unit_id for h in retriever.search(query, as_of=date(2026, 11, 20), k=50)}
    conn.execute("UPDATE unit_version SET pending = 'starts' WHERE unit_id = %s", (s2,))
    assert s2 not in {h.unit_id for h in retriever.search(query, as_of=date(2026, 11, 20), k=50)}


def test_a_placeholder_alternative_does_not_block_scoring(
    conn: psycopg.Connection, retriever: Retriever
) -> None:
    existing = dict(conn.execute("SELECT id, document FROM unit").fetchall())
    items = [
        item(
            "alt",
            "lönen medianlönen procent lönekravet",
            [["sfs:2005:716/6kap/2§", "web:tbd/salary"]],
        ),
        item("typo-with-page", "x", [["sfs:2005:716/6kap/99§"], ["web:tbd/x"]]),
    ]
    run = run_retrieval(items, retriever, existing, lambda d: units_applying_on(conn, d))
    assert [r.item.id for r in run.results] == ["alt"]
    assert run.unknown_units == {"typo-with-page": ["sfs:2005:716/6kap/99§"]}


def test_an_item_whose_wording_does_not_apply_on_its_date_is_skipped_not_missed(
    conn: psycopg.Connection, retriever: Retriever
) -> None:
    existing = dict(conn.execute("SELECT id, document FROM unit").fetchall())
    old = item("old", "lönen", [["sfs:2005:716/6kap/2§"]])
    old = GoldenItem(**{**old.__dict__, "as_of": date(2024, 1, 1)})  # before 2026-06-01
    run = run_retrieval([old], retriever, existing, lambda d: units_applying_on(conn, d))
    assert run.results == []
    assert list(run.skipped) == [
        "an expected wording does not apply on the item's as_of (history not rebuilt)"
    ]


def test_web_and_position_paper_ids_are_not_mistaken_for_golden_set_errors(
    conn: psycopg.Connection, retriever: Retriever
) -> None:
    existing = dict(conn.execute("SELECT id, document FROM unit").fetchall())
    existing["web:sv/arbeta-i-sverige#krav"] = "https://www.migrationsverket.se/arbeta"
    page = item("page", "x", [["web:sv/annan-sida#lon"]])
    run = run_retrieval([page], retriever, existing, lambda d: units_applying_on(conn, d))
    assert run.unknown_units == {}
    assert list(run.skipped) == ["cites a source type not indexed yet"]


def test_results_are_sliced_by_authority(conn: psycopg.Connection, retriever: Retriever) -> None:
    existing = dict(conn.execute("SELECT id, document FROM unit").fetchall())
    run = run_retrieval(
        [item("salary", "lönen medianlönen", [["sfs:2005:716/6kap/2§"]])],
        retriever,
        existing,
        lambda d: units_applying_on(conn, d),
    )
    assert "authority: law" in {s.name for s in slices(run)}


def test_interleaving_keeps_law_in_the_results_when_pages_match_better(
    conn: psycopg.Connection, retriever: Retriever
) -> None:
    from permitnav.corpus.store import ingest_page
    from permitnav.parsing.webpage import parse_page
    from tests.test_webpage import FIXTURE as PAGE
    from tests.test_webpage import SV, raw_page

    ingest_page(conn, raw_page(PAGE, 20), parse_page(PAGE, SV))
    build_chunks(conn, "structure")
    embed_chunks(conn, FAKE)
    query = "arbetstillstånd lönekravet månadslön kronor försäkringar"
    mixed = retriever.search(query, as_of=date(2026, 11, 21), k=4)
    kinds = [h.authority for h in mixed]
    assert "web_page" in kinds and "law" in kinds
    assert kinds[0] != kinds[1]  # alternating
    flat = Retriever(conn, FAKE, RetrievalConfig(mix="none")).search(
        query, as_of=date(2026, 11, 21), k=4
    )
    assert len(flat) == 4


def test_identical_sections_appear_once_and_count_for_each_other(
    conn: psycopg.Connection, retriever: Retriever
) -> None:
    from permitnav.corpus.store import ingest_page
    from permitnav.parsing.webpage import parse_page
    from permitnav.retrieval.retriever import units_with_same_text
    from tests.test_webpage import FIXTURE as PAGE
    from tests.test_webpage import SV, raw_page

    copy_url = SV.replace("/anstalld.html", "/artist.html")
    ingest_page(conn, raw_page(PAGE, 20), parse_page(PAGE, SV))
    ingest_page(conn, raw_page(PAGE, 20, url=copy_url), parse_page(PAGE, copy_url))
    build_chunks(conn, "structure")
    embed_chunks(conn, FAKE)
    hits = retriever.search(
        "fackförbund erbjudandet anställning yttra", as_of=date(2026, 11, 21), k=10
    )
    anchor = "#fackforbundetfarmojlighetattyttrasig"
    assert len([h for h in hits if h.unit_id.endswith(anchor)]) == 1  # the copy takes no slot
    original = "web:sv/du-vill-ansoka/arbeta/anstalld-eller-egen-foretagare/anstalld" + anchor
    copy = original.replace("/anstalld#", "/artist#")
    assert units_with_same_text(conn, original, date(2026, 11, 21)) == {copy}


def test_source_types_outside_the_named_groups_can_still_be_found(
    conn: psycopg.Connection, retriever: Retriever
) -> None:
    conn.execute("UPDATE unit SET source_type = 'ruling' WHERE id = 'sfs:2005:716/6kap/2§'")
    hits = retriever.search("lönen medianlönen procent lönekravet", as_of=date(2026, 11, 20), k=5)
    assert "sfs:2005:716/6kap/2§" in {h.unit_id for h in hits}


def test_a_mistyped_page_section_is_a_golden_set_error(
    conn: psycopg.Connection, retriever: Retriever
) -> None:
    from permitnav.corpus.store import ingest_page
    from permitnav.parsing.webpage import parse_page
    from tests.test_webpage import BASE, SV, raw_page
    from tests.test_webpage import FIXTURE as PAGE

    ingest_page(conn, raw_page(PAGE, 20), parse_page(PAGE, SV))
    existing = dict(conn.execute("SELECT id, document FROM unit").fetchall())
    typo = item("typo", "x", [[f"{BASE}#lonekrav"]])
    run = run_retrieval([typo], retriever, existing, lambda d: units_applying_on(conn, d))
    assert run.unknown_units == {"typo": [f"{BASE}#lonekrav"]}
