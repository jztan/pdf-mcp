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

import json
from collections.abc import Iterable  # noqa: F401
from typing import TypeVar

T = TypeVar("T")

VALID_CATEGORIES = {"lexical", "paraphrase-semantic", "mixed-distractor"}
REQUIRED_QUERY_FIELDS = ("id", "category", "query", "gold_section_keys")


def load_queries(path: str) -> dict:
    """Load and validate the frozen query corpus.

    Returns: {pdf_key: {"url": str, "queries": [query_dict, ...]}}.
    Raises ValueError on schema violations.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    if "pdfs" not in data:
        raise ValueError("Query file missing top-level 'pdfs' key")

    for pdf_key, pdf_data in data["pdfs"].items():
        if "url" not in pdf_data or "queries" not in pdf_data:
            raise ValueError(f"PDF '{pdf_key}' must have 'url' and 'queries'")
        for q in pdf_data["queries"]:
            for field in REQUIRED_QUERY_FIELDS:
                if field not in q:
                    raise ValueError(f"Query {q.get('id', '?')} missing field: {field}")
            if q["category"] not in VALID_CATEGORIES:
                raise ValueError(
                    f"Query {q['id']} has invalid category: {q['category']}"
                )

    return data["pdfs"]


def mrr(ranked: list[T], gold: set[T]) -> float:
    """Reciprocal rank of the first gold hit in `ranked`.

    Returns 1/(rank of first gold hit), or 0.0 if no gold hit appears.
    Ranks are 1-indexed.
    """
    for rank, item in enumerate(ranked, start=1):
        if item in gold:
            return 1.0 / rank
    return 0.0


def recall_at_k(ranked: list[T], gold: set[T], k: int) -> float:
    """Fraction of gold items appearing in the top-k of `ranked`.

    Raises ValueError when gold is empty (recall is undefined).
    """
    if not gold:
        raise ValueError("recall_at_k requires non-empty gold set")
    top_k = set(ranked[:k])
    return len(top_k & gold) / len(gold)


def embed_sections_for_pdf(
    cache,
    pdf_path: str,
    sections: list[dict],
    embedder,
    model_name: str,
) -> None:
    """Embed any sections not already cached (idempotent).

    Args:
        cache: PDFCache instance.
        pdf_path: Path to the PDF.
        sections: [{"id": int, "key": str, "text": str}, ...].
        embedder: Object with .embed(list[str]) -> iterable of np.ndarray
                  (matches fastembed.TextEmbedding).
        model_name: Identifier stored alongside embeddings.

    Note: does not detect model_name changes; clear section_embeddings
    if switching models between runs.
    """
    section_ids = [s["id"] for s in sections]
    cached = cache.get_section_embeddings(pdf_path, section_ids)
    todo = [s for s in sections if s["id"] not in cached]
    if not todo:
        return

    texts = [s["text"] for s in todo]
    vectors = list(embedder.embed(texts))

    new_blobs = {
        s["id"]: vectors[i].astype("float32").tobytes() for i, s in enumerate(todo)
    }
    new_keys = {s["id"]: s["key"] for s in todo}
    cache.save_section_embeddings(pdf_path, new_blobs, new_keys, model=model_name)
