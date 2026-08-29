#!/usr/bin/env python3
"""Generate fixtures for the demo page's JS corpus-fusion port.

Expected outputs come from the REAL Python implementations
(corpus.rrf_fuse_doc_rankings, server._corpus_query_terms,
server._corpus_coverage_scores). covered_terms replicates the set
logic of server._doc_covered_terms, which is cache-bound and cannot
be called directly on raw strings.

Run: uv run python scripts/gen_demo_fusion_fixtures.py
Writes: tests/data/demo_fusion_fixtures.json
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pdf_mcp import corpus  # noqa: E402
from pdf_mcp.server import _corpus_coverage_scores  # noqa: E402
from pdf_mcp.server import _corpus_query_terms  # noqa: E402

TERM_RE = re.compile(r"[a-z0-9]+")


class _TermsCache:
    """Minimal cache stand-in: about_terms only calls get_doc_terms."""

    def __init__(self, terms_by_path):
        self._t = terms_by_path

    def get_doc_terms(self, paths):
        return {p: self._t[p] for p in paths if p in self._t}


def covered_terms(texts: list[str], terms: set[str]) -> set[str]:
    found: set[str] = set()
    for text in texts:
        found |= terms & set(TERM_RE.findall(text.lower()))
        if len(found) == len(terms):
            break
    return found


def skey(doc: str, page: int) -> str:
    return f"{doc}\n{page}"


def e2e_expected(docs: dict[str, dict[int, str]], query: str, top_k: int):
    """Mirror server.py's keyword-arm wiring on raw page texts.

    NOTE: caps hits AFTER best-first sorting; the demo and the server's
    fallback cap in page order BEFORE sorting. Equivalent only while
    every fixture doc has <= top_k matching pages. Keep fixture docs
    small or replicate cap-before-sort here first.
    """
    terms = _corpus_query_terms(query)
    rank_lists = []
    for path in sorted(docs):
        pages = sorted(docs[path])
        hits = [
            p
            for p in pages
            if all(t in docs[path][p].lower() for t in query.lower().split())
        ]
        hits.sort(
            key=lambda p: (
                -sum(docs[path][p].lower().count(t) for t in query.lower().split()),
                p,
            )
        )
        if hits:
            rank_lists.append([(path, p) for p in hits[:top_k]])
    covered = {
        hits[0][0]: covered_terms([docs[hits[0][0]][p] for _d, p in hits], terms)
        for hits in rank_lists
    }
    doc_scores = _corpus_coverage_scores(covered)
    scores = {
        item: doc_scores.get(hits[0][0], 0.0) for hits in rank_lists for item in hits
    }
    fused = corpus.rrf_fuse_doc_rankings(rank_lists, top_k=top_k, scores=scores)
    return rank_lists, scores, fused


def main() -> None:
    fixtures = {}

    fixtures["query_terms"] = [
        {
            "query": "cloud security requirements",
            "expected": sorted(_corpus_query_terms("cloud security requirements")),
        },
        {
            "query": "AI and the fog",
            "expected": sorted(_corpus_query_terms("AI and the fog")),
        },
        {
            "query": "厚木基地 report",
            "expected": sorted(_corpus_query_terms("厚木基地 report")),
        },
    ]

    rl_basic = [
        [("b.pdf", 3), ("b.pdf", 1)],
        [("a.pdf", 2)],
        [("c.pdf", 7), ("c.pdf", 4), ("c.pdf", 9)],
    ]
    fixtures["rrf_no_scores"] = {
        "rank_lists": rl_basic,
        "expected": corpus.rrf_fuse_doc_rankings(rl_basic),
    }
    sc = {
        ("a.pdf", 2): 2.5,
        ("b.pdf", 3): 1.1,
        ("b.pdf", 1): 1.1,
        ("c.pdf", 7): 3.0,
        ("c.pdf", 4): 3.0,
        ("c.pdf", 9): 3.0,
    }
    fixtures["rrf_scores_tiebreak"] = {
        "rank_lists": rl_basic,
        "scores": {skey(d, p): v for (d, p), v in sc.items()},
        "expected": corpus.rrf_fuse_doc_rankings(rl_basic, scores=sc),
    }
    fixtures["rrf_topk"] = {
        "rank_lists": rl_basic,
        "top_k": 3,
        "scores": {skey(d, p): v for (d, p), v in sc.items()},
        "expected": corpus.rrf_fuse_doc_rankings(rl_basic, top_k=3, scores=sc),
    }

    cov = {
        "x.pdf": {"cloud", "security"},
        "y.pdf": {"cloud"},
        "z.pdf": {"cloud", "security", "requirements"},
    }
    fixtures["coverage_scores"] = {
        "covered": {p: sorted(t) for p, t in cov.items()},
        "expected": _corpus_coverage_scores(cov),
    }

    docs_terms = {
        "/a.pdf": {0: "the cat budget budget alpha", 1: "Alpha REPORT"},
        "/b.pdf": {0: "bravo budget report report"},
        "/c.pdf": {0: "charlie budget"},
    }
    fixtures["about_terms"] = {
        "docs": {p: {str(k): v for k, v in t.items()} for p, t in docs_terms.items()},
        "expected_terms": {p: corpus.profile_terms(t) for p, t in docs_terms.items()},
        "expected_about": corpus.about_terms(
            _TermsCache({p: corpus.profile_terms(t) for p, t in docs_terms.items()}),
            list(docs_terms),
        ),
    }

    docs = {
        "alpha.pdf": {
            1: "Annual budget figures and travel notes.",
            2: (
                "Cloud migration security requirements for federal agencies. "
                "Security controls."
            ),
            3: "Cloud cloud cloud. Appendix on security requirements and cloud audits.",
        },
        "beta.pdf": {
            1: "Consumer credit trends. Nothing about infrastructure.",
            2: "A single mention of cloud security requirements in passing.",
        },
        "gamma.pdf": {
            1: "Organic labeling rules for producers.",
        },
    }
    query = "cloud security requirements"
    rank_lists, scores, fused = e2e_expected(docs, query, top_k=10)
    fixtures["end_to_end"] = {
        "docs": {p: {str(k): v for k, v in pages.items()} for p, pages in docs.items()},
        "query": query,
        "rank_lists": rank_lists,
        "scores": {skey(d, p): v for (d, p), v in scores.items()},
        "expected_fused": fused,
    }
    # Permutation invariance at the Python reference level too.
    for perm in ([2, 1, 0], [1, 2, 0]):
        shuffled = [rank_lists[i] for i in perm if i < len(rank_lists)]
        assert (
            corpus.rrf_fuse_doc_rankings(shuffled, top_k=10, scores=scores) == fused
        ), perm

    out = (
        Path(__file__).resolve().parents[1]
        / "tests"
        / "data"
        / "demo_fusion_fixtures.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(fixtures, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
