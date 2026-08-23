"""Race fan-out document orderings on the spread selection deficiency.

Deficiency (spread_fanout_verdict.md): selection@5 = 73% — 17 of 62 gold
answer-parts sit at position 6+ or unnamed in the current fan-out order
(fused first-appearance, then unordered hit counts). Hop-2 ceiling 94%.

Candidates (corpus-routing-research.md §7, three-stream sweep):
  base      current order: fused first-appearance + doc_match_counts
  f1_vote   decayed-vote aggregation: doc score = sum over its pages in
            the deep fused ranking of 1/(K+rank)  (ReDDE-exact / Rank-S
            / PARM RRF-of-evidence; K = the existing CORPUS_RRF_K)
  f1_lex    knob-free best-then-count: sort by (best fused rank, then
            page count in the deep ranking)
  f2_xquad  residual-term-coverage greedy re-rank over f1_vote's top 20:
            next doc maximizes (1-a)*rel_norm + a*uncovered-term gain
            (a = 0.5, the published xQuAD default)
  f3_docrrf RRF fuse of two doc-level rankings: keyword coverage-score
            order and semantic decayed-vote order
  f1f2      f2 applied to f1_vote relevance (the composed frontrunner)

Gates (pre-registered): selection@3/@5 over the 62 gold parts;
permutation invariance (document input order must not change any
ordering); end-to-end part coverage at k=5 for the winner. Constants are
knob-free or literature defaults; the 25 spread queries are IN-SAMPLE —
corpus expansion is the quality loop's next step, not skipped silently.

Free and deterministic. Uses the warmed spike cache.

Run:  uv run python scripts/spike_fanout_ordering.py
"""

from __future__ import annotations

import json
import sys

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

DATA = REPO / "benchmark_data" / "corpus_search"
SPIKE_CACHE = REPO / "benchmark_data" / ".spike_confidence_cache"
DEEP_K = 50
HOP2_RESULTS = 10
XQUAD_ALPHA = 0.5
XQUAD_POOL = 20


def order_docs(
    variant: str,
    fused: list[tuple[str, int]],
    kw_counts: dict[str, int],
    kw_cov_score: dict[str, float],
    sem_pages: list[str],
    doc_terms: dict[str, set],
    all_terms: set,
    rrf_k: int,
) -> list[str]:
    """Return doc ids best-first under `variant`. Ties break by doc id."""
    votes: dict[str, float] = {}
    best_rank: dict[str, int] = {}
    page_count: dict[str, int] = {}
    for rank, (doc, _page) in enumerate(fused, start=1):
        votes[doc] = votes.get(doc, 0.0) + 1.0 / (rrf_k + rank)
        best_rank.setdefault(doc, rank)
        page_count[doc] = page_count.get(doc, 0) + 1
    for doc in kw_counts:  # named-but-unranked docs join the pool
        votes.setdefault(doc, 0.0)
        best_rank.setdefault(doc, len(fused) + 1)
        page_count.setdefault(doc, 0)

    if variant == "base":
        first = list(dict.fromkeys(d for d, _p in fused))
        rest = sorted(
            (d for d in kw_counts if d not in first),
            key=lambda d: (-kw_counts[d], d),
        )
        return first + rest
    if variant == "f1_vote":
        return sorted(votes, key=lambda d: (-votes[d], d))
    if variant == "f1_lex":
        return sorted(votes, key=lambda d: (best_rank[d], -page_count[d], d))
    if variant == "f3_docrrf":
        kw_order = sorted(
            (d for d in votes if kw_cov_score.get(d, 0) > 0),
            key=lambda d: (-kw_cov_score[d], d),
        )
        sem_votes: dict[str, float] = {}
        for rank, doc in enumerate(sem_pages, start=1):
            sem_votes[doc] = sem_votes.get(doc, 0.0) + 1.0 / (rrf_k + rank)
        sem_order = sorted(sem_votes, key=lambda d: (-sem_votes[d], d))
        fused_score: dict[str, float] = {}
        for order in (kw_order, sem_order):
            for rank, doc in enumerate(order, start=1):
                fused_score[doc] = fused_score.get(doc, 0.0) + 1.0 / (rrf_k + rank)
        return sorted(fused_score, key=lambda d: (-fused_score[d], d))

    # xQuAD-lite greedy over a relevance base: "f2_base" diversifies the
    # current base ordering (isolates F2's contribution); "f2_vote"
    # diversifies f1_vote relevance (the composed F1+F2 arm).
    if variant == "f2_base":
        first = list(dict.fromkeys(d for d, _p in fused))
        rest = sorted(
            (d for d in kw_counts if d not in first),
            key=lambda d: (-kw_counts[d], d),
        )
        base_order = first + rest
        rel = {d: 1.0 / (i + 1) for i, d in enumerate(base_order)}
    else:
        rel = votes
    pool = sorted(rel, key=lambda d: (-rel[d], d))[:XQUAD_POOL]
    max_rel = max((rel[d] for d in pool), default=1.0) or 1.0
    picked: list[str] = []
    uncovered = set(all_terms)
    while pool:

        def gain(d: str) -> float:
            cov = len(doc_terms.get(d, set()) & uncovered) / max(1, len(all_terms))
            return (1 - XQUAD_ALPHA) * (rel[d] / max_rel) + XQUAD_ALPHA * cov

        nxt = max(pool, key=lambda d: (gain(d), -pool.index(d)))
        picked.append(nxt)
        pool.remove(nxt)
        uncovered -= doc_terms.get(nxt, set())
    remainder = [d for d in sorted(rel, key=lambda d: (-rel[d], d)) if d not in picked]
    return picked + remainder


