#!/usr/bin/env python
"""
scripts/benchmark_embedding_models.py

Live benchmark: compare 4 fastembed models on the existing ground-truth
corpus and recommend whether to change the default embedding model.

Each of 4 fastembed models is run against the 7 hand-annotated scenarios
in benchmark_data/ground_truth.json. Metrics: per-scenario recall, RR;
aggregate MRR; p50 warm-cache query latency. Decision gate: a challenger
replaces the default iff its MRR is at least baseline + 0.05 AND its p50
latency is at most 1.5x the baseline's. The script does not edit docs;
it prints a copy-pasteable markdown block for docs/embedding-models.md.

Run:
    python scripts/benchmark_embedding_models.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pdf_mcp.server import pdf_search  # noqa: E402

# ── Models under test ───────────────────────────────────────────────
MODELS = [
    {
        "name": "BAAI/bge-small-en-v1.5",
        "size_mb": 67,
        "dim": 384,
        "license": "MIT",
        "mteb": 51.68,
        "is_baseline": True,
    },
    {
        "name": "snowflake/snowflake-arctic-embed-s",
        "size_mb": 130,
        "dim": 384,
        "license": "Apache 2.0",
        "mteb": 51.98,
        "is_baseline": False,
    },
    {
        "name": "BAAI/bge-base-en-v1.5",
        "size_mb": 210,
        "dim": 768,
        "license": "MIT",
        "mteb": 53.25,
        "is_baseline": False,
    },
    {
        "name": "snowflake/snowflake-arctic-embed-m",
        "size_mb": 430,
        "dim": 768,
        "license": "Apache 2.0",
        "mteb": 54.90,
        "is_baseline": False,
    },
]
BASELINE = next(m["name"] for m in MODELS if m["is_baseline"])

# Decision gate (see spec §4)
MRR_LIFT_THRESHOLD = 0.05
LATENCY_RATIO_THRESHOLD = 1.5


# ── Ground truth loader ─────────────────────────────────────────────
def load_ground_truth(path: str = "benchmark_data/ground_truth.json") -> dict:
    """Load ground truth annotations from JSON. Raises FileNotFoundError if missing."""
    gt_path = Path(path)
    if not gt_path.exists():
        raise FileNotFoundError(f"Ground truth file not found: {path}")
    with open(gt_path, encoding="utf-8") as f:
        return json.load(f)


# ── ANSI / printing helpers (duplicated from benchmark_rrf.py) ──────
_OUTPUT: list[str] = []
_IS_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _IS_TTY else text


def green(t: str) -> str:
    return _c("32", t)


def red(t: str) -> str:
    return _c("31", t)


def yellow(t: str) -> str:
    return _c("33", t)


def cyan(t: str) -> str:
    return _c("36", t)


def bold(t: str) -> str:
    return _c("1", t)


def _p(text: str = "") -> None:
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


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _compute_metrics(matches: list[dict], relevant_pages: set[int], k: int) -> dict:
    """
    Compute recall@K, RR (Reciprocal Rank), and rank-of-first-hit.

    matches: list of {"page": N, ...} from pdf_search (page is 1-indexed)
    relevant_pages: 1-indexed page numbers that are ground-truth relevant
    k: cutoff — only the first k entries in matches are considered
    """
    if not relevant_pages:
        return {"recall": 0.0, "rr": 0.0, "rank_first_hit": None}
    top_k_pages = [m["page"] for m in matches[:k]]
    recall = len(set(top_k_pages) & relevant_pages) / len(relevant_pages)
    rank_first_hit = None
    for i, page in enumerate(top_k_pages, 1):
        if page in relevant_pages:
            rank_first_hit = i
            break
    rr = 1.0 / rank_first_hit if rank_first_hit is not None else 0.0
    return {"recall": recall, "rr": rr, "rank_first_hit": rank_first_hit}


def _run_scenario(pdf_path: str, query: str, relevant_pages: set[int], k: int) -> dict:
    """
    Run one scenario in semantic mode and return per-scenario metrics.

    Returns dict with: recall, rr, rank_first_hit, top_pages.
    On pdf_search error, returns zero metrics with empty top_pages.
    """
    result = pdf_search(pdf_path, query, mode="semantic", max_results=k)
    if "error" in result:
        return {"recall": 0.0, "rr": 0.0, "rank_first_hit": None, "top_pages": []}
    matches = result.get("matches", [])
    metrics = _compute_metrics(matches, relevant_pages, k)
    return {**metrics, "top_pages": [m["page"] for m in matches[:k]]}


def run_latency_probe(pdf_path: str, query: str, k: int, n_runs: int = 3) -> float:
    """
    Run pdf_search n_runs times and return the median wall-clock time (ms).

    Caller must ensure the embedding cache is warm before invoking
    (one prior pdf_search call on this PDF is sufficient).
    """
    samples: list[float] = []
    for _ in range(n_runs):
        t0 = time.perf_counter()
        pdf_search(pdf_path, query, mode="semantic", max_results=k)
        samples.append((time.perf_counter() - t0) * 1000)
    samples.sort()
    return samples[len(samples) // 2]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Live benchmark of fastembed models for pdf-mcp default selection."
    )
    parser.add_argument(
        "--ground-truth",
        default="benchmark_data/ground_truth.json",
        help="Path to ground truth JSON (default: benchmark_data/ground_truth.json)",
    )
    args = parser.parse_args()
    # Wired up in Task 8
    _ = args
    raise NotImplementedError("main() will be implemented in Task 8")


if __name__ == "__main__":
    main()
