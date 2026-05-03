#!/usr/bin/env python
"""
scripts/benchmark_hybrid_sections.py

Phase-1 validation benchmark for hybrid (BM25 + semantic) section search.

Compares four cells over a frozen query corpus on three arxiv PDFs:
    keyword-page, hybrid-page, keyword-section, hybrid-section

Asserts a three-clause kill-switch gate (see
docs/superpowers/specs/2026-05-04-hybrid-section-search-validation-design.md).

Usage:
    python scripts/benchmark_hybrid_sections.py              # gated run
    python scripts/benchmark_hybrid_sections.py --calibrate  # report only
    python scripts/benchmark_hybrid_sections.py --pdfs gnn_review,llm_survey
    python scripts/benchmark_hybrid_sections.py --output-json results.json

Exit codes: 0 = PASS / calibrate, 1 = FAIL, 2 = setup error.
"""

from __future__ import annotations

from collections.abc import Iterable  # noqa: F401
from typing import TypeVar

T = TypeVar("T")


def mrr(ranked: list[T], gold: set[T]) -> float:
    """Reciprocal rank of the first gold hit in `ranked`.

    Returns 1/(rank of first gold hit), or 0.0 if no gold hit appears.
    Ranks are 1-indexed.
    """
    for rank, item in enumerate(ranked, start=1):
        if item in gold:
            return 1.0 / rank
    return 0.0
