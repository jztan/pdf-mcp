#!/usr/bin/env python
"""
scripts/benchmark_cjk_keyword.py

Graded CJK (Japanese) keyword-search benchmark over the local vertical-jp
municipal-bulletin corpus.

Validates the CJK FTS5 keyword fix: queries of unspaced CJK text (which the
porter/unicode61 index could not match when the term was embedded mid-run)
now return the pages that literally contain the term. The headline case is
``厚木基地``, which returned 0 hits before the fix.

Ground truth is NOT hand-authored. For each query the benchmark scans every
page's extracted text (the same ``extract_text_from_page`` the cache indexes)
for the literal query substring; the pages that contain it ARE the relevant
set. This makes recall an honest, reproducible measurement rather than a guess.

Usage:
    python scripts/benchmark_cjk_keyword.py            # run + print table
    python scripts/benchmark_cjk_keyword.py --json out.json

Exit codes: 0 = ran (or corpus absent), 2 = setup error.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "src"))

import pymupdf  # noqa: E402

import pdf_mcp.server as server_module  # noqa: E402
from pdf_mcp.cache import PDFCache  # noqa: E402
from pdf_mcp.extractor import extract_text_from_page  # noqa: E402

CORPUS_DIR = REPO / "docs_internal" / "sample_pdfs" / "vertical-jp"
QUERIES_PATH = REPO / "benchmark_data" / "cjk_keyword_queries.json"


def corpus_available() -> bool:
    """True if at least one referenced PDF exists locally."""
    if not QUERIES_PATH.exists():
        return False
    data = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))
    return any((CORPUS_DIR / q["pdf"]).exists() for q in data["queries"])


def _page_texts(pdf_path: str) -> list[str]:
    """Extracted text for every page, via the same path the cache indexes."""
    doc = pymupdf.open(pdf_path)
    try:
        return [extract_text_from_page(doc[i]) for i in range(len(doc))]
    finally:
        doc.close()


def run_benchmark() -> dict:
    """Run keyword search for each query and grade against literal ground truth.

    Returns a dict keyed by query string with per-query
    {pdf, hits, gt_pages, ret_pages, recall, precision}, plus a top-level
    ``mean_recall`` over queries that have at least one ground-truth page.
    Uses an isolated temp cache so the new write path populates the CJK FTS
    tables fresh (not via migration of a pre-existing cache).
    """
    data = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))

    tmp = tempfile.mkdtemp(prefix="cjk_bench_")
    server_module.cache = PDFCache(cache_dir=Path(tmp))

    text_cache: dict[str, list[str]] = {}
    results: dict[str, dict] = {}
    recalls: list[float] = []

    for q in data["queries"]:
        pdf_path = str(CORPUS_DIR / q["pdf"])
        if not Path(pdf_path).exists():
            continue
        if pdf_path not in text_cache:
            text_cache[pdf_path] = _page_texts(pdf_path)
        needle = "".join(q["query"].split())
        gt_pages = [i + 1 for i, t in enumerate(text_cache[pdf_path]) if needle in t]

        res = server_module.pdf_search(
            pdf_path, q["query"], mode="keyword", max_results=50
        )
        ret_pages = (
            [m["page"] for m in res.get("matches", [])] if "error" not in res else []
        )

        relevant = set(gt_pages)
        hit_set = set(ret_pages) & relevant
        recall = len(hit_set) / len(relevant) if relevant else 0.0
        precision = len(hit_set) / len(ret_pages) if ret_pages else 0.0

        results[q["query"]] = {
            "pdf": q["pdf"],
            "hits": len(ret_pages),
            "gt_pages": gt_pages,
            "ret_pages": ret_pages,
            "recall": recall,
            "precision": precision,
        }
        if relevant:
            recalls.append(recall)

    results["mean_recall"] = sum(recalls) / len(recalls) if recalls else 0.0
    return results


def _print_table(results: dict) -> None:
    print(
        f"\n{'query':<10} {'pdf':<14} {'gt':>3} {'hits':>4} "
        f"{'recall':>7} {'prec':>6}"
    )
    print("-" * 52)
    for query, r in results.items():
        if query == "mean_recall":
            continue
        pdf_short = r["pdf"].split("_")[0]
        print(
            f"{query:<10} {pdf_short:<14} {len(r['gt_pages']):>3} "
            f"{r['hits']:>4} {r['recall']:>7.2f} {r['precision']:>6.2f}"
        )
    print("-" * 52)
    print(f"mean_recall = {results['mean_recall']:.3f}")
    anchor = results.get("厚木基地")
    if anchor is not None:
        print(f"anchor 厚木基地 hits = {anchor['hits']} (was 0 before the fix)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", help="write full results to this path")
    args = parser.parse_args()

    if not corpus_available():
        print("vertical-jp corpus absent — nothing to benchmark.")
        return 0

    results = run_benchmark()
    _print_table(results)
    if args.json:
        Path(args.json).write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
