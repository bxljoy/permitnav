"""Building chunks and embeddings (corpus.indexing), against a real Postgres."""

from __future__ import annotations

import psycopg
import pytest

from permitnav.corpus.indexing import build_chunks, embed_chunks
from permitnav.embedding.fake import FakeEmbedder
from tests.test_ingest import FIXTURE, count, ingest

pytestmark = pytest.mark.db
TABLE = "chunk_embedding_bge_m3"


def test_every_version_gets_structure_chunks_once(conn: psycopg.Connection) -> None:
    ingest(conn, FIXTURE, 20)
    versions = count(conn, "SELECT count(*) FROM unit_version")
    created = build_chunks(conn, "structure")
    assert created >= versions
    assert count(conn, "SELECT count(DISTINCT version_id) FROM chunk") == versions
    assert build_chunks(conn, "structure") == 0  # incremental


def test_chunks_are_keyword_searchable_in_swedish(conn: psycopg.Connection) -> None:
    ingest(conn, FIXTURE, 20)
    build_chunks(conn, "structure")
    # Swedish stemming maps "medianlönen" to "medianlön". (It does not map every definite
    # form: "anställningsavtalet" stays as it is; see DESIGN.md open points.)
    hits = conn.execute(
        "SELECT c.text FROM chunk c WHERE c.tsv @@ plainto_tsquery('swedish', 'medianlönen')"
    ).fetchall()
    assert any("6 kap. Arbetstillstånd, 2 §" in row[0] for row in hits)


def test_both_strategies_coexist(conn: psycopg.Connection) -> None:
    ingest(conn, FIXTURE, 20)
    build_chunks(conn, "structure")
    build_chunks(conn, "fixed500")
    strategies = {row[0] for row in conn.execute("SELECT DISTINCT strategy FROM chunk").fetchall()}
    assert strategies == {"structure", "fixed500"}


def test_embedding_is_incremental_and_vectors_have_the_models_size(
    conn: psycopg.Connection,
) -> None:
    ingest(conn, FIXTURE, 20)
    chunks = build_chunks(conn, "structure")
    fake = FakeEmbedder(dims=1024)
    batches = []
    assert (
        embed_chunks(
            conn,
            fake,
            table=TABLE,
            batch_size=10,
            on_batch=lambda d, t: batches.append(d),
            allow_foreign_model=True,
        )
        == chunks
    )
    assert batches[-1] == chunks
    assert count(conn, f"SELECT count(*) FROM {TABLE}") == chunks
    assert count(conn, f"SELECT min(vector_dims(embedding)) FROM {TABLE}") == 1024
    assert embed_chunks(conn, fake, table=TABLE, allow_foreign_model=True) == 0


def test_nearest_chunk_by_vector_is_the_matching_section(conn: psycopg.Connection) -> None:
    ingest(conn, FIXTURE, 20)
    build_chunks(conn, "structure")
    fake = FakeEmbedder(dims=1024)
    embed_chunks(conn, fake, table=TABLE, allow_foreign_model=True)
    from permitnav.corpus.indexing import to_pgvector

    query = to_pgvector(fake.embed_query("lönen medianlönen procent lönekravet"))
    (text,) = conn.execute(
        f"SELECT c.text FROM {TABLE} e JOIN chunk c ON c.id = e.chunk_id"
        " ORDER BY e.embedding <=> %s::vector LIMIT 1",
        (query,),
    ).fetchone()  # type: ignore[misc]
    assert text.startswith("6 kap. Arbetstillstånd, 2 §")


def test_a_wrong_vector_size_is_rejected(conn: psycopg.Connection) -> None:
    ingest(conn, FIXTURE, 20)
    build_chunks(conn, "structure")
    with pytest.raises(ValueError, match="wrong size"):

        class Broken(FakeEmbedder):
            def embed_documents(self, texts: list[str]) -> list[list[float]]:
                return [[0.0] * 3 for _ in texts]

        embed_chunks(conn, Broken(dims=1024), table=TABLE, allow_foreign_model=True)


# --- regressions from the slice 3 code review (2026-11-22) -------------------------------------


def test_another_models_vectors_are_refused_for_the_bge_m3_table(conn: psycopg.Connection) -> None:
    ingest(conn, FIXTURE, 20)
    build_chunks(conn, "structure")
    with pytest.raises(ValueError, match="holds BAAI/bge-m3 vectors"):
        embed_chunks(conn, FakeEmbedder(dims=1024), table=TABLE)


def test_nan_vectors_are_rejected_before_writing(conn: psycopg.Connection) -> None:
    ingest(conn, FIXTURE, 20)
    build_chunks(conn, "structure")

    class Overflow(FakeEmbedder):
        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return [[float("nan")] * 1024 for _ in texts]

    with pytest.raises(ValueError, match="NaN/inf for chunk"):
        embed_chunks(conn, Overflow(dims=1024), table=TABLE, allow_foreign_model=True)


def test_an_interrupted_embedding_run_keeps_the_batches_it_finished(
    fresh_database_url: str,
) -> None:
    with psycopg.connect(fresh_database_url) as setup:
        ingest(setup, FIXTURE, 20)
        build_chunks(setup, "structure")

    class FailsOnSecondBatch(FakeEmbedder):
        calls = 0

        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            self.calls += 1
            if self.calls == 2:
                raise KeyboardInterrupt
            return super().embed_documents(texts)

    with pytest.raises(KeyboardInterrupt), psycopg.connect(fresh_database_url) as conn:
        embed_chunks(
            conn, FailsOnSecondBatch(), table=TABLE, batch_size=5, allow_foreign_model=True
        )
    with psycopg.connect(fresh_database_url) as check:
        assert count(check, f"SELECT count(*) FROM {TABLE}") == 5


def test_index_without_the_embed_extra_prints_the_install_hint(
    fresh_database_url: str, monkeypatch, capsys
) -> None:
    import sys

    from permitnav import cli

    monkeypatch.setenv("DATABASE_URL", fresh_database_url)
    monkeypatch.setitem(sys.modules, "torch", None)  # import torch -> ImportError
    assert cli.main(["index"]) == 1
    assert "uv sync --extra embed" in capsys.readouterr().out
