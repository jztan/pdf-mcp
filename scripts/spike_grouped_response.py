"""Grouped-vs-flat A/B for the corpus response shape (research doc C6).

The spread decomposition showed the ranking layer finds 93% of gold
documents while the flat top-10 response carries 75%. The measurable
core of a grouped response is a per-document quota: within the SAME
response budget, cap how many slots one document may take, so documents
the fused ranking already surfaced are not crowded out.

Arms (same 10-hit budget):
    flat      the shipped shape: first 10 hits of the fused ranking
    grouped   fused ranking taken deep (top_k=30), 10 slots filled in
              fused order with at most PER_DOC_CAP hits per document

Queries: caller-emitted where available (described, needle, spread from
the cached caller evals), raw benchmark strings otherwise (trap).
Metrics per query, against gold labels:
    doc-cov    fraction of gold docs with >=1 hit in the response
    page-hit   fraction of gold docs with a hit on a labeled gold page
    hit1       first-ranked doc is gold (do-no-harm control for
               single-gold classes; grouping must not change the head)

Free and deterministic. Uses the warmed spike cache.

Run:  uv run python scripts/spike_grouped_response.py
"""

from __future__ import annotations

import json
import sys

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

DATA = REPO / "benchmark_data" / "corpus_search"
SPIKE_CACHE = REPO / "benchmark_data" / ".spike_confidence_cache"
BUDGET = 10
DEEP_K = 30
PER_DOC_CAP = 3
DOCS_CAP = 5


def quota_fill(
    hits: list[tuple[str, int]], budget: int, cap: int
) -> list[tuple[str, int]]:
    """Fill `budget` slots in fused order, at most `cap` per document."""
    out: list[tuple[str, int]] = []
    per_doc: dict[str, int] = {}
    for doc, page in hits:
        if per_doc.get(doc, 0) >= cap:
            continue
        out.append((doc, page))
        per_doc[doc] = per_doc.get(doc, 0) + 1
        if len(out) == budget:
            break
    return out


def load_emissions() -> dict[str, str]:
    emitted: dict[str, str] = {}
    for name in ("caller_eval_results.json", "caller_eval_spread_results.json"):
        path = DATA / "c2_rewrite" / name
        if path.exists():
            for r in json.loads(path.read_text())["rows"]:
                if r.get("old_query"):
                    emitted[r["id"]] = r["old_query"]
    return emitted


