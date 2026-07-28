"""Measure the DESIGNED spread workflow end-to-end: search, then fan out.

The tool description tells callers with a multi-document question to
re-ask per document, using `doc_match_counts` to decide which. Every
prior spread number measured a single call (gold-page delivery 48%);
this is the first measurement of the workflow itself.

Hop 1: `pdf_corpus_search` (hybrid, top_k=10) with the caller-emitted
query. Hop 2: full single-doc `pdf_search` on each of the first K
documents the response names. Two selection policies:

    fused   distinct documents in fused-match order only
    named   fused order, then remaining `doc_match_counts` documents by
            count (the response's full document knowledge)

Metrics per query, against multi-doc gold labels:
    part coverage   fraction of gold docs whose gold page came back in
                    that doc's hop-2 results (the answer's parts)
    complete        every gold doc's part assembled
    calls           1 + K searches

NOT the rejected two-hop (§8 item 8): that was a routing workaround for
single-gold described questions, capped by routing order. This feeds on
doc_match_counts (93% gold-doc coverage on spread) and asks whether the
documented multi-document workflow assembles the answer.

Free and deterministic. Uses the warmed spike cache.

Run:  uv run python scripts/spike_spread_fanout.py
"""

from __future__ import annotations

import json
import sys

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

DATA = REPO / "benchmark_data" / "corpus_search"
SPIKE_CACHE = REPO / "benchmark_data" / ".spike_confidence_cache"
TOP_K = 10
HOP2_RESULTS = 10
FANOUT_KS = (2, 3, 5)


def main() -> int:
    import pdf_mcp.server as server_module

    from pdf_mcp.cache import PDFCache
    from pdf_mcp.server import pdf_corpus_search, pdf_search

    server_module.cache = PDFCache(cache_dir=SPIKE_CACHE, ttl_hours=24 * 30)

    manifest = json.loads((DATA / "manifest.json").read_text())
    id_by_path = {str(REPO / d["path"]): d["id"] for d in manifest["docs"]}
    path_by_id = {v: k for k, v in id_by_path.items()}
    paths = [p for p in id_by_path if Path(p).exists()]
    queries = [
        q
        for q in json.loads((DATA / "queries.json").read_text())["queries"]
        if q["class"] == "spread"
    ]
    emitted = {
        r["id"]: r["old_query"]
        for r in json.loads(
            (DATA / "c2_rewrite" / "caller_eval_spread_results.json").read_text()
        )["rows"]
        if r.get("old_query")
    }

    rows = []
    for q in queries:
        text = emitted[q["id"]]
        gold_pages: dict[str, set[int]] = {}
        for lab in q["labels"]:
            if lab.get("gain", 0) > 0:
                gold_pages.setdefault(lab["doc"], set()).add(lab["page"])

        r = pdf_corpus_search(paths, text, mode="auto", top_k=TOP_K)
        fused_docs = list(dict.fromkeys(id_by_path[m["path"]] for m in r["matches"]))
        counts = {id_by_path[p]: c for p, c in r["doc_match_counts"].items()}
        named_docs = fused_docs + [
            d
            for d in sorted(counts, key=lambda d: (-counts[d], d))
            if d not in fused_docs
        ]

        # hop 2 once per candidate doc, reused across policies and Ks
        hop2: dict[str, bool] = {}

        def part_found(doc_id: str) -> bool:
            if doc_id not in hop2:
                if doc_id not in gold_pages:
                    hop2[doc_id] = False
                else:
                    s = pdf_search(
                        path_by_id[doc_id],
                        text,
                        mode="auto",
                        max_results=HOP2_RESULTS,
                    )
                    pages = {m["page"] for m in s.get("matches", [])}
                    hop2[doc_id] = bool(pages & gold_pages[doc_id])
            return hop2[doc_id]

        row: dict = {"id": q["id"], "gold": sorted(gold_pages)}
        for policy, order in (("fused", fused_docs), ("named", named_docs)):
            for k in FANOUT_KS:
                selected = order[:k]
                parts = [d for d in gold_pages if d in selected and part_found(d)]
                row[f"{policy}_k{k}_cov"] = len(parts) / len(gold_pages)
                row[f"{policy}_k{k}_complete"] = len(parts) == len(gold_pages)
        rows.append(row)

    out = DATA / "spread_fanout_results.json"
    out.write_text(
        json.dumps(
            {"top_k": TOP_K, "hop2_results": HOP2_RESULTS, "rows": rows},
            indent=1,
        )
    )
    print(f"wrote {out}\n")
    n = len(rows)
    print(f"SPREAD FAN-OUT WORKFLOW (n={n}, caller-emitted queries, hybrid)")
    print("single-call baselines: gold-page delivery 48%, doc coverage 75%\n")
    print(f"{'policy':<8}{'k':>3}{'calls':>7}{'part coverage':>15}{'complete':>10}")
    for policy in ("fused", "named"):
        for k in FANOUT_KS:
            cov = sum(r[f"{policy}_k{k}_cov"] for r in rows) / n
            comp = sum(r[f"{policy}_k{k}_complete"] for r in rows)
            print(f"{policy:<8}{k:>3}{1 + k:>7}{cov:>14.0%}{comp:>8}/{n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