def main() -> int:
    import pdf_mcp.server as server_module

    from pdf_mcp import corpus, embedder
    from pdf_mcp.cache import PDFCache
    from pdf_mcp.server import (
        _corpus_coverage_scores,
        _corpus_keyword_rankings,
        _corpus_query_terms,
        _corpus_semantic_scores,
        _doc_covered_terms,
        pdf_search,
    )

    server_module.cache = PDFCache(cache_dir=SPIKE_CACHE, ttl_hours=24 * 30)
    model = server_module.pdf_config.embedding_model

    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    id_by_path = {str(REPO / d["path"]): d["id"] for d in manifest["docs"]}
    path_by_id = {v: k for k, v in id_by_path.items()}
    paths = [p for p in id_by_path if Path(p).exists()]
    queries = [
        q
        for q in json.loads((DATA / "queries.json").read_text(encoding="utf-8"))[
            "queries"
        ]
        if q["class"] == "spread"
    ]
    emitted = {
        r["id"]: r["old_query"]
        for r in json.loads(
            (DATA / "c2_rewrite" / "caller_eval_spread_results.json").read_text(
                encoding="utf-8"
            )
        )["rows"]
    }

    variants = ("base", "f1_vote", "f1_lex", "f2_xquad", "f3_docrrf", "f1f2")

    def per_query_data(query: str, files: list[str]) -> dict:
        rank_lists, kw_counts_p, _pl = _corpus_keyword_rankings(
            files, query, 10, 200, allow_or_fallback=False
        )
        terms = _corpus_query_terms(query)
        covered = {
            hits[0][0]: _doc_covered_terms(hits[0][0], [p for _d, p in hits], terms)
            for hits in rank_lists
        }
        cov_scores = _corpus_coverage_scores(covered)
        kw_scores = {
            item: cov_scores.get(hits[0][0], 0.0)
            for hits in rank_lists
            for item in hits
        }
        kw_fused = corpus.rrf_fuse_doc_rankings(
            rank_lists, top_k=DEEP_K, scores=kw_scores
        )
        qv = embedder.encode_query(query, model)
        scored, _un = _corpus_semantic_scores(files, model, qv)
        sem_sorted = sorted(scored, key=lambda t: (-t[2], t[0], t[1]))
        sem_ranking = [(p, pg) for p, pg, _s in sem_sorted[: DEEP_K * 3]]
        fused = corpus.rrf_fuse_two_rankings(kw_fused, sem_ranking, top_k=DEEP_K)
        return {
            "fused": [(id_by_path[d], p) for d, p in fused],
            "kw_counts": {id_by_path[d]: c for d, c in kw_counts_p.items()},
            "kw_cov": {id_by_path[d]: s for d, s in cov_scores.items()},
            "sem_pages": [id_by_path[p] for p, _pg in sem_ranking],
            "doc_terms": {id_by_path[d]: t for d, t in covered.items()},
            "all_terms": terms,
        }

    rows = []
    for q in queries:
        text = emitted[q["id"]]
        gold_pages: dict[str, set[int]] = {}
        for lab in q["labels"]:
            if lab.get("gain", 0) > 0:
                gold_pages.setdefault(lab["doc"], set()).add(lab["page"])

        d1 = per_query_data(text, paths)
        d2 = per_query_data(text, list(reversed(paths)))  # permutation probe

        row: dict = {"id": q["id"], "gold": sorted(gold_pages)}
        for v in variants:
            base_variant = "f1_vote" if v == "f1f2" else v
            use_xquad = v in ("f2_xquad", "f1f2")
            args1 = (
                d1["fused"],
                d1["kw_counts"],
                d1["kw_cov"],
                d1["sem_pages"],
                d1["doc_terms"],
                d1["all_terms"],
                corpus.CORPUS_RRF_K,
            )
            args2 = (
                d2["fused"],
                d2["kw_counts"],
                d2["kw_cov"],
                d2["sem_pages"],
                d2["doc_terms"],
                d2["all_terms"],
                corpus.CORPUS_RRF_K,
            )
            if use_xquad:
                key = "f2_base" if v == "f2_xquad" else "f2_vote"
            else:
                key = base_variant
            o1 = order_docs(key, *args1)
            o2 = order_docs(key, *args2)
            row[f"{v}_perm_ok"] = o1 == o2
            row[f"{v}_order"] = o1[:10]
        rows.append(row)

    # hop-2 outcomes per gold part (query-dependent, doc-fixed)
    hop2: dict[tuple[str, str], bool] = {}
    for q in queries:
        text = emitted[q["id"]]
        for lab in q["labels"]:
            if lab.get("gain", 0) > 0 and (q["id"], lab["doc"]) not in hop2:
                s = pdf_search(
                    path_by_id[lab["doc"]],
                    text,
                    mode="auto",
                    max_results=HOP2_RESULTS,
                )
                pages = {m["page"] for m in s.get("matches", [])}
                gold = {
                    x["page"]
                    for x in q["labels"]
                    if x["doc"] == lab["doc"] and x.get("gain", 0) > 0
                }
                hop2[(q["id"], lab["doc"])] = bool(pages & gold)

    gold_by_q = {r["id"]: r["gold"] for r in rows}
    total = sum(len(g) for g in gold_by_q.values())
    print(f"FAN-OUT ORDERING RACE (n=25 spread queries, {total} gold parts)")
    print(f"{'variant':<10}{'sel@3':>8}{'sel@5':>8}{'cov@5':>8}{'perm':>6}")
    results = {}
    for v in variants:
        s3 = s5 = cov5 = 0
        perm = all(r[f"{v}_perm_ok"] for r in rows)
        for r in rows:
            order = r[f"{v}_order"]
            for d in r["gold"]:
                if d in order[:3]:
                    s3 += 1
                if d in order[:5]:
                    s5 += 1
                    if hop2[(r["id"], d)]:
                        cov5 += 1
        results[v] = {
            "sel3": s3 / total,
            "sel5": s5 / total,
            "cov5": cov5 / total,
            "perm_ok": perm,
        }
        print(
            f"{v:<10}{s3 / total:>7.0%}{s5 / total:>8.0%}"
            f"{cov5 / total:>8.0%}{'ok' if perm else 'FAIL':>6}"
        )
    out = DATA / "fanout_ordering_race.json"
    out.write_text(
        json.dumps({"results": results, "rows": rows}, indent=1), encoding="utf-8"
    )
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
