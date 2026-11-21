"""Reranking (phase 2, slice 6)."""

from __future__ import annotations

from datetime import date

import psycopg
import pytest

from permitnav.corpus.indexing import build_chunks, embed_chunks
from permitnav.reranking.fake import FakeReranker
from permitnav.retrieval.retriever import RetrievalConfig, Retriever
from tests.test_ingest import FIXTURE, ingest
from tests.test_retriever import FAKE

pytestmark = pytest.mark.db


@pytest.fixture
def corpus(conn: psycopg.Connection) -> None:
    ingest(conn, FIXTURE, 20)
    build_chunks(conn, "structure")
    embed_chunks(conn, FAKE)


class Prefers:
    """A reranker that puts one unit's text first, whatever vector search said."""

    model_id = "prefers"

    def __init__(self, phrase: str) -> None:
        self.phrase = phrase

    def score(self, question: str, texts: list[str]) -> list[float]:
        return [1.0 if self.phrase in text else 0.0 for text in texts]


def test_the_reranker_reorders_the_candidates(conn: psycopg.Connection, corpus: None) -> None:
    query = "arbetstillstånd"
    plain = Retriever(conn, FAKE, RetrievalConfig(mode="vector"))
    before = [h.unit_id for h in plain.search(query, date(2026, 11, 20), k=5)]
    phrase = "Regeringen får meddela föreskrifter om att en ansökan om"  # 6 kap. 8 §
    config = RetrievalConfig(mode="vector", rerank=True, rerank_depth=30)
    reranked = Retriever(conn, FAKE, config, reranker=Prefers(phrase))
    hits = reranked.search(query, date(2026, 11, 20), k=5)
    assert before[0] != "sfs:2005:716/6kap/8§"
    assert hits[0].unit_id == "sfs:2005:716/6kap/8§"
    assert hits[0].rerank_score == 1.0
    assert hits[0].score == pytest.approx(  # the cosine score is kept
        {h.unit_id: h.score for h in plain.search(query, date(2026, 11, 20), k=50)}[
            "sfs:2005:716/6kap/8§"
        ]
    )


def test_only_the_top_candidates_are_reranked(conn: psycopg.Connection, corpus: None) -> None:
    reranker = FakeReranker()
    config = RetrievalConfig(mode="hybrid", rerank=True, rerank_depth=7)
    Retriever(conn, FAKE, config, reranker=reranker).search("lönen", date(2026, 11, 20), k=3)
    assert reranker.calls and all(n <= 7 for _, n in reranker.calls)


def test_named_sections_stay_first_when_reranking(conn: psycopg.Connection, corpus: None) -> None:
    config = RetrievalConfig(mode="hybrid", rerank=True)
    searcher = Retriever(conn, FAKE, config, reranker=Prefers("medianlönen"))
    hits = searcher.search("Vad säger 6 kap. 3 § utlänningslagen?", date(2026, 11, 20))
    assert hits[0].unit_id == "sfs:2005:716/6kap/3§"


def test_one_ranking_by_the_reranker(conn: psycopg.Connection, corpus: None) -> None:
    config = RetrievalConfig(mode="vector", rerank=True, mix="rerank")
    phrase = "Beslut om arbetstillstånd meddelas av Migrationsverket"
    hits = Retriever(conn, FAKE, config, reranker=Prefers(phrase)).search(
        "arbetstillstånd", date(2026, 11, 20), k=5
    )
    assert hits[0].unit_id == "sfs:2005:716/6kap/5§"
    scores = [h.rerank_score or 0.0 for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_reranking_needs_a_reranker(conn: psycopg.Connection) -> None:
    with pytest.raises(ValueError, match="reranker"):
        Retriever(conn, FAKE, RetrievalConfig(rerank=True))
    with pytest.raises(ValueError, match="rerank"):
        Retriever(conn, FAKE, RetrievalConfig(mix="rerank"), reranker=FakeReranker())


def test_candidates_past_the_rerank_depth_are_kept_after_the_reranked_ones(
    conn: psycopg.Connection, corpus: None
) -> None:
    config = RetrievalConfig(mode="vector", rerank=True, rerank_depth=2, mix="none")
    hits = Retriever(conn, FAKE, config, reranker=FakeReranker()).search(
        "arbetstillstånd", date(2026, 11, 20), k=5
    )
    assert len(hits) == 5
    assert [h.rerank_score is None for h in hits] == [False, False, True, True, True]


def test_the_law_is_ordered_by_the_translation_in_a_reranked_mix(
    conn: psycopg.Connection, corpus: None
) -> None:
    from tests.test_translate import Dictionary

    words = Dictionary({"Which salary?": "lönen medianlönen"})
    config = RetrievalConfig(
        mode="vector", translate=True, translated_vector=True, rerank=True, mix="rerank"
    )
    Retriever(conn, FAKE, config, words, FakeReranker()).search("Which salary?", date(2026, 11, 20))
    assert words.asked == ["Which salary?"]


def test_unscored_hits_sort_after_negative_scores() -> None:
    import dataclasses

    from permitnav.retrieval.retriever import _by_rerank_score
    from tests.test_variants import hit

    hits = [
        dataclasses.replace(hit("a"), rerank_score=None),
        dataclasses.replace(hit("b"), rerank_score=-3.0),
        dataclasses.replace(hit("c"), rerank_score=2.0),
    ]
    assert [h.unit_id for h in sorted(hits, key=_by_rerank_score)] == ["c", "b", "a"]


def test_rerank_options_are_checked_before_any_model_loads() -> None:
    import argparse

    from permitnav.cli import _add_retrieval_options, _retrieval_config

    parser = argparse.ArgumentParser()
    _add_retrieval_options(parser)
    with pytest.raises(SystemExit, match="--mix rerank needs --rerank"):
        _retrieval_config(parser.parse_args(["--mix", "rerank"]))
    with pytest.raises(SystemExit, match="at least 1"):
        _retrieval_config(parser.parse_args(["--rerank", "--rerank-depth", "0"]))
