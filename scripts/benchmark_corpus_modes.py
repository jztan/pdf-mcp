#!/usr/bin/env python
"""
scripts/benchmark_corpus_modes.py

Retrieval quality of the PRODUCTION `pdf_corpus_search` tool, all three
modes, with REAL embeddings, against the stage-2 graded ground truth
(benchmark_data/corpus_search/queries.json, 64 queries over 100 docs).

This closes the evidence gap left by the stage-2 spike, which compared
keyword-arm DESIGNS on scratch FTS tables: here the actual tool is called
per query (isolated cache, warmed once), so the numbers measure exactly
what an agent receives, semantic and hybrid included.

Sanity cross-check: the keyword mode reuses the design the spike selected
(per-doc doc-local BM25 + RRF fusion), so its overall NDCG@10 should land
near the spike's arm-B result (~0.547 at the 100-doc re-run). A large
deviation means a wiring bug, not a quality change.

Run:  uv run python scripts/benchmark_corpus_modes.py
Writes benchmark_data/corpus_search/modes_results.{json,md}. Exits 0
(informational; the committed md is the record).
"""

from __future__ import annotations

import json
import sys
import tempfile
import time

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _retrieval_metrics as rm  # noqa: E402

DATA = REPO / "benchmark_data" / "corpus_search"
TOP_K = 10
MODES = ("keyword", "semantic", "auto")
CJK_DOCS = {"ibk_72-102", "iwaki_koho_2025-12", "chukobungaku_104-52"}


