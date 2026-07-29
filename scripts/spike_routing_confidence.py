"""Calibrate a routing-confidence signal for pdf_corpus_search (C3 spike).

Replays the hybrid mode's two arms (keyword-fused and semantic rankings)
for every graded query in the corpus benchmark, computes cheap
agreement/strength signals available at fusion time, and measures how
well each signal predicts routing failure (gold document absent from the
top of the fused doc ranking). This is the offline calibration step for
the `routing_confidence` response field: the signal must discriminate
(AUC), and the chosen threshold must flag described-class failures
without punishing needle/trap successes.

Free and deterministic (no LLM). Uses a persistent spike cache so the
100-doc warm (~3 min with embeddings) happens once.

Usage:
    uv run python scripts/spike_routing_confidence.py
    uv run python scripts/spike_routing_confidence.py \
        --data-dir <variant dir>   # e.g. the C2 rewritten queries

Signals (computed exactly from what the hybrid path already has):
    doc_jaccard5   Jaccard of the first-5 distinct docs of each arm
    doc_overlap3   |top-3 docs kw ∩ top-3 docs sem| / 3
    sem_top1       best semantic cosine across the corpus
    sem_nqc        std-dev of the top-10 semantic cosines (NQC)
    kw_cov_max     best per-doc fraction of query terms covered
    kw_ndocs       number of docs with >=1 keyword hit
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

DEFAULT_DATA = REPO / "benchmark_data" / "corpus_search"
DEFAULT_CACHE = REPO / "benchmark_data" / ".spike_confidence_cache"
TOP_K = 10


def distinct_docs(ranking: list[tuple[str, int]], limit: int) -> list[str]:
    seen: list[str] = []
    for path, _page in ranking:
        if path not in seen:
            seen.append(path)
            if len(seen) == limit:
                break
    return seen


def auc(pairs: list[tuple[float, bool]]) -> float | None:
    """Mann-Whitney AUC: P(signal(success) > signal(failure)), ties 0.5."""
    pos = [s for s, ok in pairs if ok]
    neg = [s for s, ok in pairs if not ok]
    if not pos or not neg:
        return None
    wins = 0.0
    for p in pos:
        for n in neg:
            wins += 1.0 if p > n else (0.5 if p == n else 0.0)
    return wins / (len(pos) * len(neg))


def youden_threshold(pairs: list[tuple[float, bool]]) -> tuple[float, float, float]:
    """Threshold t maximizing TPR-FPR for 'signal < t predicts failure'.

    Returns (threshold, flagged_failure_rate, flagged_success_rate).
    """
    best = (0.0, 0.0, 0.0, -1.0)
    candidates = sorted({s for s, _ok in pairs})
    fails = [s for s, ok in pairs if not ok]
    succs = [s for s, ok in pairs if ok]
    for t in candidates:
        tpr = sum(1 for s in fails if s < t) / len(fails) if fails else 0.0
        fpr = sum(1 for s in succs if s < t) / len(succs) if succs else 0.0
        j = tpr - fpr
        if j > best[3]:
            best = (t, tpr, fpr, j)
    return best[0], best[1], best[2]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    ap.add_argument(
        "--json-out",
        type=Path,
        help="where to write per-query signals (default <data-dir>/"
        "routing_confidence_signals.json)",
    )
    args = ap.parse_args(argv)
    data = args.data_dir if args.data_dir.is_absolute() else REPO / args.data_dir

    import numpy as np  # noqa: F401  (embeddings path needs it)

    import pdf_mcp.server as server_module

    from pdf_mcp import corpus, embedder
    from pdf_mcp.cache import PDFCache
    from pdf_mcp.server import (
        _corpus_coverage_scores,
        _corpus_keyword_rankings,
        _corpus_query_terms,
        _corpus_semantic_scores,
        _doc_covered_terms,
    )

    manifest = json.loads((data / "manifest.json").read_text())
    queries = json.loads((data / "queries.json").read_text())["queries"]
    id_by_path = {str(REPO / d["path"]): d["id"] for d in manifest["docs"]}
    paths = [p for p in id_by_path if Path(p).exists()]
    if len(paths) != len(manifest["docs"]):
        print(f"WARNING: {len(manifest['docs']) - len(paths)} docs missing")

    args.cache_dir.mkdir(parents=True, exist_ok=True)
    server_module.cache = PDFCache(cache_dir=args.cache_dir, ttl_hours=24 * 30)
    model = server_module.pdf_config.embedding_model

    def _embed(texts: list[str]) -> list[bytes]:
        return [v.tobytes() for v in embedder.encode(texts, model)]

    t0 = time.time()
    warm = corpus.warm_docs(
        paths,
        600,
        server_module.cache,
        embeddings=True,
        model_name=model,
        embed=_embed,
    )
    print(
        f"warm: {len(warm['docs'])} ready, {warm['warmed_this_call']} warmed"
        f" this call, {time.time() - t0:.0f}s"
    )
    ready = [row["path"] for row in warm["docs"]]

    rows: list[dict] = []
    for q in queries:
        query = q["query"]
        gold = {lab["doc"] for lab in q["labels"] if lab.get("gain", 0) > 0}

        rank_lists, _counts, kw_payload = _corpus_keyword_rankings(
            ready, query, TOP_K, 200, allow_or_fallback=False
        )
        kw_terms = _corpus_query_terms(query)
        kw_covered = {
            hits[0][0]: _doc_covered_terms(hits[0][0], [p for _d, p in hits], kw_terms)
            for hits in rank_lists
        }
        kw_doc_scores = _corpus_coverage_scores(kw_covered)
        kw_scores = {
            item: kw_doc_scores.get(hits[0][0], 0.0)
            for hits in rank_lists
            for item in hits
        }
        kw_fused = corpus.rrf_fuse_doc_rankings(
            rank_lists, top_k=TOP_K, scores=kw_scores
        )

        query_vec = embedder.encode_query(query, model)
        scored, _un = _corpus_semantic_scores(ready, model, query_vec)
        sem_sorted = sorted(scored, key=lambda t: (-t[2], t[0], t[1]))
        sem_ranking = [(p, pg) for p, pg, _s in sem_sorted[: TOP_K * 3]]
        sem_scores = [s for _p, _pg, s in sem_sorted[:TOP_K]]

        fused_scored = corpus.rrf_fuse_two_rankings_scored(
            kw_fused, sem_ranking, top_k=TOP_K
        )
        fused_docs = distinct_docs([i for i, _s in fused_scored], TOP_K)
        fused_ids = [id_by_path[p] for p in fused_docs]

        kw_docs5 = set(distinct_docs(kw_fused, 5))
        sem_docs5 = set(distinct_docs(sem_ranking, 5))
        kw_docs3 = set(distinct_docs(kw_fused, 3))
        sem_docs3 = set(distinct_docs(sem_ranking, 3))
        union5 = kw_docs5 | sem_docs5
        signals = {
            "doc_jaccard5": (
                len(kw_docs5 & sem_docs5) / len(union5) if union5 else 0.0
            ),
            "doc_overlap3": len(kw_docs3 & sem_docs3) / 3.0,
            "sem_top1": sem_scores[0] if sem_scores else 0.0,
            "sem_nqc": (statistics.pstdev(sem_scores) if len(sem_scores) > 1 else 0.0),
            "sem_cv": (
                statistics.pstdev(sem_scores) / statistics.mean(sem_scores)
                if len(sem_scores) > 1 and statistics.mean(sem_scores) > 0
                else 0.0
            ),
            "sem_gap_rel": (
                (sem_scores[0] - sem_scores[-1]) / sem_scores[0]
                if len(sem_scores) > 1 and sem_scores[0] > 0
                else 0.0
            ),
            "kw_cov_max": (
                max(
                    (len(v) / len(kw_terms) for v in kw_covered.values()),
                    default=0.0,
                )
                if kw_terms
                else 0.0
            ),
            "kw_ndocs": float(len(rank_lists)),
        }
        rows.append(
            {
                "id": q["id"],
                "class": q["class"],
                "gold": sorted(gold),
                "fused_docs": fused_ids,
                "hit1": bool(gold & set(fused_ids[:1])),
                "hit3": bool(gold & set(fused_ids[:3])),
                "signals": signals,
            }
        )
        print(
            f"{q['id']:<14} hit3={int(rows[-1]['hit3'])} "
            + " ".join(f"{k}={v:.3f}" for k, v in signals.items())
        )

    out = args.json_out or (data / "routing_confidence_signals.json")
    out.write_text(
        json.dumps(
            {
                "data_dir": str(data),
                "model": model,
                "top_k": TOP_K,
                "queries": rows,
            },
            indent=1,
        )
    )
    print(f"\nwrote {out}")

    # ── calibration report ────────────────────────────────────────────
    signal_names = list(rows[0]["signals"].keys())
    print(f"\nAUC (signal predicts routing hit@3; n={len(rows)}):")
    print(f"{'signal':<14}{'all':>8}{'described':>11}{'non-desc':>10}")
    for name in signal_names:
        allp = [(r["signals"][name], r["hit3"]) for r in rows]
        desc = [p for p, r in zip(allp, rows) if r["class"] == "described"]
        rest = [p for p, r in zip(allp, rows) if r["class"] != "described"]

        def fmt(v: float | None) -> str:
            return f"{v:.3f}" if v is not None else "  n/a"

        print(
            f"{name:<14}{fmt(auc(allp)):>8}{fmt(auc(desc)):>11}" f"{fmt(auc(rest)):>10}"
        )

    print("\nYouden threshold per signal (flag = signal < t, outcome hit@3):")
    print(
        f"{'signal':<14}{'t':>8}{'flags fail%':>12}{'flags succ%':>12}"
        f"{'ndl/trap FP':>12}"
    )
    for name in signal_names:
        allp = [(r["signals"][name], r["hit3"]) for r in rows]
        t, tpr, fpr = youden_threshold(allp)
        nt = [r for r in rows if r["class"] in ("needle", "trap") and r["hit3"]]
        nt_fp = sum(1 for r in nt if r["signals"][name] < t) / len(nt) if nt else 0.0
        print(f"{name:<14}{t:>8.3f}{tpr:>11.0%}{fpr:>12.0%}{nt_fp:>12.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
