#!/usr/bin/env python
"""
scripts/recheck_production_tiebreak.py

Does the SHIPPED corpus search depend on what the files are called?

Companion to recheck_tiebreak_permutation.py, which tests the stage-2
spike harness. This one drives the production path in server.py -- the
keyword arm, the coverage/IDF tie-break, and the hybrid fusion with the
semantic arm -- then re-fuses everything under stable renamings of every
document. A ranking that carries real relevance is invariant; one that
moves is reporting filename order.

Both bugs found on 2026-07-27 were exactly this failure, so the drift
numbers here are the acceptance gate for any change to cross-document
ranking. Needle is a built-in control: a needle query matches one
document, nothing ties, and its drift must stay 0.000.

REQUIRES the cache it points at to hold text AND embeddings for the whole
corpus, or the semantic arm silently contributes nothing and the "hybrid"
columns are really keyword-only. The needle/described columns will read
0.000 if that happens -- treat that as a broken run, not a result.

Free and deterministic. Writes nothing.

Run:  uv run python scripts/recheck_production_tiebreak.py
"""

import hashlib
import json
import sys

from pathlib import Path
from statistics import mean

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))
import pdf_mcp.server as sm  # noqa: E402
from pdf_mcp import corpus, embedder as E  # noqa: E402
from pdf_mcp.cache import PDFCache  # noqa: E402
from pdf_mcp.config import PDFConfig  # noqa: E402
import _retrieval_metrics as rm  # noqa: E402

CACHE_DIR = REPO / "benchmark_data" / ".tiebreak_probe_cache"
sm.cache = PDFCache(cache_dir=CACHE_DIR, ttl_hours=24 * 30)
model = PDFConfig().embedding_model
E.check_available(model)
man = json.load(open(REPO / "benchmark_data/corpus_search/manifest.json"))
paths = [str((REPO / d["path"]).resolve()) for d in man["docs"]]
pbid = {d["id"]: str((REPO / d["path"]).resolve()) for d in man["docs"]}
Q = json.load(open(REPO / "benchmark_data/corpus_search/queries.json"))["queries"]
TOP = 10


def docndcg(f, g):
    seen = []
    for d, _p in f:
        if d not in seen:
            seen.append(d)
    return rm.ndcg_at_k([g.get(d, 0.0) for d in seen], list(g.values()), TOP)


cap = {}
for q in Q:
    rl, _d, _p = sm._corpus_keyword_rankings(
        paths, q["query"], TOP, 300, allow_or_fallback=False
    )
    terms = sm._corpus_query_terms(q["query"])
    cov = {
        h[0][0]: sm._doc_covered_terms(h[0][0], [p for _x, p in h], terms) for h in rl
    }
    qv = E.encode_query(q["query"], model)
    scored, _u = sm._corpus_semantic_scores(paths, model, qv)
    cap[q["id"]] = (q["class"], rl, cov, scored)
print("captured", len(cap), flush=True)


def ev(seed, use_cov):
    out = {}
    for q in Q:
        cls, rl, cov, scored = cap[q["id"]]
        ren = (
            (lambda d: d)
            if seed is None
            else (lambda d: hashlib.sha1(f"{seed}:{d}".encode()).hexdigest()[:12])
        )
        rl2 = [[(ren(d), p) for d, p in h] for h in rl]
        sc = None
        if use_cov:
            ds = sm._corpus_coverage_scores(cov)
            sc = {(ren(d), p): ds.get(d, 0.0) for h in rl for d, p in h}
        kw = corpus.rrf_fuse_doc_rankings(rl2, top_k=TOP, scores=sc)
        s2 = [(ren(p), pg, v) for p, pg, v in scored]
        s2.sort(key=lambda t: (-t[2], t[0], t[1]))
        sem = [(p, pg) for p, pg, _v in s2[: min(TOP * 3, len(s2))]]
        fused = [i for i, _s in corpus.rrf_fuse_two_rankings_scored(kw, sem, top_k=TOP)]
        g = {}
        for lb in q["labels"]:
            k = ren(pbid[lb["doc"]])
            g[k] = max(g.get(k, 0.0), float(lb["gain"]))
        out.setdefault(cls, []).append(docndcg(fused, g))
    o = {c: mean(v) for c, v in out.items()}
    o["OVERALL"] = mean(v for vs in out.values() for v in vs)
    return o


for use, label in ((False, "OLD alphabetical"), (True, "NEW coverage x IDF")):
    real = ev(None, use)
    perms = [ev(s, use) for s in range(1, 5)]
    print(f"\n{label}  (mode=auto, end-to-end)")
    for cls in ("described", "needle", "spread", "trap", "OVERALL"):
        p = [x[cls] for x in perms]
        print(
            f"  {cls:10s} real={real[cls]:.3f}"
            f"  perm mean={mean(p):.3f}  drift={mean(p) - real[cls]:+.3f}"
        )