def main() -> int:
    import pdf_mcp.server as server_module

    from pdf_mcp import corpus, embedder
    from pdf_mcp.cache import PDFCache
    from pdf_mcp.server import (
        _corpus_keyword_rankings,
        _corpus_semantic_scores,
        pdf_corpus_search,
    )

    server_module.cache = PDFCache(cache_dir=SPIKE_CACHE, ttl_hours=24 * 30)
    model = server_module.pdf_config.embedding_model

    manifest = json.loads((DATA / "manifest.json").read_text())
    id_by_path = {str(REPO / d["path"]): d["id"] for d in manifest["docs"]}
    paths = [p for p in id_by_path if Path(p).exists()]
    queries = json.loads((DATA / "queries.json").read_text())["queries"]
    emitted = load_emissions()

    rows = []
    for q in queries:
        text = emitted.get(q["id"], q["query"])
        gold_pages: dict[str, set[int]] = {}
        for lab in q["labels"]:
            if lab.get("gain", 0) > 0:
                gold_pages.setdefault(lab["doc"], set()).add(lab["page"])
        gold = set(gold_pages)

        r = pdf_corpus_search(paths, text, mode="auto", top_k=DEEP_K)
        deep = [(id_by_path[m["path"]], m["page"]) for m in r["matches"]]

        # doc-major arm: each doc contributes its OWN best pages, from a
        # per-doc RRF of its keyword hits and its semantic page ranking.
        # Doc order: fused first-appearance, then doc_match_counts-only
        # docs by count. Top DOCS_CAP docs, PER_DOC_CAP pages each.
        rank_lists, kw_counts, _payload = _corpus_keyword_rankings(
            paths, text, BUDGET, 200, allow_or_fallback=False
        )
        kw_by_doc = {
            id_by_path[hits[0][0]]: [p for _d, p in hits] for hits in rank_lists
        }
        qv = embedder.encode_query(text, model)
        scored, _un = _corpus_semantic_scores(paths, model, qv)
        sem_by_doc: dict[str, list[int]] = {}
        for path, page, _s in sorted(scored, key=lambda t: (-t[2], t[0], t[1])):
            sem_by_doc.setdefault(id_by_path[path], []).append(page)

        fused_docs = list(dict.fromkeys(d for d, _p in deep))
        counts_by_id = {id_by_path[p]: c for p, c in kw_counts.items()}
        dmc_docs = sorted(counts_by_id, key=lambda d: (-counts_by_id[d], d))

        def doc_sections(doc_order: list[str]) -> list[tuple[str, int]]:
            out: list[tuple[str, int]] = []
            for d in doc_order[:DOCS_CAP]:
                own = corpus.rrf_fuse_two_rankings(
                    [(d, p) for p in kw_by_doc.get(d, [])],
                    [(d, p) for p in sem_by_doc.get(d, [])[: BUDGET * 3]],
                    top_k=PER_DOC_CAP,
                )
                out.extend(own)
            return out

        arms = {
            "flat": deep[:BUDGET],
            "grouped": quota_fill(deep, BUDGET, PER_DOC_CAP),
            # doc sections ordered by fused first-appearance, then
            # doc_match_counts-only docs
            "docmajor": doc_sections(list(dict.fromkeys([*fused_docs, *dmc_docs]))),
            # doc sections ordered by keyword-hit count first (the
            # doc_match_counts signal), fused appearance as tiebreak pool
            "docmajor_kw": doc_sections(list(dict.fromkeys([*dmc_docs, *fused_docs]))),
        }
        row: dict = {
            "id": q["id"],
            "class": q["class"],
            "source": "caller" if q["id"] in emitted else "raw",
        }
        for arm, hits in arms.items():
            docs_in = list(dict.fromkeys(d for d, _p in hits))
            covered = gold & set(docs_in)
            page_hit = {d for d, p in hits if d in gold_pages and p in gold_pages[d]}
            row[f"{arm}_doc_cov"] = len(covered) / len(gold)
            row[f"{arm}_page_hit"] = len(page_hit) / len(gold)
            row[f"{arm}_hit1"] = bool(docs_in and docs_in[0] in gold)
        row["docmajor_size"] = len(arms["docmajor"])
        rows.append(row)

    out = DATA / "grouped_response_ab.json"
    out.write_text(
        json.dumps(
            {
                "budget": BUDGET,
                "deep_k": DEEP_K,
                "per_doc_cap": PER_DOC_CAP,
                "rows": rows,
            },
            indent=1,
        )
    )
    print(f"wrote {out}\n")
    print(
        f"GROUPED vs FLAT (budget={BUDGET}, deep_k={DEEP_K},"
        f" per-doc cap={PER_DOC_CAP})"
    )
    header = f"{'class':<11}{'n':>3}{'src':>7}"
    for arm in ("flat", "grouped", "docmajor", "docmajor_kw"):
        header += f" | {arm}: {'doc-cov':>7}{'page-hit':>9}{'hit1':>6}"
    print(header)
    for cls in ("needle", "trap", "spread", "described"):
        sub = [r for r in rows if r["class"] == cls]
        n = len(sub)
        src = sub[0]["source"] if sub else "?"

        def m(arm: str, k: str) -> str:
            vals = [r[f"{arm}_{k}"] for r in sub]
            return (
                f"{sum(vals) / n:.0%}"
                if k != "hit1"
                else f"{sum(bool(v) for v in vals)}/{n}"
            )

        line = f"{cls:<11}{n:>3}{src:>7}"
        for arm in ("flat", "grouped", "docmajor", "docmajor_kw"):
            line += (
                f" |       {m(arm, 'doc_cov'):>7}{m(arm, 'page_hit'):>9}"
                f"{m(arm, 'hit1'):>6}"
            )
        print(line)
    mean_size = sum(r["docmajor_size"] for r in rows) / len(rows)
    print(f"\nmean docmajor hits/response: {mean_size:.1f} (flat/grouped: 10)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
