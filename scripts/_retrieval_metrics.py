"""Pure ranking-quality metrics for retrieval benchmarks. No I/O, no deps."""

import math
from typing import TypeVar

T = TypeVar("T")


def dcg_at_k(gains: list[float], k: int) -> float:
    """Discounted cumulative gain over the first k graded gains (rank order)."""
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains[:k]))


def ndcg_at_k(ranked_gains: list[float], ideal_gains: list[float], k: int) -> float:
    """NDCG@k. ranked_gains: graded relevance of retrieved items in rank order.
    ideal_gains: all graded relevances available for the query (any order)."""
    idcg = dcg_at_k(sorted(ideal_gains, reverse=True), k)
    if idcg == 0:
        return 0.0
    return dcg_at_k(ranked_gains, k) / idcg


def mrr(ranked: list[T], gold: set[T]) -> float:
    """Reciprocal rank of the first gold item, else 0."""
    for i, item in enumerate(ranked):
        if item in gold:
            return 1.0 / (i + 1)
    return 0.0


def recall_at_k(ranked: list[T], gold: set[T], k: int) -> float:
    """Fraction of gold items present in the top k."""
    if not gold:
        return 0.0
    return len(set(ranked[:k]) & gold) / len(gold)
