#!/usr/bin/env python
"""
scripts/benchmark_rrf.py

Benchmark: RRF hybrid search vs keyword-only vs semantic-only.

Run synthetic scenarios (always):
    python scripts/benchmark_rrf.py

Run with a real PDF (optional — appends a "Real PDF" section):
    python scripts/benchmark_rrf.py --pdf path/to/doc.pdf \\
        --query "your query" --relevant-pages "1,3,5"

--pdf accepts a local path or a URL.
Always exits 0 (informational report, no CI gate).
"""

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pymupdf  # noqa: E402
import pdf_mcp.server as server_module  # noqa: E402
from pdf_mcp.cache import PDFCache  # noqa: E402
from pdf_mcp.server import _resolve_path, pdf_search  # noqa: E402

# Detect fastembed once at import time.
try:
    import fastembed  # type: ignore  # noqa: F401
    _FASTEMBED_AVAILABLE = True
except ImportError:
    _FASTEMBED_AVAILABLE = False

# Accumulated output lines (with ANSI) for saving to files.
_OUTPUT: list[str] = []

_IS_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _IS_TTY else text


def green(t: str) -> str:
    return _c("32", t)


def red(t: str) -> str:
    return _c("31", t)


def bold(t: str) -> str:
    return _c("1", t)


def cyan(t: str) -> str:
    return _c("36", t)


def yellow(t: str) -> str:
    return _c("33", t)


def _p(text: str = "") -> None:
    """Print a line and append to output buffer for file saving."""
    _OUTPUT.append(text)
    print(text)


def _section(title: str) -> None:
    width = 68
    _p()
    _p(bold(cyan("=" * width)))
    _p(bold(cyan(f"  {title}")))
    _p(bold(cyan("=" * width)))


def _row(label: str, value: str, ok: bool | None = None) -> None:
    marker = ""
    if ok is True:
        marker = green(" ✓")
    elif ok is False:
        marker = red(" ✗")
    _p(f"  {label:<36} {value}{marker}")


FILLER = "The ancient oak tree stood beside the quiet mountain stream."


def _build_pdf(page_texts: dict[int, str]) -> str:
    """Write a PDF to a temp file and return its absolute path. Keys are 0-indexed."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        doc = pymupdf.open()
        for i in sorted(page_texts.keys()):
            page = doc.new_page()
            page.insert_text((50, 50), page_texts[i])
        doc.save(f.name)
        doc.close()
        return str(Path(f.name).resolve())


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes for plain-text file output."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _compute_metrics(
    matches: list[dict], relevant_pages: set[int], k: int
) -> dict:
    """
    Compute recall@K and rank-of-first-hit from a matches list.

    matches: list of {"page": N, ...} from pdf_search (page is 1-indexed)
    relevant_pages: 1-indexed page numbers that are ground-truth relevant
    k: cutoff — only the first k entries in matches are considered

    Returns:
        {"recall": float, "rank_first_hit": int | None}
        recall = |relevant ∩ top_k| / |relevant|
        rank_first_hit = 1-indexed position of first relevant page, or None
    """
    top_k_pages = [m["page"] for m in matches[:k]]
    recall = len(set(top_k_pages) & relevant_pages) / len(relevant_pages)
    rank_first_hit = None
    for i, page in enumerate(top_k_pages, 1):
        if page in relevant_pages:
            rank_first_hit = i
            break
    return {"recall": recall, "rank_first_hit": rank_first_hit}


def _run_mode(
    pdf_path: str, query: str, api_mode: str, max_results: int
) -> list[dict]:
    """
    Call pdf_search for one mode and return the matches list.

    api_mode: "keyword", "semantic", or "auto" (hybrid).
    Returns [] on error (e.g. fastembed not installed for semantic mode).
    max_results is passed directly as the api parameter — recall@K is
    enforced by slicing matches[:K] in the caller.
    """
    result = pdf_search(pdf_path, query, mode=api_mode, max_results=max_results)
    if "error" in result:
        return []
    return result.get("matches", [])


def _run_scenario(
    name: str,
    pdf_path: str,
    query: str,
    relevant_pages: set[int],
    k: int,
) -> dict:
    """
    Run keyword, semantic, and hybrid search on pdf_path and return metrics.

    relevant_pages: 1-indexed page numbers that are ground-truth relevant
    k: recall cutoff (passed as max_results to pdf_search)

    Returns a scenario result dict ready for JSON serialization.
    Does NOT print anything — callers handle display.
    """
    mode_data: dict[str, dict] = {}
    for mode, api_mode in [
        ("keyword", "keyword"),
        ("semantic", "semantic"),
        ("hybrid", "auto"),
    ]:
        matches = _run_mode(pdf_path, query, api_mode, max_results=k)
        metrics = _compute_metrics(matches, relevant_pages, k)
        mode_data[mode] = {
            "recall": metrics["recall"],
            "rank_first_hit": metrics["rank_first_hit"],
            "top_pages": [m["page"] for m in matches[:k]],
        }
    return {
        "name": name,
        "query": query,
        "k": k,
        "relevant_pages": sorted(relevant_pages),
        "modes": mode_data,
    }


def _print_scenario_table(result: dict, assertions: dict) -> None:
    """
    Print the mode comparison table for one scenario.

    result: dict from _run_scenario
    assertions: {key: bool | None}  — None means N/A (fastembed absent)
    """
    k = result["k"]
    relevant = set(result["relevant_pages"])
    n_relevant = len(relevant)

    _p()
    _p(f"  Query: {bold(repr(result['query']))}   K={k}")
    _p()
    _p(f"  {'Mode':<10} {'Recall@' + str(k):<12} {'Rank-1st':<10} {'Top-' + str(k) + ' pages'}")
    _p(f"  {'─' * 9}  {'─' * 10}  {'─' * 8}  {'─' * 20}")

    for mode in ("keyword", "semantic", "hybrid"):
        d = result["modes"][mode]
        recall = d["recall"]
        rank = d["rank_first_hit"]
        top = ", ".join(str(p) for p in d["top_pages"]) or "(none)"

        hits = int(round(recall * n_relevant))
        recall_str = f"{hits}/{n_relevant}  {recall * 100:.0f}%"
        rank_str = f"rank {rank}" if rank is not None else "∞"

        suffix = ""
        if mode == "hybrid":
            hybrid_key = next(
                (key for key in assertions if "hybrid" in key), None
            )
            if hybrid_key and assertions[hybrid_key] is True:
                suffix = f"  {green('✓')}"

        _p(f"  {mode:<10} {recall_str:<12} {rank_str:<10} {top}{suffix}")


def main() -> None:
    _p(bold("\npdf-mcp RRF Hybrid Search — Benchmark Report"))
    _p("─" * 68)
    sys.exit(0)


if __name__ == "__main__":
    main()
