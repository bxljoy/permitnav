"""`Retriever.search(query, as_of, k)` (docs/DESIGN.md, "Phase 1 internals").

Modes (`RetrievalConfig.mode`):
- `vector`: nearest chunks by cosine distance (bge-m3).
- `keyword`: BM25 over the chunks' full-text index, the question stemmed with each chunk's own
  configuration (Swedish or English).
- `hybrid`: both rankings fused with Reciprocal Rank Fusion, plus a direct lookup of sections
  the question names ("6 kap. 2 a §"): up to `MAX_NAMED` of them come first, in the order the
  question names them.

With `translate`, an English question is translated to Swedish (retrieval/translate.py), only
when something uses it: keyword search then matches Swedish chunks with the Swedish text and
English pages with the original, and with `translated_vector` the law's vector ranking is
ordered by the Swedish text too. Scores are always against the original question.

With `rerank`, a cross-encoder (reranking/) reorders each ranking's top `rerank_depth`
candidates; with `mix="rerank"` the groups are merged into one ranking by its scores, which,
unlike cosine scores, compare across law and pages.

Returns citable units, not chunks: when several chunks of one unit match, the best one
represents the unit (D2). Only wordings that apply on `as_of` are searched (D8, D5):
wordings waiting for a government decision, and withdrawn ones, never are.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import date, datetime

import psycopg
from psycopg import sql

from permitnav import tracing
from permitnav.corpus.indexing import EMBEDDING_TABLES, to_pgvector
from permitnav.embedding.port import Embedder
from permitnav.parsing.references import named_units
from permitnav.reranking.port import Reranker
from permitnav.retrieval.translate import Translator, is_swedish
from permitnav.retrieval.variants import collapse_variants


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
    rerank_score: float | None = None  # the reranker's, when reranking; orders the hits


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
    mix: str = "interleave"  # or "none", or "rerank" (one ranking by reranker score)
    candidates: int = 50  # per ranking, before fusion (hybrid)
    # Translate English questions to Swedish for keyword search (needs a Translator), and with
    # translated_vector also for the law's vector ranking.
    translate: bool = False
    translated_vector: bool = False
    # Hybrid: for English questions, a third ranking in the page group, ordered by the Swedish
    # translation's vector and fused with this weight, so Swedish-only pages (no English
    # twin) can reach English questions. 0 = off. Over all pages it crowded out English twins
    # (English readers' page sources in English 73% -> 41%), so by default it covers only
    # sections of pages that have no English version.
    translated_page_weight: float = 0.0
    translated_pages_untranslated_only: bool = True
    # One slot per near-duplicate page section (the same section on the employee, artist,
    # seasonal-worker… pages; retrieval/variants.py). Measured in phase 2 slice 5: recall
    # unchanged, but without it 36 of 512 top-8 slots on the dev set held such a copy.
    collapse_variants: bool = True
    # Reorder each ranking's top candidates with a cross-encoder (needs a Reranker). Measured in
    # phase 2 slice 6: R@5 0.55 -> 0.61 at depth 20 (agency items 0.38 -> 0.62-0.75), at about
    # 4 s per question on the Mac GPU; off by default until the phase 3 hosting decision.
    rerank: bool = False
    rerank_depth: int = 20
    # Slots for the "other" group (rulings, later position papers), placed last among the k
    # results: they complement the law and the guidance instead of taking a third of the
    # places in the interleaving (phase 3: with equal shares, dev R@5 fell 0.53 -> 0.46).
    # None gives the group an equal share, as before.
    other_slots: int | None = 1
    # The rank the "other" slots start at (1-based), the same whatever k a caller asks for,
    # so the eval (k=10) and the answerer (k=8) see the same ranking: the answer's k.
    other_rank: int = 8
    # Hybrid: the keyword ranking's weight in the fusion, relative to the vector ranking's 1.
    # 0 keeps only the vector ranking and the lookup of sections the question names. Measured
    # in phase 2 (dev set, 48 items): without translation R@5 0.47 at 0, 0.46 at 0.3, 0.40 at
    # 1, since an English question's words don't occur in the Swedish law; with translation
    # 0.56 at 0.5 (slice 4), the CLI's default. The default here stays offline: 0.
    keyword_weight: float = 0.0


class Retriever:
    def __init__(
        self,
        conn: psycopg.Connection,
        embedder: Embedder,
        config: RetrievalConfig | None = None,
        translator: Translator | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        self._conn = conn
        self._embedder = embedder
        self._translator = translator
        self._reranker = reranker
        self.config = config or RetrievalConfig()
        if self.config.rerank and reranker is None:
            raise ValueError("rerank=True needs a reranker")
        if self.config.mix == "rerank" and not self.config.rerank:
            raise ValueError('mix="rerank" needs rerank=True')
        if self.config.rerank_depth < 1:
            raise ValueError("rerank_depth must be >= 1")
        if self.config.translate and translator is None:
            raise ValueError("translate=True needs a translator")
        if self.config.translated_vector and not self.config.translate:
            raise ValueError("translated_vector needs translate=True")
        if self.config.mode not in MODES:
            raise ValueError(f"unknown retrieval mode {self.config.mode!r}; one of {MODES}")
        if self.config.mix not in ("interleave", "none", "rerank"):
            raise ValueError(f"unknown mix {self.config.mix!r}")
        if self.config.keyword_weight < 0 or self.config.candidates < 1:
            raise ValueError("keyword_weight must be >= 0 and candidates >= 1")
        if self.config.translated_page_weight < 0:
            raise ValueError("translated_page_weight must be >= 0")
        if self.config.translated_page_weight > 0 and not self.config.translate:
            raise ValueError("translated_page_weight needs translate=True")
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
        with tracing.span(
            "retrieve",
            "retriever",
            **{
                "permitnav.k": k,
                "permitnav.mode": self.config.mode,
                "permitnav.rerank": self.config.rerank,
            },
        ) as current:
            tracing.text(current, input=query)
            hits = self._search_all(query, as_of, k)
            tracing.set_attributes(
                current,
                **{
                    "permitnav.hits": len(hits),
                    # The best score, not the first hit's: named sections come first.
                    "permitnav.top_score": round(max(h.score for h in hits), 4) if hits else None,
                    "permitnav.units": [h.unit_id for h in hits],  # ids, never text
                },
            )
            return hits

    def _embed(self, text: str) -> str:
        with tracing.span(
            "embed", "embedding", **{"gen_ai.request.model": self._embedder.model_id}
        ):
            return to_pgvector(self._embedder.embed_query(text))

    def _search_all(self, query: str, as_of: date, k: int) -> list[Hit]:
        vector = self._embed(query)
        swedish = query
        if self._translator is not None and self._uses_translation() and not is_swedish(query):
            swedish = self._translator.translate(query)
        keyword = (swedish, query)  # (text for Swedish chunks, text for English chunks)
        law_order = None  # the vector the law is ordered by, when not the question's own
        swedish_vector = None
        if swedish != query and (self.config.translated_vector or self._page_translation()):
            swedish_vector = self._embed(swedish)
        if self.config.translated_vector:
            law_order = swedish_vector
        page_order = swedish_vector if self._page_translation() else None
        first: list[Hit] = []
        named = named_units(query) if self.config.mode == "hybrid" else []
        if named:
            found = {
                h.unit_id: h for h in self._search(vector, as_of, len(named), None, units=named)
            }
            first = [found[u] for u in named if u in found][:MAX_NAMED]
        depth = k + len(first)  # room for results the named sections duplicate
        # Collapsing variants removes results, so rank deeper first and cut after.
        fetch = 3 * depth if self.config.collapse_variants else depth
        if self.config.rerank:
            fetch = max(fetch, self.config.rerank_depth)

        def ranked(types, exclude, order_vector, extra_order=None):
            hits = self._ranking(
                keyword, vector, as_of, fetch, types, exclude, order_vector, extra_order
            )
            if self.config.rerank:  # the top candidates reordered; the rest kept after them
                cut = self.config.rerank_depth
                hits = self._rerank(query, hits[:cut]) + hits[cut:]
            if self.config.collapse_variants:
                hits = collapse_variants(hits, query)
            return hits[:depth]

        if self.config.mix == "none":
            rest = ranked(None, False, None)
        else:
            groups = [(t, False) for t in SOURCE_GROUPS.values()] + [(LISTED_TYPES, True)]
            rankings = [
                ranked(
                    t,
                    x,
                    law_order if t == SOURCE_GROUPS["law"] else None,
                    page_order if t == SOURCE_GROUPS["guidance"] else None,
                )
                for t, x in groups
            ]
            other: list[Hit] = []
            if self.config.other_slots is not None:  # the last group is "other"
                other = rankings.pop()[: self.config.other_slots]
            rankings = [hits for hits in rankings if hits]
            if self.config.mix == "rerank":  # one ranking: reranker scores compare across groups
                rest = sorted((hit for hits in rankings for hit in hits), key=_by_rerank_score)
            else:
                # The best-matching group first: by reranker score when reranking.
                rankings.sort(key=lambda hits: -_order_score(hits[0], self.config.rerank))
                rest = [
                    hits[position]
                    for position in range(depth)
                    for hits in rankings
                    if position < len(hits)
                ]
            if other:  # at a fixed rank, never in place of a section the question names
                ordered = _unique(first + rest)
                at = max(len(_unique(first)), min(k, self.config.other_rank) - len(other))
                return _unique(ordered[:at] + other + ordered[at:])[:k]
        return _unique(first + rest)[:k]

    def _rerank(self, question: str, hits: list[Hit]) -> list[Hit]:
        assert self._reranker is not None
        with tracing.span("rerank", "span", **{"permitnav.candidates": len(hits)}):
            scores = self._reranker.score(question, [h.text for h in hits])
        scored = [dataclasses.replace(h, rerank_score=s) for h, s in zip(hits, scores, strict=True)]
        return sorted(scored, key=_by_rerank_score)  # stable: ties keep their order

    def _page_translation(self) -> bool:
        return (
            self.config.mode == "hybrid"
            and self.config.translated_page_weight > 0
            and self.config.mix != "none"
        )

    def _uses_translation(self) -> bool:
        """Translation costs a model call: only when keyword search or the law's vector
        ordering will use it."""
        config = self.config
        if not config.translate:
            return False
        keyword_runs = config.mode == "keyword" or (
            config.mode == "hybrid" and config.keyword_weight > 0
        )
        # The law is ordered by the translation whenever groups are searched separately.
        return (
            keyword_runs
            or (config.translated_vector and config.mix != "none")
            or self._page_translation()
        )

    def _ranking(
        self,
        keyword: tuple[str, str],
        vector: str,
        as_of: date,
        k: int,
        types: tuple[str, ...] | None,
        exclude: bool,
        order_vector: str | None,
        extra_order: str | None = None,
    ) -> list[Hit]:
        """One group's ranking. `extra_order`: a further vector ranking (the translated
        question, for pages) fused in with translated_page_weight."""
        mode = self.config.mode
        if mode == "vector":
            return self._search(vector, as_of, k, types, exclude, order_vector=order_vector)
        if mode == "keyword":
            return self._search(vector, as_of, k, types, exclude, keyword=keyword)
        depth = max(self.config.candidates, k)
        rankings = [self._search(vector, as_of, depth, types, exclude, order_vector=order_vector)]
        weights = [1.0]
        if self.config.keyword_weight > 0:
            rankings.append(self._search(vector, as_of, depth, types, exclude, keyword=keyword))
            weights.append(self.config.keyword_weight)
        if extra_order is not None:  # scores stay against the question's own vector
            rankings.append(
                self._search(
                    vector,
                    as_of,
                    depth,
                    types,
                    exclude,
                    order_vector=extra_order,
                    untranslated_only=self.config.translated_pages_untranslated_only,
                )
            )
            weights.append(self.config.translated_page_weight)
        return fuse(rankings, weights)[:k]

    def _search(
        self,
        vector: str,
        as_of: date,
        k: int,
        source_types: tuple[str, ...] | None,
        exclude: bool = False,
        keyword: tuple[str, str] | None = None,
        units: list[str] | None = None,
        order_vector: str | None = None,
        untranslated_only: bool = False,
    ) -> list[Hit]:
        """One ranking: by cosine distance, or by BM25 when `keyword` (the question's text for
        Swedish and for English chunks) is given. `units` restricts it to those units.
        `order_vector` orders a vector ranking by another vector (the translated question);
        the score stays against `vector`."""
        keyword_ctes = sql.SQL("")
        keyword_join = sql.SQL("")
        keyword_where = sql.SQL("")
        order = sql.SQL(
            "e.embedding <=> %(oq)s::vector" if order_vector else "e.embedding <=> %(q)s::vector"
        )
        if keyword is not None:
            keyword_ctes = sql.SQL(KEYWORD_CTES).format(  # pyright: ignore[reportArgumentType]
                applies=sql.SQL(APPLIES_ON),  # pyright: ignore[reportArgumentType]
                in_group=sql.SQL(IN_GROUP),  # pyright: ignore[reportArgumentType]
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
                    AND (NOT %(untranslated)s OR (u.language = 'sv' AND {no_twin}))
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
                no_twin=sql.SQL(NO_LIVE_ENGLISH_TWIN),
                keyword_ctes=keyword_ctes,
                order=order,
                table=sql.Identifier(self._table),
                keyword_join=keyword_join,
                applies=sql.SQL(APPLIES_ON),  # pyright: ignore[reportArgumentType]
                keyword_where=keyword_where,
            ),
            {
                "q": vector,
                "oq": order_vector,
                "untranslated": untranslated_only,
                "strategy": self.config.strategy,
                "d": as_of,
                "k": k,
                "types": list(source_types) if source_types else None,
                "exclude": exclude,
                "units": units,
                "text_sv": _plain(keyword[0] if keyword else ""),
                "text_en": _plain(keyword[1] if keyword else ""),
            },
        ).fetchall()
        return [Hit(*row) for row in rows]


# A Swedish page with no English version that still exists: no twin recorded from either
# side (the Swedish page's hreflang, or an English page naming it) whose sections are still
# observed. A twin that disappeared from the site no longer counts.
NO_LIVE_ENGLISH_TWIN = """NOT EXISTS (
  SELECT 1 FROM page_translation pt
  JOIN unit tu ON tu.document = CASE WHEN pt.document = u.document THEN pt.translation
                                     ELSE pt.document END
  JOIN unit_version tv ON tv.unit_id = tu.id AND tv.observed_to IS NULL
  WHERE tu.language = 'en'
    AND ((pt.document = u.document AND pt.language = 'sv')
         OR (pt.translation = u.document AND pt.language = 'en'))
)"""


# BM25 over the chunks of one strategy whose wording applies on the date, the statistics
# included. The question is stemmed once per text-search configuration (its Swedish text with
# `swedish`, its original with `english`); each chunk is matched with the configuration it was
# indexed with. Document frequencies come from the GIN index;
# length is the number of distinct lexemes. k1 = 1.2
# (term-frequency saturation) and b = 0.75 (length normalisation), the usual defaults.
IN_GROUP = """(%(types)s::text[] IS NULL
  OR (u.source_type = ANY(%(types)s::text[])) <> %(exclude)s)"""
KEYWORD_CTES = """
terms AS (
  SELECT DISTINCT q.cfg, t.lexeme
  FROM (VALUES ('swedish'::regconfig, %(text_sv)s), ('english'::regconfig, %(text_en)s))
         AS q(cfg, text),
       LATERAL unnest(to_tsvector(q.cfg, q.text)) AS t
),
-- Collection statistics of the group being searched (its source types), not of every
-- source: each group is ranked on its own, so adding one kind of source (e.g. position
-- papers) must not shift how another is ranked.
corpus AS MATERIALIZED (
  SELECT c.ts_config AS cfg, count(*) AS n, avg(length(c.tsv)) AS avglen
  FROM chunk c JOIN unit_version v ON v.id = c.version_id JOIN unit u ON u.id = v.unit_id
  WHERE c.strategy = %(strategy)s AND {applies} AND {in_group}
  GROUP BY c.ts_config
),
df AS MATERIALIZED (
  SELECT t.cfg, t.lexeme,
         (SELECT count(*) FROM chunk c JOIN unit_version v ON v.id = c.version_id
          JOIN unit u ON u.id = v.unit_id
          WHERE c.strategy = %(strategy)s AND c.ts_config = t.cfg
            AND c.tsv @@ quote_literal(t.lexeme)::tsquery AND {applies} AND {in_group}) AS df
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


def _order_score(hit: Hit, reranked: bool) -> float:
    if not reranked:
        return hit.score
    return hit.rerank_score if hit.rerank_score is not None else float("-inf")


def _by_rerank_score(hit: Hit) -> tuple[bool, float]:
    """Highest reranker score first; hits the reranker did not score after all scored ones.
    (Raw scores can be negative, so a missing score is not 0.)"""
    return (hit.rerank_score is None, -(hit.rerank_score or 0.0))


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
