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
import sys
from collections.abc import Iterable  # noqa: F401
from pathlib import Path
from typing import TypeVar

# Make src/pdf_mcp importable even when run from outside the project root.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import sqlite3  # noqa: E402

import numpy as np  # noqa: E402

from pdf_mcp.server import _rrf_fuse  # noqa: E402  reuse the existing RRF

T = TypeVar("T")

VALID_CATEGORIES = {"lexical", "paraphrase-semantic", "mixed-distractor"}
REQUIRED_QUERY_FIELDS = ("id", "category", "query", "gold_section_keys")

GATE_CLAUSE_1_MARGIN = 0.10
GATE_CLAUSE_2_TOLERANCE = 0.05


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


def _cosine_rank(cache, pdf_path: str, query_vec: np.ndarray, top_k: int) -> list[int]:
    """Return section IDs ranked by cosine similarity to query_vec."""
    with sqlite3.connect(cache.db_path) as conn:
        rows = conn.execute(
            "SELECT section_id, embedding FROM section_embeddings"
            " WHERE file_path = ?",
            (pdf_path,),
        ).fetchall()

    if not rows:
        return []

    ids = [int(r[0]) for r in rows]
    mat = np.stack([np.frombuffer(r[1], dtype="float32") for r in rows])
    # bge embeddings are L2-normalized, so dot product == cosine similarity.
    # Only normalize the query (defensive; cheap and harmless if already unit).
    qn = query_vec / (np.linalg.norm(query_vec) + 1e-12)
    scores = mat @ qn.astype("float32")

    order = np.argsort(-scores)[:top_k]
    return [ids[i] for i in order]


def hybrid_section_search(
    cache,
    pdf_path: str,
    query: str,
    query_vec: np.ndarray,
    top_k: int = 5,
) -> list[int]:
    """Phase-1 shim: BM25 over pdf_section_fts + cosine over
    section_embeddings, fused with RRF (k=60). Returns ranked section IDs."""
    bm25_results = cache.search_section_fts(pdf_path, query, max_results=top_k * 2)
    keyword_ids = [int(r["section_id"]) for r in bm25_results]
    semantic_ids = _cosine_rank(cache, pdf_path, query_vec, top_k * 2)

    fused = _rrf_fuse(keyword_ids, semantic_ids, max_results=top_k)
    return [sid for sid, _score in fused]


def evaluate_gate(cells: dict) -> dict:
    """Evaluate the three-clause kill-switch gate.

    cells: {cell_name: {"lexical": float, "paraphrase-semantic": float,
                        "mixed-distractor": float, "all": float}}.
            All values are micro-mean MRR over the relevant query subset.

    Returns a dict with overall `pass` and per-clause detail.
    """
    hs = cells["hybrid-section"]
    others = {k: v for k, v in cells.items() if k != "hybrid-section"}

    next_best_md = max(c["mixed-distractor"] for c in others.values())
    clause_1_pass = hs["mixed-distractor"] >= next_best_md + GATE_CLAUSE_1_MARGIN

    ks_lex = cells["keyword-section"]["lexical"]
    clause_2_pass = hs["lexical"] >= ks_lex - GATE_CLAUSE_2_TOLERANCE

    clause_3_pass = hs["all"] >= cells["hybrid-page"]["all"]

    return {
        "pass": clause_1_pass and clause_2_pass and clause_3_pass,
        "clause_1_mixed_distractor": {
            "pass": clause_1_pass,
            "hybrid_section": hs["mixed-distractor"],
            "next_best": next_best_md,
            "required_margin": GATE_CLAUSE_1_MARGIN,
        },
        "clause_2_lexical": {
            "pass": clause_2_pass,
            "hybrid_section": hs["lexical"],
            "keyword_section": ks_lex,
            "tolerance": GATE_CLAUSE_2_TOLERANCE,
        },
        "clause_3_overall": {
            "pass": clause_3_pass,
            "hybrid_section": hs["all"],
            "hybrid_page": cells["hybrid-page"]["all"],
        },
    }
