#!/usr/bin/env python
"""
scripts/recheck_tiebreak_permutation.py

Is a cross-document ranking result real, or an artifact of file naming?

`rrf_fuse_doc_rankings` gives every matching document its own rank-1 page,
so all matching documents tie at exactly 1/(k+0) and the documented
`(doc_path, page)` tie-break decides their order. Alphabetical order then
carries the ranking. That is fine only if document names are uncorrelated
with relevance -- and in `benchmark_data/corpus_search` they are not: the
labelled documents are the original old-arXiv corpus (24 of the
alphabetically-first 30 are labelled, against 16 of the last 30) while the
79 later-added distractors are newer, higher-numbered IDs.

This script measures how much of a result rests on that correlation. It
renames every document with a stable hash, which destroys the alphabetical
skew and changes nothing else, and re-scores. A result that is a property
of the ranking is invariant under renaming. A result that moves is telling
you about the corpus's filenames.

It reproduces the stage-2 spike (arm A = one corpus-wide temp FTS table
with global IDF; arm B = RRF fusion of per-document rank lists) so the
published decision can be re-checked directly. Arm A never uses a
document tie-break and is therefore invariant by construction, which makes
it a built-in control: if arm A ever moves under permutation, this script
is broken, not the benchmark.

Free and deterministic. Writes nothing -- in particular it does NOT touch
RESULTS.md, unlike `benchmark_corpus_search.py --run`.

Run:  uv run python scripts/recheck_tiebreak_permutation.py
      uv run python scripts/recheck_tiebreak_permutation.py --seeds 12
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile

from pathlib import Path
from statistics import mean

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

DATA = REPO / "benchmark_data" / "corpus_search"

# The stage-2 spike predates the `described` class, so grading it here
# would compare against a published table that never included it.
SPIKE_CLASSES = ("needle", "spread", "trap")


def alias(seed: int, doc_id: str) -> str:
    """Stable pseudo-random rename. Same seed always yields the same map,
    so a reported number can be reproduced exactly."""
    return hashlib.sha1(f"{seed}:{doc_id}".encode()).hexdigest()[:12]


def labelled_position_skew(doc_ids: list[str], labelled: set[str]) -> str:
    """One-line description of how far the labelled docs sit from uniform
    in alphabetical order. This is the bias the permutation removes."""
    ids = sorted(doc_ids)
    pos = [i for i, d in enumerate(ids) if d in labelled]
    window = max(1, len(ids) // 10 * 3)
    first = sum(1 for p in pos if p < window)
    last = sum(1 for p in pos if p >= len(ids) - window)
    return (
        f"{len(labelled)}/{len(ids)} labelled; mean alphabetical position"
        f" {mean(pos):.1f} against {(len(ids) - 1) / 2:.1f} for uniform;"
        f" {first}/{window} in the first {window}, {last}/{window} in the last"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--seeds",
        type=int,
        default=6,
        help="number of independent renamings (default 6)",
    )
    args = ap.parse_args(argv)

    import _corpus_ranking as cr
    import _retrieval_metrics as rm

    from benchmark_corpus_search import (
        TOP_K,
        build_corpus_index,
        build_per_doc_indexes,
        search_corpus,
        search_per_doc_rrf,
    )
    from pdf_mcp.cache import PDFCache
    from pdf_mcp.corpus import warm_docs

    manifest = json.loads((DATA / "manifest.json").read_text())
    queries = json.loads((DATA / "queries.json").read_text())
    qs = [q for q in queries["queries"] if q["class"] in SPIKE_CLASSES]

    paths = [
        str(REPO / d["path"]) for d in manifest["docs"] if (REPO / d["path"]).exists()
    ]
    if not paths:
        print("ERROR: no corpus PDFs available locally")
        return 2
    id_by_path = {str(REPO / d["path"]): d["id"] for d in manifest["docs"]}

    labelled = {lb["doc"] for q in qs for lb in q["labels"]}
    print(labelled_position_skew([d["id"] for d in manifest["docs"]], labelled))

    with tempfile.TemporaryDirectory() as tmp:
        cache = PDFCache(cache_dir=Path(tmp), ttl_hours=1)
        warm = warm_docs(paths, budget_seconds=3600, cache=cache)
        pages: list[tuple[str, int, str]] = []
        for row in warm["docs"]:
            doc_id = id_by_path[row["path"]]
            texts = cache.get_pages_text(row["path"], list(range(row["pages"])))
            for pn, text in sorted(texts.items()):
                pages.append((doc_id, pn + 1, text))
    print(f"warmed {len(warm['docs'])} docs, {len(pages)} pages\n")

    def score_arm_b(seed: int | None) -> dict[str, tuple[str, float]]:
        """Arm B under one renaming; seed None keeps the real names."""
        if seed is None:
            renamed = pages
            name = dict.fromkeys((d for d, _p, _t in pages))
            name = {d: d for d in name}
        else:
            name = {d: alias(seed, d) for d, _p, _t in pages}
            renamed = [(name[d], p, t) for d, p, t in pages]
        conn = sqlite3.connect(":memory:")
        doc_ids = build_per_doc_indexes(conn, renamed)
        out: dict[str, tuple[str, float]] = {}
        for q in qs:
            labels = {
                (name[lb["doc"]], lb["page"]): float(lb["gain"]) for lb in q["labels"]
            }
            ranked = search_per_doc_rrf(conn, doc_ids, q["query"], TOP_K, TOP_K)
            out[q["id"]] = (
                q["class"],
                rm.ndcg_at_k(
                    cr.grade_ranking(ranked, labels), list(labels.values()), TOP_K
                ),
            )
        conn.close()
        return out

    # Arm A: one corpus-wide table. No document tie-break, so it is the
    # control -- it must not move under renaming.
    conn_a = sqlite3.connect(":memory:")
    build_corpus_index(conn_a, pages)
    arm_a: dict[str, tuple[str, float]] = {}
    for q in qs:
        labels = {(lb["doc"], lb["page"]): float(lb["gain"]) for lb in q["labels"]}
        ranked = search_corpus(conn_a, q["query"], TOP_K)
        arm_a[q["id"]] = (
            q["class"],
            rm.ndcg_at_k(
                cr.grade_ranking(ranked, labels), list(labels.values()), TOP_K
            ),
        )
    conn_a.close()

    def cmean(rows: dict[str, tuple[str, float]], cls: str | None) -> float:
        vals = [s for _c, s in rows.values() if cls is None or _c == cls]
        return sum(vals) / len(vals)

    classes = (*SPIKE_CLASSES, None)
    published = score_arm_b(None)
    runs = [score_arm_b(s) for s in range(1, args.seeds + 1)]

    head = f"{'run':>8s}" + "".join(f"{(c or 'OVERALL'):>9s}" for c in classes)
    print(head)
    print("-" * len(head))
    print(f"{'arm A':>8s}" + "".join(f"{cmean(arm_a, c):9.3f}" for c in classes))
    print(
        f"{'arm B':>8s}"
        + "".join(f"{cmean(published, c):9.3f}" for c in classes)
        + "   <- real filenames (published)"
    )
    for i, r in enumerate(runs, 1):
        print(
            f"{'perm ' + str(i):>8s}" + "".join(f"{cmean(r, c):9.3f}" for c in classes)
        )

    print()
    for cls in classes:
        name = cls or "OVERALL"
        real = cmean(published, cls)
        perm = [cmean(r, cls) for r in runs]
        a = cmean(arm_a, cls)
        outside = "OUTSIDE" if real > max(perm) or real < min(perm) else "inside"
        flip = ""
        if (real > a) != (mean(perm) > a):
            flip = "  ** ARM ORDER FLIPS **"
        print(
            f"{name:8s} armA={a:.3f}  armB real={real:.3f}"
            f"  permuted mean={mean(perm):.3f}"
            f" [{min(perm):.3f}, {max(perm):.3f}]  real is {outside}"
            f" the permuted range{flip}"
        )

    print(
        "\nA result that moves under renaming is a property of the corpus's"
        "\nfilenames, not of the ranking. Arm A is the control and must not move."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
