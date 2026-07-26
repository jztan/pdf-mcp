#!/usr/bin/env python
"""
scripts/diagnose_excerpt_fidelity.py

Does the excerpt actually carry the answer that is on the page we found?

The answerability eval grades the whole payload with an LLM, which costs
money and carries a 13% verdict noise floor (see
scripts/measure_judge_noise_floor.py). This asks a narrower question that
needs no judge at all and is fully deterministic:

  1. locate the page that verifiably contains the reference fact
  2. ask pdf_search the question
  3. if that page came back, does its excerpt quote the fact -- and in
     particular the FIGURE the fact turns on?

Every question therefore lands in exactly one bucket:

  ok            excerpt carries the fact          -- nothing to fix
  EXCERPT MISS  right page retrieved, wrong block -- excerpt selection
  RECALL MISS   page never retrieved              -- ranking / scoring
  unlocatable   reference fact not found in text  -- ground-truth issue

The split matters because the three have different fixes, and the
wrong-attribution failures found by the judge turned out to be almost
entirely the second kind: the page ranked first and the excerpt quoted a
neighbouring paragraph. On a segment-results page the consolidated and
per-segment paragraphs share nearly every query token, so the block picker
has no signal separating them.

Free: no judge calls. Retrieval is deterministic, so this is repeatable.

Run:  uv run python scripts/diagnose_excerpt_fidelity.py
      uv run python scripts/diagnose_excerpt_fidelity.py --ids a,b,c
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

DATA = REPO / "benchmark_data" / "financial_reports"
CACHE_DIR = REPO / "benchmark_data" / ".answerability_cache"
TOP_K = 10
ANCHOR_WORDS = 8
MAX_PAGES = 400

# "$12.9 billion", "17%", "224.2" -- the tokens an answer actually turns on.
FIGURE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?")


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def figures(text: str) -> set[str]:
    """Numeric tokens, ignoring bare small integers that match by chance."""
    out = set()
    for m in FIGURE.findall(text):
        t = m.strip().rstrip(".")
        if t.startswith("$") or t.endswith("%") or "." in t or "," in t:
            out.add(t.lstrip("$").rstrip("%"))
    return out


def locate(fact: str, pages: dict[int, str]) -> list[int]:
    """EVERY page that states the fact, not just the first one found.

    A 10-K repeats the same figure in the MD&A, the segment note, and the
    financial statements. Stopping at the first matching window pins one
    location and reports the others as recall misses -- which is how an
    earlier version of this script produced 28% "RECALL MISS" on questions
    the judge had already called fully answerable.

    So: union across all anchor windows, plus any page carrying the exact
    figures the fact turns on together with a content word from it.
    """
    toks = norm(fact).split()
    normed = {n: norm(t) for n, t in pages.items()}
    hits: set[int] = set()
    for i in range(max(1, len(toks) - ANCHOR_WORDS + 1)):
        probe = " ".join(toks[i : i + ANCHOR_WORDS])
        hits |= {n for n, t in normed.items() if probe in t}

    want = figures(fact)
    if want:
        content = [w for w in toks if len(w) > 5 and not FIGURE.fullmatch(w)]
        for n, t in normed.items():
            if want & figures(t) and any(w in t for w in content[:6]):
                hits.add(n)
    return sorted(hits)


def classify(fact: str, excerpts: list[str]) -> tuple[bool, bool]:
    """(quotes_fact, carries_figure) for the excerpts of the gold page."""
    joined = norm(" ".join(excerpts))
    toks = norm(fact).split()
    quotes = any(
        " ".join(toks[i : i + ANCHOR_WORDS]) in joined
        for i in range(max(1, len(toks) - ANCHOR_WORDS + 1))
    )
    want = figures(fact)
    carries = bool(want & figures(joined)) if want else quotes
    return quotes, carries


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", default="auto", choices=["auto", "keyword", "semantic"])
    ap.add_argument("--ids", help="comma-separated question ids (default: all)")
    args = ap.parse_args(argv)

    import pdf_mcp.server as server_module

    from pdf_mcp.cache import PDFCache
    from pdf_mcp.server import pdf_search

    cache = PDFCache(cache_dir=CACHE_DIR, ttl_hours=24 * 30)
    server_module.cache = cache

    manifest = json.loads((DATA / "manifest.json").read_text())
    path_by_id = {d["id"]: str(REPO / d["path"]) for d in manifest["docs"]}
    questions = [
        q
        for q in json.loads((DATA / "answerability_questions.json").read_text())[
            "questions"
        ]
        if q["scope"] == "single-doc"
    ]
    if args.ids:
        keep = set(args.ids.split(","))
        questions = [q for q in questions if q["id"] in keep]

    rows: list[dict[str, Any]] = []
    for q in questions:
        doc = q["expect_docs"][0]
        path = path_by_id[doc]
        info = pdf_search(path, q["question"], mode=args.mode, max_results=TOP_K)
        matches = info.get("matches", [])

        # get_pages_text is an INTERNAL API: 0-indexed. pdf_search returns
        # 1-indexed pages. Convert here, or every comparison below is off by
        # one -- which silently reports a located page as a recall miss.
        raw = cache.get_pages_text(path, list(range(0, MAX_PAGES)))
        pages = {n + 1: t for n, t in raw.items()}
        gold = locate(q["reference_facts"][0], pages)

        got = [m["page"] for m in matches]
        hit = [p for p in gold if p in got]
        if not gold:
            bucket = "unlocatable"
            quotes = carries = False
        elif not hit:
            bucket = "RECALL MISS"
            quotes = carries = False
        else:
            ex = [m.get("excerpt") or "" for m in matches if m["page"] in hit]
            quotes, carries = classify(q["reference_facts"][0], ex)
            bucket = "ok" if carries else "EXCERPT MISS"
        rows.append(
            {
                "id": q["id"],
                "type": q["type"],
                "doc": doc,
                "gold_pages": gold,
                "gold_rank": next(
                    (i + 1 for i, m in enumerate(matches) if m["page"] in gold), None
                ),
                "bucket": bucket,
                "quotes_fact": quotes,
                "carries_figure": carries,
            }
        )

    order = {"EXCERPT MISS": 0, "RECALL MISS": 1, "unlocatable": 2, "ok": 3}
    rows.sort(key=lambda r: (order[r["bucket"]], r["id"]))
    print(f"{'question':32s} {'type':15s} {'gold':>10s} {'rank':>5s}  bucket")
    print("-" * 92)
    for r in rows:
        print(
            f"{r['id']:32s} {r['type']:15s} {str(r['gold_pages'][:2]):>10s}"
            f" {str(r['gold_rank'] or '-'):>5s}  {r['bucket']}"
        )

    n = len(rows)
    counts = {b: sum(1 for r in rows if r["bucket"] == b) for b in order}
    print(f"\n{n} single-document questions, mode={args.mode}")
    for b in ("ok", "EXCERPT MISS", "RECALL MISS", "unlocatable"):
        print(f"  {b:14s}: {counts[b]:3d}  ({counts[b]/n:.0%})")
    fixable = counts["EXCERPT MISS"]
    print(
        f"\n{fixable} questions have the answer on a retrieved page but not in"
        " the excerpt.\nThat is excerpt selection, not retrieval -- and it is"
        " testable without a judge."
    )
    out = DATA / f"excerpt_fidelity_{args.mode}.json"
    out.write_text(json.dumps({"mode": args.mode, "rows": rows}, indent=2) + "\n")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
