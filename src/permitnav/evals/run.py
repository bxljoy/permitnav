"""Running the golden set through the retriever and summarising the results."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date

from permitnav.evals.golden import GoldenItem
from permitnav.evals.retrieval_metrics import bootstrap_ci, group_ranks, mrr, ndcg_at, recall_at
from permitnav.retrieval.retriever import Retriever

K_MAX = 10


@dataclass(frozen=True)
class ItemResult:
    item: GoldenItem
    retrieved: tuple[str, ...]  # unit ids, best first
    ranks: tuple[int | None, ...]  # per required group


@dataclass
class RetrievalRun:
    results: list[ItemResult] = field(default_factory=list)
    skipped: dict[str, list[str]] = field(default_factory=dict)  # reason -> item ids
    unknown_units: dict[str, list[str]] = field(default_factory=dict)  # item id -> missing ids


PLACEHOLDER = "web:tbd/"


def run_retrieval(
    items: Iterable[GoldenItem],
    retriever: Retriever,
    existing_units: dict[str, str],
    applying_on: Callable[[date], set[str]],
) -> RetrievalRun:
    """Score every item whose required groups can all be found; record why others can't.

    `existing_units` maps each unit id in the corpus to its document; `applying_on(d)` gives
    the units with a wording that applies on date d (the only ones search can return).

    - An expected unit from an indexed statute that is not in the corpus is a golden-set
      error (a typo or a wrong section number), reported, never counted as a miss.
    - Placeholders (`web:tbd/...`) and units from sources not indexed yet are alternatives that
      cannot be found; a group is scored if another alternative can be. An item is skipped
      only when some group has no findable alternative left.
    """
    indexed_statutes = {doc for doc in existing_units.values() if doc.startswith("sfs:")}
    run = RetrievalRun()

    def skip(reason: str, item: GoldenItem) -> None:
        run.skipped.setdefault(reason, []).append(item.id)

    for item in items:
        if not item.expected:
            skip("out of scope (no sources to retrieve)", item)
            continue
        units = [u for group in item.expected for u in group]
        unknown = [
            u
            for u in units
            if u.startswith("sfs:")
            and u.split("/", 1)[0] in indexed_statutes
            and u not in existing_units
        ]
        if unknown:
            run.unknown_units[item.id] = unknown
            continue
        if not all(any(u in existing_units for u in group) for group in item.expected):
            only_pages = all(
                any(u.startswith(PLACEHOLDER) for u in group)
                for group in item.expected
                if not any(u in existing_units for u in group)
            )
            reason = (
                "cites a Migrationsverket page not indexed yet"
                if only_pages
                else "cites a source type not indexed yet"
            )
            skip(reason, item)
            continue
        applying = applying_on(item.as_of)
        if not all(any(u in applying for u in group) for group in item.expected):
            skip(
                "an expected wording does not apply on the item's as_of (history not rebuilt)", item
            )
            continue
        hits = retriever.search(item.question, as_of=item.as_of, k=K_MAX)
        retrieved = tuple(h.unit_id for h in hits)
        run.results.append(
            ItemResult(
                item=item, retrieved=retrieved, ranks=tuple(group_ranks(item.expected, retrieved))
            )
        )
    return run


@dataclass(frozen=True)
class SliceSummary:
    name: str
    n: int
    recall_1: float
    recall_5: float
    recall_5_ci: tuple[float, float]
    recall_10: float
    mrr: float
    ndcg_5: float


def summarise(results: Sequence[ItemResult], name: str) -> SliceSummary:
    def per_item(metric: Callable[[ItemResult], float]) -> list[float]:
        return [metric(r) for r in results]

    def mean(values: list[float]) -> float:
        return sum(values) / len(values)

    r5 = per_item(lambda r: recall_at(r.ranks, 5))
    return SliceSummary(
        name=name,
        n=len(results),
        recall_1=mean(per_item(lambda r: recall_at(r.ranks, 1))),
        recall_5=mean(r5),
        recall_5_ci=bootstrap_ci(r5),
        recall_10=mean(per_item(lambda r: recall_at(r.ranks, 10))),
        mrr=mean(per_item(lambda r: mrr(r.ranks))),
        ndcg_5=mean(per_item(lambda r: ndcg_at(r.ranks, 5))),
    )


def slices(run: RetrievalRun) -> list[SliceSummary]:
    """All items; by question language and original vs translated (D20); by type; by authority."""
    groups: dict[str, list[ItemResult]] = {"all": list(run.results)}
    for r in run.results:
        origin = "translated" if r.item.is_translation else "original"
        groups.setdefault(f"{r.item.original_language} ({origin})", []).append(r)
    for r in run.results:
        groups.setdefault(f"type: {r.item.type}", []).append(r)
    for r in run.results:  # legal requirement vs agency practice (D22)
        if r.item.authority:
            groups.setdefault(f"authority: {r.item.authority}", []).append(r)
    return [summarise(results, name) for name, results in groups.items() if results]
