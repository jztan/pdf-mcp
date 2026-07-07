#!/usr/bin/env python
"""
scripts/archive/spike_rerank.py  —  SPIKE (not a shipped feature)

Question: does a local cross-encoder reranker over the auto-mode (hybrid RRF)
top-K improve NDCG@10 on the graded corpus enough to justify shipping it, and
at what latency cost?

Method (measurement only — no change to server.py / the tool API):
  1. For each of the 28 graded queries, get the auto-mode top-POOL matches with
     paragraph excerpts (the text a real `rerank=True` would have in hand).
  2. Rerank those candidates with fastembed's TextCrossEncoder over
     (query, excerpt) pairs; reorder; recompute NDCG@10.
  3. Compare baseline-auto NDCG@10 vs reranked NDCG@10, overall and per query
     class, and report per-query rerank latency.

Gate (same bar prior retrieval work was held to):
  ship only if mean NDCG@10 lift >= +0.05 AND rerank latency stays modest,
  without regressing the clean lexical cases.

Runs against a hermetic corpus-only cache (reusing benchmark_rrf's isolation)
so bm25() IDF is reproducible. Always exits 0 — informational.

    uv run python scripts/archive/spike_rerank.py
    uv run python scripts/archive/spike_rerank.py \
        --model BAAI/bge-reranker-base --input page
"""

import argparse
import collections
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))  # for _retrieval_metrics

import _retrieval_metrics as _rm  # noqa: E402
from benchmark_rrf import _isolated_corpus_cache  # noqa: E402
from pdf_mcp.server import _resolve_path, pdf_search  # noqa: E402

import fitz  # noqa: E402
from fastembed.rerank.cross_encoder import TextCrossEncoder  # noqa: E402

# Cross-encoders truncate ~512 tokens; cap page text so the tail isn't dropped
# mid-scoring. ~2000 chars ≈ 500 tokens.
_PAGE_CHARS = 2000
_page_cache: dict = {}


def _page_text(path: str, page_1indexed: int) -> str:
    """Truncated plain text of a page (cached). Page label grain = whole page,
    so this is the fair rerank input vs a single excerpt block."""
    key = (path, page_1indexed)
    if key not in _page_cache:
        with fitz.open(path) as doc:
            txt = doc[page_1indexed - 1].get_text()
        _page_cache[key] = txt[:_PAGE_CHARS]
    return _page_cache[key]


def _ndcg(matches, labels, k=10):
    """NDCG@k of a ranked matches list against per-page graded labels."""
    gains = [float(labels.get(str(m["page"]), 0)) for m in matches]
    ideal = [float(g) for g in labels.values()]
    return _rm.ndcg_at_k(gains, ideal, k)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="Xenova/ms-marco-MiniLM-L-6-v2")
    ap.add_argument(
        "--pool",
        type=int,
        default=20,
        help="candidates to retrieve and rerank (top-K pool)",
    )
    ap.add_argument("--k", type=int, default=10, help="NDCG cutoff")
    ap.add_argument(
        "--input",
        choices=("excerpt", "page"),
        default="excerpt",
        help="text fed to the cross-encoder: the returned paragraph excerpt "
        "(realistic feature) or truncated page text (fair vs page labels)",
    )
    args = ap.parse_args()

    corpus = json.loads(
        Path("benchmark_data/rrf_v2_queries.json").read_text("utf-8")
    )
    gt = json.loads(Path("benchmark_data/ground_truth.json").read_text("utf-8"))

    print(f"Loading cross-encoder: {args.model} ...", flush=True)
    ce = TextCrossEncoder(model_name=args.model)

    rows = []  # per-query records
    latencies = []

    with _isolated_corpus_cache():
        for q in corpus["queries"]:
            meta = gt["pdfs"][q["pdf"]]
            path, err = _resolve_path(meta["url"])
            if err:
                raise RuntimeError(err["error"])

            res = pdf_search(
                path,
                q["query"],
                mode="auto",
                max_results=args.pool,
                excerpt_style="paragraph",
            )
            matches = res.get("matches", []) if not res.get("error") else []
            base_ndcg = _ndcg(matches, q["labels"], args.k)

            # Rerank the pool on either the returned excerpt or page text.
            if args.input == "page":
                docs = [_page_text(path, m["page"]) for m in matches]
            else:
                docs = [m.get("excerpt", "") or "" for m in matches]
            if docs:
                t0 = time.perf_counter()
                scores = list(ce.rerank(q["query"], docs))
                latencies.append((time.perf_counter() - t0) * 1000)
                order = sorted(
                    range(len(matches)), key=lambda i: scores[i], reverse=True
                )
                reranked = [matches[i] for i in order]
            else:
                reranked = matches
            rr_ndcg = _ndcg(reranked, q["labels"], args.k)

            rows.append(
                {
                    "id": q["id"],
                    "class": q.get("class", "?"),
                    "base": base_ndcg,
                    "rerank": rr_ndcg,
                    "delta": rr_ndcg - base_ndcg,
                    "n": len(matches),
                }
            )

    # ── report ────────────────────────────────────────────────────────────
    n = len(rows)
    mean_base = sum(r["base"] for r in rows) / n
    mean_rr = sum(r["rerank"] for r in rows) / n
    lift = mean_rr - mean_base
    improved = sum(1 for r in rows if r["delta"] > 1e-9)
    regressed = sum(1 for r in rows if r["delta"] < -1e-9)
    med_lat = sorted(latencies)[len(latencies) // 2] if latencies else 0.0
    max_lat = max(latencies) if latencies else 0.0

    print("\n" + "=" * 64)
    print(f"  Cross-encoder rerank spike — {args.model}")
    print(f"  pool={args.pool}  NDCG@{args.k}  queries={n}")
    print("=" * 64)
    print(f"  auto (baseline) NDCG@{args.k}:  {mean_base:.4f}")
    print(f"  auto + rerank   NDCG@{args.k}:  {mean_rr:.4f}")
    print(f"  lift:                    {lift:+.4f}")
    print(f"  improved / regressed / flat:  {improved} / {regressed}"
          f" / {n - improved - regressed}")
    print(f"  rerank latency (ms/query):  median {med_lat:.1f}  max {max_lat:.1f}")

    print("\n  Per query class:")
    by_class = collections.defaultdict(list)
    for r in rows:
        by_class[r["class"]].append(r)
    print(f"    {'class':<18} {'base':>7} {'rerank':>8} {'lift':>8}  n")
    for cls in sorted(by_class):
        g = by_class[cls]
        b = sum(x["base"] for x in g) / len(g)
        rr = sum(x["rerank"] for x in g) / len(g)
        print(f"    {cls:<18} {b:>7.3f} {rr:>8.3f} {rr - b:>+8.3f}  {len(g)}")

    # biggest regressions — the lexical-clean cases we must not break
    regr = sorted([r for r in rows if r["delta"] < -1e-9], key=lambda r: r["delta"])
    if regr:
        print("\n  Largest regressions:")
        for r in regr[:5]:
            print(f"    {r['id']:<12} {r['class']:<16}"
                  f" {r['base']:.3f} -> {r['rerank']:.3f}  ({r['delta']:+.3f})")

    print("\n  " + "-" * 60)
    gate_lift = lift >= 0.05
    print(f"  GATE  lift >= +0.05 : {'PASS' if gate_lift else 'FAIL'}"
          f"  ({lift:+.4f})")
    print(f"  Reference: median +{med_lat:.0f}ms/query added latency")
    print()


if __name__ == "__main__":
    main()
