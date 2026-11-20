"""`Retriever.search(query, as_of, k)` (docs/DESIGN.md, "Phase 1 internals").

Phase 1: vector search only. Phase 2 adds keyword search, fusion, reranking and
authority-aware ranking behind the same interface, selected by `RetrievalConfig.mode`.

Returns citable units, not chunks: when several chunks of one unit match, the best one
represents the unit (D2). Only wordings that apply on `as_of` are searched (D8, D5):
wordings waiting for a government decision, and withdrawn ones, never are.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import psycopg
from psycopg import sql

from permitnav.corpus.indexing import EMBEDDING_TABLES, to_pgvector
from permitnav.embedding.port import Embedder


@dataclass(frozen=True)
class Hit:
    unit_id: str
    version_id: int
    chunk_id: int
    text: str
    score: float  # cosine similarity, higher is better
    authority: str
    heading: str
    url: str
    valid_from: date | None
    valid_to: date | None
    fetched_at: datetime  # when the crawler first saw this wording


# Which wordings apply on a date (D8, D5). Web pages are dated by when the crawler saw them,
# in Swedish time, so the answer does not depend on the database session's time zone.
APPLIES_ON = """
    v.pending IS DISTINCT FROM 'starts'
    AND NOT v.withdrawn
    AND (
      (v.time_basis = 'valid'
        AND (v.valid_from IS NULL OR v.valid_from <= %(d)s)
        AND (v.valid_to IS NULL OR v.valid_to > %(d)s))
      OR (v.time_basis = 'observed'
        AND (v.observed_from AT TIME ZONE 'Europe/Stockholm')::date <= %(d)s
        AND (v.observed_to IS NULL
             OR (v.observed_to AT TIME ZONE 'Europe/Stockholm')::date > %(d)s))
    )
"""


# Source groups for mixing results: legal text, Migrationsverket's guidance (D22), and a
# catch-all ("other": every other source type, e.g. rulings and position papers later).
SOURCE_GROUPS: dict[str, tuple[str, ...]] = {
    "law": ("statute", "transitional"),
    "guidance": ("web_page",),
}
LISTED_TYPES = tuple(t for types in SOURCE_GROUPS.values() for t in types)


@dataclass(frozen=True)
class RetrievalConfig:
    mode: str = "vector"  # phase 2: "hybrid", "hybrid_rerank"
    strategy: str = "structure"  # which chunks to search
    # "interleave": search law and guidance separately and alternate the results, so pages in
    # the question's language cannot push the (Swedish) law out of the top k. "none": one
    # ranking over everything. Phase 2 replaces this with authority-aware fused ranking.
    mix: str = "interleave"


class Retriever:
    def __init__(
        self, conn: psycopg.Connection, embedder: Embedder, config: RetrievalConfig | None = None
    ) -> None:
        self._conn = conn
        self._embedder = embedder
        self.config = config or RetrievalConfig()
        if self.config.mode != "vector":
            raise ValueError(f"retrieval mode {self.config.mode!r} arrives in phase 2")
        table = EMBEDDING_TABLES.get(embedder.model_id)
        if table is None:
            raise ValueError(f"no embedding table for model {embedder.model_id!r}")
        self._table = table

    def search(self, query: str, as_of: date | None = None, k: int = 5) -> list[Hit]:
        """The k best units, each represented by its best-matching chunk. Units with
        identical text (Migrationsverket repeats sections across pages) appear once.

        Exact search: every chunk that passes the filters is scored, then the best chunk per
        unit is kept. (An approximate HNSW scan returns its nearest rows before filtering,
        so filters could leave fewer than k; at this corpus size exact search is fast.)
        """
        as_of = as_of or date.today()
        vector = to_pgvector(self._embedder.embed_query(query))
        if self.config.mix == "none":
            return self._search(vector, as_of, k, None)
        if self.config.mix != "interleave":
            raise ValueError(f"unknown mix {self.config.mix!r}")
        rankings = [self._search(vector, as_of, k, types) for types in SOURCE_GROUPS.values()]
        rankings.append(self._search(vector, as_of, k, LISTED_TYPES, exclude=True))
        rankings = [hits for hits in rankings if hits]
        rankings.sort(key=lambda hits: -(hits[0].score if hits else 0.0))  # best group first
        merged: list[Hit] = []
        for position in range(k):
            for hits in rankings:
                if position < len(hits) and len(merged) < k:
                    merged.append(hits[position])
        return merged

    def _search(
        self,
        vector: str,
        as_of: date,
        k: int,
        source_types: tuple[str, ...] | None,
        exclude: bool = False,
    ) -> list[Hit]:
        rows = self._conn.execute(
            sql.SQL(
                """
                WITH scored AS MATERIALIZED (
                  SELECT u.id AS unit_id, v.id AS version_id, c.id AS chunk_id, c.text,
                         v.text_hash,
                         e.embedding <=> %(q)s::vector AS distance,
                         u.authority, u.heading, u.url, v.valid_from, v.valid_to, v.observed_from
                  FROM {table} e
                  JOIN chunk c ON c.id = e.chunk_id
                  JOIN unit_version v ON v.id = c.version_id
                  JOIN unit u ON u.id = v.unit_id
                  WHERE c.strategy = %(strategy)s AND {applies}
                    AND (%(types)s::text[] IS NULL
                         OR (u.source_type = ANY(%(types)s::text[])) <> %(exclude)s)
                ),
                best_per_unit AS (
                  SELECT DISTINCT ON (unit_id) * FROM scored ORDER BY unit_id, distance, chunk_id
                ),
                -- The site repeats identical sections on many pages; keep one per text.
                best AS (
                  SELECT DISTINCT ON (text_hash) * FROM best_per_unit
                  ORDER BY text_hash, distance, unit_id
                )
                SELECT unit_id, version_id, chunk_id, text, 1 - distance, authority, heading,
                       url, valid_from, valid_to, observed_from
                FROM best ORDER BY distance, unit_id LIMIT %(k)s
                """
            ).format(table=sql.Identifier(self._table), applies=sql.SQL(APPLIES_ON)),  # pyright: ignore[reportArgumentType]
            {
                "q": vector,
                "strategy": self.config.strategy,
                "d": as_of,
                "k": k,
                "types": list(source_types) if source_types else None,
                "exclude": exclude,
            },
        ).fetchall()
        return [Hit(*row) for row in rows]


def units_with_same_text(conn: psycopg.Connection, unit_id: str, as_of: date) -> set[str]:
    """Other units whose wording on a date is identical to this unit's."""
    rows = conn.execute(
        sql.SQL(
            """
            SELECT DISTINCT v.unit_id FROM unit_version v
            WHERE {applies} AND v.unit_id <> %(u)s AND v.text_hash IN (
              SELECT v.text_hash FROM unit_version v WHERE v.unit_id = %(u)s AND {applies})
            """
        ).format(applies=sql.SQL(APPLIES_ON)),  # pyright: ignore[reportArgumentType]
        {"u": unit_id, "d": as_of},
    ).fetchall()
    return {row[0] for row in rows}


def units_applying_on(conn: psycopg.Connection, as_of: date) -> set[str]:
    """Units that have a wording applying on a date: the only ones search can return."""
    rows = conn.execute(
        sql.SQL("SELECT DISTINCT v.unit_id FROM unit_version v WHERE {applies}").format(
            applies=sql.SQL(APPLIES_ON)  # pyright: ignore[reportArgumentType]
        ),
        {"d": as_of},
    ).fetchall()
    return {row[0] for row in rows}