def main() -> int:
    import pdf_mcp.server as server_module
    from pdf_mcp.cache import PDFCache
    from pdf_mcp.server import pdf_corpus_search, pdf_corpus_warm

    manifest = json.loads((DATA / "manifest.json").read_text())
    queries = json.loads((DATA / "queries.json").read_text())
    id_by_path = {str(REPO / d["path"]): d["id"] for d in manifest["docs"]}
    paths = [p for p in id_by_path if Path(p).exists()]
    if len(paths) != len(manifest["docs"]):
        print(
            f"WARNING: {len(manifest['docs']) - len(paths)} manifest docs "
            "missing locally; proceeding with the rest"
        )
    if not paths:
        print("ERROR: no corpus docs available")
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        prev_cache = server_module.cache
        server_module.cache = PDFCache(cache_dir=Path(tmp), ttl_hours=24)
        try:
            t0 = time.perf_counter()
            warm = pdf_corpus_warm(paths, budget_seconds=300, embeddings=True)
            while warm.get("unprocessed"):
                warm = pdf_corpus_warm(paths, budget_seconds=300, embeddings=True)
            warm_s = time.perf_counter() - t0
            print(
                f"warmed {len(paths)} docs (text+embeddings) in {warm_s:.0f}s"
                f" ({len(warm.get('skipped', []))} skipped)"
            )

            per_mode: dict[str, dict] = {}
            for mode in MODES:
                rows: dict[str, dict] = {}
                times: list[float] = []
                reported_mode: set[str] = set()
                for q in queries["queries"]:
                    labels = {
                        (lb["doc"], lb["page"]): float(lb["gain"]) for lb in q["labels"]
                    }
                    gold_docs = {lb["doc"] for lb in q["labels"] if lb["gain"] >= 2}
                    tq = time.perf_counter()
                    res = pdf_corpus_search(paths, q["query"], mode=mode, top_k=TOP_K)
                    times.append(time.perf_counter() - tq)
                    if "error" in res:
                        print(f"ERROR {mode} {q['id']}: {res['error']}")
                        return 2
                    reported_mode.add(res["search_mode"])
                    if res["coverage"]["searched"] != len(paths):
                        print(
                            f"ERROR {mode} {q['id']}: partial coverage "
                            f"{res['coverage']}"
                        )
                        return 2
                    ranked = [
                        (id_by_path[m["path"]], m["page"]) for m in res["matches"]
                    ]
                    gains = [labels.get(item, 0.0) for item in ranked]
                    ideal = list(labels.values())
                    # Doc-level NDCG: dedupe ranked docs in rank order,
                    # gain = the doc's best labeled gain. Separates
                    # "wrong doc" from "right doc, unlabeled page" —
                    # spread labels grade 2-3 (doc, page) pairs while
                    # gold docs match on many pages, so page-level NDCG
                    # floors on label sparsity there.
                    doc_gains: dict[str, float] = {}
                    for lb in q["labels"]:
                        doc_gains[lb["doc"]] = max(
                            doc_gains.get(lb["doc"], 0.0), float(lb["gain"])
                        )
                    seen_docs: set[str] = set()
                    doc_ranked_gains = []
                    for doc_id, _pg in ranked:
                        if doc_id in seen_docs:
                            continue
                        seen_docs.add(doc_id)
                        doc_ranked_gains.append(doc_gains.get(doc_id, 0.0))
                    rows[q["id"]] = {
                        "class": q["class"],
                        "cjk": any(d in CJK_DOCS for d in gold_docs),
                        "ndcg": rm.ndcg_at_k(gains, ideal, TOP_K),
                        "doc_ndcg": rm.ndcg_at_k(
                            doc_ranked_gains, list(doc_gains.values()), TOP_K
                        ),
                        "dochit3": int(bool({d for d, _p in ranked[:3]} & gold_docs)),
                        "seconds": round(times[-1], 3),
                    }
                per_mode[mode] = {
                    "search_mode_reported": sorted(reported_mode),
                    "per_query": rows,
                    "mean_query_seconds": round(sum(times) / len(times), 3),
                }
                print(
                    f"{mode}: done ({per_mode[mode]['mean_query_seconds']}s"
                    f"/query, reported={sorted(reported_mode)})"
                )
        finally:
            server_module.cache = prev_cache

    def agg(rows: dict, key=lambda r: True) -> dict[str, float]:
        sel = [r for r in rows.values() if key(r)]
        if not sel:
            return {"ndcg": 0.0, "doc_ndcg": 0.0, "dochit3": 0.0, "n": 0}
        return {
            "ndcg": round(sum(r["ndcg"] for r in sel) / len(sel), 4),
            "doc_ndcg": round(sum(r["doc_ndcg"] for r in sel) / len(sel), 4),
            "dochit3": round(sum(r["dochit3"] for r in sel) / len(sel), 4),
            "n": len(sel),
        }

    summary: dict[str, dict] = {}
    for mode in MODES:
        rows = per_mode[mode]["per_query"]
        summary[mode] = {
            "overall": agg(rows),
            "by_class": {
                c: agg(rows, lambda r, c=c: r["class"] == c)
                for c in ("needle", "spread", "trap")
            },
            "cjk_subset": agg(rows, lambda r: r["cjk"]),
            "non_cjk": agg(rows, lambda r: not r["cjk"]),
            "mean_query_seconds": per_mode[mode]["mean_query_seconds"],
            "search_mode_reported": per_mode[mode]["search_mode_reported"],
        }

    out = {
        "corpus_docs": len(paths),
        "top_k": TOP_K,
        "queries": len(queries["queries"]),
        "summary": summary,
        "per_query": {m: per_mode[m]["per_query"] for m in MODES},
    }
    (DATA / "modes_results.json").write_text(
        json.dumps(out, indent=2, sort_keys=True) + "\n"
    )

    lines = [
        "# pdf_corpus_search mode benchmark (production tool, real embeddings)",
        "",
        f"Corpus: {len(paths)} docs. Queries: {len(queries['queries'])}"
        f" (graded ground truth, stage-2). top_k={TOP_K}. The tool itself is"
        " called per query on a warmed isolated cache, so numbers measure the"
        " agent-facing contract end to end.",
        "",
        "| mode | overall NDCG@10 | needle | spread | trap | doc-hit@3 |" " s/query |",
        "|---|---|---|---|---|---|---|",
    ]
    for mode in MODES:
        s = summary[mode]
        lines.append(
            f"| {mode} ({'/'.join(s['search_mode_reported'])}) |"
            f" {s['overall']['ndcg']:.3f} |"
            f" {s['by_class']['needle']['ndcg']:.3f} |"
            f" {s['by_class']['spread']['ndcg']:.3f} |"
            f" {s['by_class']['trap']['ndcg']:.3f} |"
            f" {s['overall']['dochit3']:.3f} |"
            f" {s['mean_query_seconds']:.2f} |"
        )
    lines += [
        "",
        "## Doc-level NDCG@10 (ranked docs deduped, gain = doc's best label)",
        "",
        'Separates "wrong doc" from "right doc, unlabeled page": spread'
        " labels grade 2-3 (doc, page) pairs while gold docs match the query"
        " on many pages, so page-level NDCG floors on label sparsity there."
        " Doc-level is the honest ceiling-side read for the spread class.",
        "",
        "| mode | overall | needle | spread | trap |",
        "|---|---|---|---|---|",
    ]
    for mode in MODES:
        s = summary[mode]
        lines.append(
            f"| {mode} | {s['overall']['doc_ndcg']:.3f} |"
            f" {s['by_class']['needle']['doc_ndcg']:.3f} |"
            f" {s['by_class']['spread']['doc_ndcg']:.3f} |"
            f" {s['by_class']['trap']['doc_ndcg']:.3f} |"
        )
    lines += [
        "",
        "## CJK subset (5 needle queries on Japanese docs; embedding model is"
        " English bge-small, so the semantic arm is expected to be weak"
        " there)",
        "",
        "| mode | CJK NDCG@10 (n=5) | non-CJK NDCG@10 |",
        "|---|---|---|",
    ]
    for mode in MODES:
        s = summary[mode]
        lines.append(
            f"| {mode} | {s['cjk_subset']['ndcg']:.3f} |"
            f" {s['non_cjk']['ndcg']:.3f} |"
        )
    lines += [
        "",
        "Sanity cross-check: keyword overall should land near the stage-2"
        " arm-B result (~0.547). Interpretation is appended by hand after"
        " the run.",
        "",
    ]
    # Preserve the hand-appended interpretation section across reruns.
    md_path = DATA / "modes_results.md"
    interp = ""
    if md_path.exists():
        prev = md_path.read_text()
        idx = prev.find("## Interpretation")
        if idx != -1:
            interp = prev[idx:]
    md_path.write_text("\n".join(lines) + interp)
    print("\nwrote modes_results.{json,md}")
    for mode in MODES:
        s = summary[mode]
        print(
            f"  {mode:8s} overall={s['overall']['ndcg']:.3f} "
            f"needle={s['by_class']['needle']['ndcg']:.3f} "
            f"spread={s['by_class']['spread']['ndcg']:.3f} "
            f"trap={s['by_class']['trap']['ndcg']:.3f} "
            f"dochit3={s['overall']['dochit3']:.3f} "
            f"doc_ndcg={s['overall']['doc_ndcg']:.3f} "
            f"(spread {s['by_class']['spread']['doc_ndcg']:.3f})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
