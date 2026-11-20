"""Retrieval metrics over required groups of alternatives (D16)."""

from __future__ import annotations

import math
from pathlib import Path

from permitnav.evals.golden import load_golden
from permitnav.evals.retrieval_metrics import bootstrap_ci, group_ranks, mrr, ndcg_at, recall_at


def test_a_group_is_found_by_its_best_ranked_alternative() -> None:
    expected = [["a"], ["b", "c"]]
    assert group_ranks(expected, ["x", "c", "a", "b"]) == [3, 2]
    assert group_ranks(expected, ["x", "y"]) == [None, None]


def test_recall_mrr_and_ndcg() -> None:
    ranks = [1, 4, None]
    assert recall_at(ranks, 1) == 1 / 3
    assert recall_at(ranks, 5) == 2 / 3
    assert math.isclose(mrr(ranks), (1 + 1 / 4) / 3)
    ideal = 1 + 1 / math.log2(3) + 1 / math.log2(4)
    assert math.isclose(ndcg_at(ranks, 5), (1 + 1 / math.log2(5)) / ideal)
    assert ndcg_at([1, 2], 5) == 1.0


def test_bootstrap_interval_is_deterministic_and_brackets_the_mean() -> None:
    values = [1.0] * 7 + [0.0] * 3
    low, high = bootstrap_ci(values)
    assert low <= 0.7 <= high
    assert (low, high) == bootstrap_ci(values)
    assert bootstrap_ci([1.0, 1.0]) == (1.0, 1.0)


def test_the_dev_set_loads() -> None:
    items = load_golden(Path(__file__).resolve().parents[1] / "evals" / "golden.dev.jsonl")
    assert len(items) >= 50
    assert all(isinstance(group, tuple) for i in items for group in i.expected)


def test_bootstrap_bounds_are_symmetric_for_any_level() -> None:
    values = [float(i % 2) for i in range(40)]
    for level in (0.8, 0.9, 0.95):
        low, high = bootstrap_ci(values, level=level)
        assert low <= 0.5 <= high
