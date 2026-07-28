"""Is the spread class a ranking problem or a response-shape problem?

The ledger's open question, answered deterministically. For each spread
query, compare three views of the same hybrid corpus-search response:

  flat-cov  fraction of gold docs represented in the flat `matches` list
  dmc-cov   fraction of gold docs present in `doc_match_counts` (the
            field whose documented purpose is exactly this case)
  top-share fraction of the top_k slots taken by the first-ranked doc

If dmc-cov exceeds flat-cov, the ranking layer found the documents and
the flat response shape discarded them; grouping (research doc C6), not
ranking work, is the fix.

Free and deterministic. Uses the warmed spike cache.

Run:  uv run python scripts/spike_spread_shape.py
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


def main() -> int:
    import pdf_mcp.server as server_module

    from pdf_mcp.cache import PDFCache
    from pdf_mcp.server import pdf_corpus_search

    server_module.cache = PDFCache(cache_dir=SPIKE_CACHE, ttl_hours=24 * 30)

    manifest = json.loads((DATA / "manifest.json").read_text())
    id_by_path = {str(REPO / d["path"]): d["id"] for d in manifest["docs"]}
    paths = [p for p in id_by_path if Path(p).exists()]
    queries = [
        q
        for q in json.loads((DATA / "queries.json").read_text())["queries"]
        if q["class"] == "spread"
    ]

    rows = []
    for q in queries:
        gold = {lab["doc"] for lab in q["labels"] if lab.get("gain", 0) > 0}
        r = pdf_corpus_search(paths, q["query"], mode="auto", top_k=TOP_K)
        match_docs = [id_by_path[m["path"]] for m in r["matches"]]
        distinct = list(dict.fromkeys(match_docs))
        dmc = {id_by_path[p] for p in r["doc_match_counts"]}
        rows.append(
            {
                "id": q["id"],
                "gold": len(gold),
                "flat_cov": len(gold & set(distinct)) / len(gold),
                "dmc_cov": len(gold & dmc) / len(gold),
                "top_share": (
                    match_docs.count(distinct[0]) / len(match_docs)
                    if match_docs
                    else 0.0
                ),
            }
        )

    n = len(rows)
    out = DATA / "spread_shape_decomposition.json"
    out.write_text(json.dumps({"top_k": TOP_K, "rows": rows}, indent=1))
    print(f"wrote {out}\n")
    print(f"SPREAD DECOMPOSITION (n={n}, raw strings, hybrid, top_k={TOP_K})")
    for key, label in (
        ("flat_cov", "gold docs represented in flat matches"),
        ("dmc_cov", "gold docs present in doc_match_counts"),
    ):
        mean = sum(r[key] for r in rows) / n
        full = sum(1 for r in rows if r[key] == 1)
        print(f"  {label:<38}: mean {mean:.0%}  (all-gold {full}/{n})")
    print(
        f"  {'top-doc share of the flat slots':<38}:"
        f" mean {sum(r['top_share'] for r in rows) / n:.0%}"
    )
    print("\n  id          gold  flat-cov  dmc-cov  top-doc-share")
    for r in rows:
        flag = "  <-- shape gap" if r["dmc_cov"] > r["flat_cov"] else ""
        print(
            f"  {r['id']:<11} {r['gold']:>3}  {r['flat_cov']:>7.0%}"
            f"  {r['dmc_cov']:>6.0%}  {r['top_share']:>8.0%}{flag}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
