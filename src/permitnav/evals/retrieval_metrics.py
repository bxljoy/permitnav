"""Retrieval metrics over required groups of alternative units (D16).

For one item with groups G1..Gn and a ranked list of units, a group's rank is the position of
the first unit from that group (1-based), or None if none was retrieved.

- recall@k: share of groups with rank <= k.
- MRR: mean over groups of 1/rank (0 if missing).
- nDCG@k: DCG over the groups found within k, divided by the ideal (all groups at the top).
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence


def group_ranks(expected: Sequence[Sequence[str]], ranked: Sequence[str]) -> list[int | None]:
    position = {}
    for i, unit in enumerate(ranked, start=1):
        position.setdefault(unit, i)
    ranks: list[int | None] = []
    for group in expected:
        found = [position[u] for u in group if u in position]
        ranks.append(min(found) if found else None)
    return ranks


def recall_at(ranks: Sequence[int | None], k: int) -> float:
    return sum(1 for r in ranks if r is not None and r <= k) / len(ranks)


def mrr(ranks: Sequence[int | None]) -> float:
    return sum(1 / r for r in ranks if r is not None) / len(ranks)


def ndcg_at(ranks: Sequence[int | None], k: int) -> float:
    dcg = sum(1 / math.log2(r + 1) for r in ranks if r is not None and r <= k)
    ideal = sum(1 / math.log2(i + 1) for i in range(1, min(len(ranks), k) + 1))
    return dcg / ideal


def bootstrap_ci(
    values: Sequence[float],
    statistic: Callable[[Sequence[float]], float] = lambda v: sum(v) / len(v),
    rounds: int = 2000,
    level: float = 0.95,
    seed: int = 0,
) -> tuple[float, float]:
    """Percentile bootstrap over items. Small sets give wide intervals; that is the point."""
    if not values:
        return (math.nan, math.nan)
    rng = random.Random(seed)
    n = len(values)
    samples = sorted(statistic([values[rng.randrange(n)] for _ in range(n)]) for _ in range(rounds))
    alpha = 1 - level
    low = samples[round(alpha / 2 * (rounds - 1))]
    high = samples[round((1 - alpha / 2) * (rounds - 1))]
    return (low, high)
