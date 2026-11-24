"""Building chunks and embeddings for stored versions. Both steps are incremental: they only
work on versions without chunks, and chunks without a vector, so re-running is cheap."""

from __future__ import annotations

import math
from collections.abc import Callable

import psycopg
from psycopg import sql

from permitnav.corpus.chunking import STRATEGIES, chunk_text
from permitnav.embedding.port import Embedder

# One table per model, because dimensions differ and pgvector indexes are per column.
EMBEDDING_TABLES = {"BAAI/bge-m3": "chunk_embedding_bge_m3"}
TS_CONFIG = {"sv": "swedish", "en": "english"}


def build_chunks(conn: psycopg.Connection, strategy: str) -> int:
    """Chunk every version that has no chunks for this strategy yet. Returns chunks created."""
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown chunking strategy: {strategy!r}")
    with conn.transaction():
        rows = conn.execute(
            """
            SELECT v.id, u.heading, v.text, u.language
            FROM unit_version v JOIN unit u ON u.id = v.unit_id
            WHERE NOT EXISTS (
              SELECT 1 FROM chunk c WHERE c.version_id = v.id AND c.strategy = %s
                AND c.kind = 'text')
            ORDER BY v.id
            """,
            (strategy,),
        ).fetchall()
        records = [
            (version_id, strategy, ordinal, text, TS_CONFIG[language])
            for version_id, heading, body, language in rows
            for ordinal, text in enumerate(chunk_text(strategy, heading, body))
        ]
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO chunk (version_id, strategy, ordinal, text, ts_config)"
                " VALUES (%s, %s, %s, %s, %s::regconfig)",
                records,
            )
    return len(records)


def embed_chunks(
    conn: psycopg.Connection,
    embedder: Embedder,
    table: str | None = None,
    batch_size: int = 64,
    on_batch: Callable[[int, int], None] | None = None,
    allow_foreign_model: bool = False,
) -> int:
    """Embed every chunk that has no vector in the model's table yet. Returns chunks embedded.

    Each batch is its own transaction (on an idle connection), so an interrupted run keeps
    what it finished. A table belongs to one model: writing another model's vectors into it
    would make a later real run skip those chunks, so that is refused unless
    `allow_foreign_model` (tests only).
    """
    owner = {t: m for m, t in EMBEDDING_TABLES.items()}
    table = table or EMBEDDING_TABLES.get(embedder.model_id)
    if table is None:
        raise ValueError(f"no embedding table for model {embedder.model_id!r}")
    if owner.get(table, embedder.model_id) != embedder.model_id and not allow_foreign_model:
        raise ValueError(f"{table} holds {owner[table]} vectors, not {embedder.model_id}")
    missing = sql.SQL(
        "SELECT c.id, c.text FROM chunk c WHERE NOT EXISTS"
        " (SELECT 1 FROM {table} e WHERE e.chunk_id = c.id) ORDER BY c.id"
    ).format(table=sql.Identifier(table))
    with conn.transaction():  # end the read, so each batch below is a real transaction
        todo = conn.execute(missing).fetchall()
    insert = sql.SQL("INSERT INTO {table} (chunk_id, embedding) VALUES (%s, %s::vector)").format(
        table=sql.Identifier(table)
    )
    done = 0
    for start in range(0, len(todo), batch_size):
        batch = todo[start : start + batch_size]
        vectors = embedder.embed_documents([text for _, text in batch])
        for (chunk_id, _), vector in zip(batch, vectors, strict=True):
            if len(vector) != embedder.dims:
                raise ValueError(f"{embedder.model_id} returned vectors of the wrong size")
            if not all(math.isfinite(x) for x in vector):  # e.g. fp16 overflow on the GPU
                raise ValueError(f"{embedder.model_id} returned NaN/inf for chunk {chunk_id}")
        with conn.transaction(), conn.cursor() as cur:
            cur.executemany(
                insert,
                [
                    (chunk_id, to_pgvector(v))
                    for (chunk_id, _), v in zip(batch, vectors, strict=True)
                ],
            )
        done += len(batch)
        if on_batch:
            on_batch(done, len(todo))
    return done


def to_pgvector(vector: list[float]) -> str:
    """pgvector's text format, so no extra Python package is needed."""
    return "[" + ",".join(f"{x:.7g}" for x in vector) + "]"
