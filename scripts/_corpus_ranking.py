"""Pure ranking and decision logic for the corpus-search spike.

No I/O, stdlib only (same discipline as _retrieval_metrics.py). The
benchmark script wires these to real FTS results; unit tests exercise
them on synthetic data.
"""

from __future__ import annotations

try:
    from _query_classes import LEXICAL_DISTRACTOR, normalize_keys
except ImportError:  # imported as scripts._corpus_ranking (tests)
    from scripts._query_classes import LEXICAL_DISTRACTOR, normalize_keys

# Pre-committed decision-rule constants (from the multi-doc design spec).
LEXICAL_DISTRACTOR_GAIN_MIN = 0.05
CLASS_REGRESS_MAX = 0.02
ARM_A_QUERY_BUDGET_S = 1.0
RRF_K = 60  # matches production _RRF_K

# Epsilon for float-representation noise only, never for real margins.
_EPS = 1e-9


def rrf_fuse_doc_rankings(
    rank_lists: list[list[tuple[str, int]]],
    k: int = RRF_K,
    top_k: int | None = None,
) -> list[tuple[str, int]]:
    """Fuse per-doc rank lists into one global ranking via RRF.

    Each inner list is one document's (doc_id, page) hits, best first.
    Every item appears in exactly one list, so the fused score is
    1 / (k + rank): items interleave by within-doc rank. Ties break
    deterministically by (doc_id, page).
    """
    scored: list[tuple[float, str, int]] = []
    for hits in rank_lists:
        for rank, (doc, page) in enumerate(hits):
            scored.append((1.0 / (k + rank), doc, page))
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))
    fused = [(doc, page) for _score, doc, page in scored]
    return fused[:top_k] if top_k is not None else fused


def grade_ranking(
    ranked: list[tuple[str, int]],
    labels: dict[tuple[str, int], float],
) -> list[float]:
    """Graded relevance of each retrieved (doc, page) in rank order."""
    return [labels.get(item, 0.0) for item in ranked]


def evaluate_decision(
    class_ndcg_a: dict[str, float],
    class_ndcg_b: dict[str, float],
    arm_a_mean_query_seconds: float,
) -> dict:
    """Apply the pre-committed decision rule.

    Arm A = corpus-wide temp FTS, arm B = RRF fusion. A wins iff its
    lexical_distractor-class NDCG@10 beats B's by >=
    LEXICAL_DISTRACTOR_GAIN_MIN, no other class regresses by >
    CLASS_REGRESS_MAX, and A's mean per-query cost is < ARM_A_QUERY_BUDGET_S.
    Otherwise (including ties) B wins.
    """
    reasons: list[str] = []
    class_ndcg_a = normalize_keys(class_ndcg_a)
    class_ndcg_b = normalize_keys(class_ndcg_b)
    gain = class_ndcg_a.get(LEXICAL_DISTRACTOR, 0.0) - class_ndcg_b.get(
        LEXICAL_DISTRACTOR, 0.0
    )
    if gain >= LEXICAL_DISTRACTOR_GAIN_MIN - _EPS:
        reasons.append(
            f"lexical_distractor-class NDCG delta {gain:+.3f}"
            f" >= {LEXICAL_DISTRACTOR_GAIN_MIN}"
        )
        win = True
    else:
        reasons.append(
            f"lexical_distractor-class NDCG delta {gain:+.3f}"
            f" < {LEXICAL_DISTRACTOR_GAIN_MIN}"
        )
        win = False

    for cls in sorted((set(class_ndcg_a) | set(class_ndcg_b)) - {LEXICAL_DISTRACTOR}):
        regress = class_ndcg_b.get(cls, 0.0) - class_ndcg_a.get(cls, 0.0)
        if regress > CLASS_REGRESS_MAX + _EPS:
            reasons.append(f"{cls}-class regresses {regress:.3f} > {CLASS_REGRESS_MAX}")
            win = False

    # Timing values carry no float-representation concern at this scale.
    if arm_a_mean_query_seconds >= ARM_A_QUERY_BUDGET_S:
        reasons.append(
            f"arm-A mean per-query cost {arm_a_mean_query_seconds:.3f}s"
            f" >= {ARM_A_QUERY_BUDGET_S}s budget"
        )
        win = False

    return {"winner": "temp-fts" if win else "rrf-fusion", "reasons": reasons}
