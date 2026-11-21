"""`Retriever.search(query, as_of, k)` (docs/DESIGN.md, "Phase 1 internals").

Modes (`RetrievalConfig.mode`):
- `vector`: nearest chunks by cosine distance (bge-m3).
- `keyword`: BM25 over the chunks' full-text index, the question stemmed with each chunk's own
  configuration (Swedish or English).
- `hybrid`: both rankings fused with Reciprocal Rank Fusion, plus a direct lookup of sections
  the question names ("6 kap. 2 a §"): up to `MAX_NAMED` of them come first, in the order the
  question names them.

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
from permitnav.parsing.references import named_units


@dataclass(frozen=True)
class Hit:
    unit_id: str
    version_id: int
    chunk_id: int
    text: str
    # Cosine similarity of the chunk to the question, higher is better, in every mode (also
    # keyword): it orders the source groups and is comparable across modes.
    score: float
    authority: str
    heading: str
    url: str
    valid_from: date | None
    valid_to: date | None
    fetched_at: datetime  # when the crawler first saw this wording
    text_hash: str  # identical wordings (repeated page sections) share it


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


MODES = ("vector", "keyword", "hybrid")
RRF_K = 60  # the usual Reciprocal Rank Fusion constant: 1 / (60 + rank)
# Named sections placed first, so a range ("10 kap. 1–17 §§") cannot fill every slot.
MAX_NAMED = 3


@dataclass(frozen=True)
class RetrievalConfig:
    mode: str = "hybrid"  # vector | keyword | hybrid
    strategy: str = "structure"  # which chunks to search
    # "interleave": search law and guidance separately and alternate the results, so pages in
    # the question's language cannot push the (Swedish) law out of the top k. "none": one
    # ranking over everything.
    mix: str = "interleave"
    candidates: int = 50  # per ranking, before fusion (hybrid)
    # Hybrid: the keyword ranking's weight in the fusion, relative to the vector ranking's 1.
    # 0 keeps only the vector ranking and the lookup of sections the question names. Measured
    # in phase 2 slice 1 (dev set, 48 items): R@5 0.47 at 0, 0.46 at 0.3, 0.40 at 1. BM25
    # helps Swedish questions but adds noise for English ones, whose words don't occur in the
    # Swedish law; revisit once English questions are translated for keyword search.
    keyword_weight: float = 0.0


class Retriever:
    def __init__(
        self, conn: psycopg.Connection, embedder: Embedder, config: RetrievalConfig | None = None
    ) -> None:
        self._conn = conn
        self._embedder = embedder
        self.config = config or RetrievalConfig()
        if self.config.mode not in MODES:
            raise ValueError(f"unknown retrieval mode {self.config.mode!r}; one of {MODES}")
        if self.config.mix not in ("interleave", "none"):
            raise ValueError(f"unknown mix {self.config.mix!r}")
        if self.config.keyword_weight < 0 or self.config.candidates < 1:
            raise ValueError("keyword_weight must be >= 0 and candidates >= 1")
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
        first: list[Hit] = []
        named = named_units(query) if self.config.mode == "hybrid" else []
        if named:
            found = {
                h.unit_id: h for h in self._search(vector, as_of, len(named), None, units=named)
            }
            first = [found[u] for u in named if u in found][:MAX_NAMED]
        depth = k + len(first)  # room for results the named sections duplicate
        if self.config.mix == "none":
            rest = self._ranking(query, vector, as_of, depth, None, False)
        else:
            groups = [(t, False) for t in SOURCE_GROUPS.values()] + [(LISTED_TYPES, True)]
            rankings = [self._ranking(query, vector, as_of, depth, t, x) for t, x in groups]
            rankings = [hits for hits in rankings if hits]
            rankings.sort(key=lambda hits: -hits[0].score)  # the best-matching group first
            rest = [
                hits[position]
                for position in range(depth)
                for hits in rankings
                if position < len(hits)
            ]
        return _unique(first + rest)[:k]

    def _ranking(
        self,
        query: str,
        vector: str,
        as_of: date,
        k: int,
        types: tuple[str, ...] | None,
        exclude: bool,
    ) -> list[Hit]:
        mode = self.config.mode
        if mode == "vector":
            return self._search(vector, as_of, k, types, exclude)
        if mode == "keyword":
            return self._search(vector, as_of, k, types, exclude, keyword=query)
        depth = max(self.config.candidates, k)
        rankings = [self._search(vector, as_of, depth, types, exclude)]
        weights = [1.0]
        if self.config.keyword_weight > 0:
            rankings.append(self._search(vector, as_of, depth, types, exclude, keyword=query))
            weights.append(self.config.keyword_weight)
        return fuse(rankings, weights)[:k]

    def _search(
        self,
        vector: str,
        as_of: date,
        k: int,
        source_types: tuple[str, ...] | None,
        exclude: bool = False,
        keyword: str | None = None,
        units: list[str] | None = None,
    ) -> list[Hit]:
        """One ranking: by cosine distance, or by BM25 when `keyword` is given. `units`
        restricts it to those units."""
        keyword_ctes = sql.SQL("")
        keyword_join = sql.SQL("")
        keyword_where = sql.SQL("")
        order = sql.SQL("e.embedding <=> %(q)s::vector")
        if keyword is not None:
            keyword_ctes = sql.SQL(KEYWORD_CTES).format(  # pyright: ignore[reportArgumentType]
                applies=sql.SQL(APPLIES_ON)  # pyright: ignore[reportArgumentType]
            )
            keyword_join = sql.SQL(
                "JOIN queries kq ON kq.cfg = c.ts_config JOIN corpus co ON co.cfg = c.ts_config"
            )
            keyword_where = sql.SQL("AND c.tsv @@ kq.tsq")
            order = sql.SQL(NEGATIVE_BM25)
        rows = self._conn.execute(
            sql.SQL(
                """
                WITH {keyword_ctes}
                scored AS MATERIALIZED (
                  SELECT u.id AS unit_id, v.id AS version_id, c.id AS chunk_id, c.text,
                         v.text_hash,
                         e.embedding <=> %(q)s::vector AS distance,
                         {order} AS rank_key,
                         u.authority, u.heading, u.url, v.valid_from, v.valid_to, v.observed_from
                  FROM {table} e
                  JOIN chunk c ON c.id = e.chunk_id
                  JOIN unit_version v ON v.id = c.version_id
                  JOIN unit u ON u.id = v.unit_id
                  {keyword_join}
                  WHERE c.strategy = %(strategy)s AND {applies} {keyword_where}
                    AND (%(types)s::text[] IS NULL
                         OR (u.source_type = ANY(%(types)s::text[])) <> %(exclude)s)
                    AND (%(units)s::text[] IS NULL OR u.id = ANY(%(units)s::text[]))
                ),
                best_per_unit AS (
                  SELECT DISTINCT ON (unit_id) * FROM scored ORDER BY unit_id, rank_key, chunk_id
                ),
                -- The site repeats identical sections on many pages; keep one per text.
                best AS (
                  SELECT DISTINCT ON (text_hash) * FROM best_per_unit
                  ORDER BY text_hash, rank_key, unit_id
                )
                SELECT unit_id, version_id, chunk_id, text, 1 - distance, authority, heading,
                       url, valid_from, valid_to, observed_from, text_hash
                FROM best ORDER BY rank_key, unit_id LIMIT %(k)s
                """
            ).format(
                keyword_ctes=keyword_ctes,
                order=order,
                table=sql.Identifier(self._table),
                keyword_join=keyword_join,
                applies=sql.SQL(APPLIES_ON),  # pyright: ignore[reportArgumentType]
                keyword_where=keyword_where,
            ),
            {
                "q": vector,
                "strategy": self.config.strategy,
                "d": as_of,
                "k": k,
                "types": list(source_types) if source_types else None,
                "exclude": exclude,
                "units": units,
                "text": _plain(keyword or ""),
            },
        ).fetchall()
        return [Hit(*row) for row in rows]


# BM25 over the chunks of one strategy whose wording applies on the date, the statistics
# included. The question is stemmed once per text-search configuration; each chunk is matched
# with the configuration it was indexed with. Document frequencies come from the GIN index;
# length is the number of distinct lexemes. k1 = 1.2
# (term-frequency saturation) and b = 0.75 (length normalisation), the usual defaults.
KEYWORD_CTES = """
terms AS (
  SELECT DISTINCT cfg, t.lexeme
  FROM unnest(ARRAY['swedish', 'english']::regconfig[]) AS cfg,
       LATERAL unnest(to_tsvector(cfg, %(text)s)) AS t
),
corpus AS MATERIALIZED (
  SELECT c.ts_config AS cfg, count(*) AS n, avg(length(c.tsv)) AS avglen
  FROM chunk c JOIN unit_version v ON v.id = c.version_id
  WHERE c.strategy = %(strategy)s AND {applies}
  GROUP BY c.ts_config
),
df AS MATERIALIZED (
  SELECT t.cfg, t.lexeme,
         (SELECT count(*) FROM chunk c JOIN unit_version v ON v.id = c.version_id
          WHERE c.strategy = %(strategy)s AND c.ts_config = t.cfg
            AND c.tsv @@ quote_literal(t.lexeme)::tsquery AND {applies}) AS df
  FROM terms t
),
queries AS (
  SELECT cfg, string_agg(quote_literal(lexeme), ' | ')::tsquery AS tsq FROM terms GROUP BY cfg
),
"""
NEGATIVE_BM25 = """-(
  SELECT sum(
    ln(1 + (co.n - d.df + 0.5) / (d.df + 0.5))
    * cardinality(x.positions) * (1.2 + 1)
    / (cardinality(x.positions)
       + 1.2 * (1 - 0.75 + 0.75 * length(c.tsv) / NULLIF(co.avglen, 0))))
  FROM unnest(c.tsv) AS x JOIN df d ON d.cfg = c.ts_config AND d.lexeme = x.lexeme
)"""


def fuse(rankings: list[list[Hit]], weights: list[float] | None = None) -> list[Hit]:
    """Reciprocal Rank Fusion: each ranking adds weight / (RRF_K + rank) to an item's score.
    Items are identified by their text, so an identical copy found by the other ranking
    counts as the same item; the first ranking's unit represents it."""
    scores: dict[str, float] = {}
    chosen: dict[str, Hit] = {}
    for ranking, weight in zip(rankings, weights or [1.0] * len(rankings), strict=True):
        for rank, hit in enumerate(ranking, start=1):
            scores[hit.text_hash] = scores.get(hit.text_hash, 0.0) + weight / (RRF_K + rank)
            chosen.setdefault(hit.text_hash, hit)
    order = sorted(scores, key=lambda h: (-scores[h], chosen[h].unit_id))
    return [chosen[h] for h in order]


def _unique(hits: list[Hit]) -> list[Hit]:
    seen: set[str] = set()
    out = []
    for hit in hits:
        if hit.unit_id not in seen and hit.text_hash not in seen:
            seen |= {hit.unit_id, hit.text_hash}
            out.append(hit)
    return out


def _plain(text: str) -> str:
    """Question text for to_tsvector: backslashes would break the quoted lexemes."""
    return text.replace("\\", " ")


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
