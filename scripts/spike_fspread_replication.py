"""Cross-corpus replication of the spread findings on the 10-K set.

The entire spread story (shape gap, width curve, ordering ceiling) rests
on 25 in-sample arXiv queries. This replays the same deterministic
measurements on 16 independently-authored financial spread queries
(spread_queries.json, 44 verified labels, 19 filings) — new corpus, new
questions, new authorship. Raw query strings are used: on the arXiv set
spread routing was measured caller-insensitive (23/25 doc-hit@3 both
raw and caller-emitted), noted as an assumption here.

Replicated measurements:
  1. shape gap:   gold docs in doc_match_counts vs in the flat top-10
  2. width curve: fan-out part coverage at k = 3/5/7/10/all-named
  3. ordering:    does decayed-vote (f1_vote) still lose to the shipped
                  first-appearance order on selection@5?

Free and deterministic. Uses the warmed spike cache.

Run:  uv run python scripts/spike_fspread_replication.py
"""

from __future__ import annotations

import json
import sys

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

FIN = REPO / "benchmark_data" / "financial_reports"
SPIKE_CACHE = REPO / "benchmark_data" / ".spike_confidence_cache"
TOP_K = 10
HOP2_RESULTS = 10
RRF_K = 60


def main() -> int:
    import pdf_mcp.server as server_module

    from pdf_mcp.cache import PDFCache
    from pdf_mcp.server import pdf_corpus_search, pdf_search

    server_module.cache = PDFCache(cache_dir=SPIKE_CACHE, ttl_hours=24 * 30)

    man = json.loads((FIN / "manifest.json").read_text(encoding="utf-8"))
    id_by_path = {str(REPO / d["path"]): d["id"] for d in man["docs"]}
    path_by_id = {v: k for k, v in id_by_path.items()}
    paths = [p for p in id_by_path if Path(p).exists()]
    queries = json.loads((FIN / "spread_queries.json").read_text(encoding="utf-8"))[
        "queries"
    ]

    rows = []
    for q in queries:
        gold: dict[str, set[int]] = {}
        for lab in q["labels"]:
            if lab.get("gain", 0) > 0:
                gold.setdefault(lab["doc"], set()).add(lab["page"])

        r = pdf_corpus_search(paths, q["query"], mode="auto", top_k=30)
        deep = [(id_by_path[m["path"]], m["page"]) for m in r["matches"]]
        flat10_docs = list(dict.fromkeys(d for d, _p in deep[:TOP_K]))
        dmc = {id_by_path[p]: c for p, c in r["doc_match_counts"].items()}
        named = list(dict.fromkeys(d for d, _p in deep)) + [
            d
            for d in sorted(dmc, key=lambda d: (-dmc[d], d))
            if d not in {x for x, _p in deep}
        ]
        votes: dict[str, float] = {}
        for rank, (d, _p) in enumerate(deep, start=1):
            votes[d] = votes.get(d, 0.0) + 1.0 / (RRF_K + rank)
        for d in dmc:
            votes.setdefault(d, 0.0)
        f1_order = sorted(votes, key=lambda d: (-votes[d], d))

        hop2: dict[str, bool] = {}

        def part(d: str) -> bool:
            if d not in hop2:
                s = pdf_search(
                    path_by_id[d], q["query"], mode="auto", max_results=HOP2_RESULTS
                )
                pages = {m["page"] for m in s.get("matches", [])}
                hop2[d] = bool(pages & gold[d])
            return hop2[d]

        row: dict = {
            "id": q["id"],
            "n_gold": len(gold),
            "flat_cov": len(set(gold) & set(flat10_docs)) / len(gold),
            "dmc_cov": len(set(gold) & set(dmc)) / len(gold),
            "named_cov": len(set(gold) & set(named)) / len(gold),
            "hop2": {d: part(d) for d in gold},
            "sel5_base": len(set(gold) & set(named[:5])) / len(gold),
            "sel5_f1": len(set(gold) & set(f1_order[:5])) / len(gold),
        }
        for k in (3, 5, 7, 10):
            row[f"cov_k{k}"] = sum(
                1 for d in gold if d in named[:k] and row["hop2"][d]
            ) / len(gold)
        row["cov_all_named"] = sum(
            1 for d in gold if d in named and row["hop2"][d]
        ) / len(gold)
        rows.append(row)

    out = FIN / "spread_replication_results.json"
    out.write_text(json.dumps({"rows": rows}, indent=1), encoding="utf-8")
    n = len(rows)
    total = sum(r["n_gold"] for r in rows)

    def mean(key: str) -> float:
        return sum(r[key] * r["n_gold"] for r in rows) / total

    print(f"wrote {out}\n")
    print(f"FINANCIAL SPREAD REPLICATION (n={n} queries, {total} gold parts)")
    print(f"  {'metric':<38}{'10-K':>8}{'arXiv ref':>10}")
    print(f"  {'dmc coverage (docs identified)':<38}{mean('dmc_cov'):>7.0%}{'93%':>10}")
    print(f"  {'flat top-10 coverage':<38}{mean('flat_cov'):>7.0%}{'75%':>10}")
    print(f"  {'width: cov @3':<38}{mean('cov_k3'):>7.0%}{'56%':>10}")
    print(f"  {'width: cov @5':<38}{mean('cov_k5'):>7.0%}{'65%':>10}")
    print(f"  {'width: cov @10':<38}{mean('cov_k10'):>7.0%}{'79%':>10}")
    print(f"  {'width: cov all-named':<38}{mean('cov_all_named'):>7.0%}{'87%':>10}")
    hop2_ceiling = sum(sum(1 for v in r["hop2"].values() if v) for r in rows) / total
    print(
        f"  {'hop-2 ceiling (right doc searched)':<38}{hop2_ceiling:>7.0%}{'94%':>10}"
    )
    print(f"  {'selection@5, shipped order':<38}{mean('sel5_base'):>7.0%}{'73%':>10}")
    print(f"  {'selection@5, decayed-vote (f1)':<38}{mean('sel5_f1'):>7.0%}{'66%':>10}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
