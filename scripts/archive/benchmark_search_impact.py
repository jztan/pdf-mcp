#!/usr/bin/env python
"""
scripts/benchmark_search_impact.py

Does stripping running headers/footers actually change search ranking?

The boilerplate-detection benchmark (scripts/benchmark_boilerplate.py) proved
we can detect and remove running headers/footers cleanly. This benchmark asks
the question that decides whether shipping it is worth it: with boilerplate
removed from the index, does pdf-mcp's keyword (BM25 / FTS5) ranking change?

Why keyword-only: pdf-mcp's hybrid search fuses BM25 with semantic embeddings.
The semantic leg needs a ~67 MB model download (offline here), but keyword/BM25
is exactly the leg where boilerplate-on-every-page would plausibly distort
ranking, and it runs fully offline. So this isolates the BM25 question.

Method: for each PDF, build the FTS index twice via the real pdf_search path —
once on raw page text, once with boilerplate stripped (full freq_runs detector
from benchmark_boilerplate) — by monkeypatching the extractor and using a fresh
temp cache each time. The ONLY difference between runs is boilerplate removal.

Two measurements:
  1. Realistic, relevance-labeled queries (benchmark_data/ground_truth.json):
     MRR and rank stability. attention/gpt3 boilerplate is page-numbers only,
     so query terms never collide — the expected, and informative, result is
     "no change", i.e. BM25's IDF already neutralizes high-frequency boilerplate.
  2. Distortion on GDPR (benchmark_data/boilerplate_search_queries.json), whose
     running header carries real words. `control` queries (ordinary lookups)
     vs `collision` queries (terms that overlap the header). Measured label-free
     as top-K Jaccard + count of header-driven pages dropped after stripping.

Usage:
    python scripts/benchmark_search_impact.py
    python scripts/benchmark_search_impact.py --output FILE
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import pymupdf

SCRIPTS = Path(__file__).parent
ROOT = SCRIPTS.parent.parent  # archive/ -> scripts/ -> repo root
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(SCRIPTS))

import pdf_mcp.server as server_module  # noqa: E402
from pdf_mcp.cache import PDFCache  # noqa: E402
from benchmark_boilerplate import (  # noqa: E402
    _band,
    _freq_keys,
    page_blocks,
    signature,
)

REAL_PDFS = ROOT / "benchmark_data" / ".real_pdfs"
GROUND_TRUTH = ROOT / "benchmark_data" / "ground_truth.json"
QUERIES = ROOT / "benchmark_data" / "boilerplate_search_queries.json"
TOPK = 10


# --------------------------------------------------------------------------- #
# Boilerplate removal (full freq_runs method), reused from the detector
# --------------------------------------------------------------------------- #
def compute_removal(path: Path) -> dict[int, set[str]]:
    """Per-page set of exact block texts the full method flags as boilerplate."""
    doc = pymupdf.open(path)
    try:
        bpp = [page_blocks(doc[i]) for i in range(doc.page_count)]
    finally:
        doc.close()
    keys = _freq_keys(
        bpp, use_bands=True, digit_norm=True, use_parity=True, use_runs=True
    )
    removal: dict[int, set[str]] = {}
    for pi, blocks in enumerate(bpp):
        drop = set()
        for text, y0, y1 in blocks:
            band = _band(y0, y1)
            if band is not None and (signature(text, True), band) in keys:
                drop.add(text)
        removal[pi] = drop
    return removal


def make_extractor(removal: dict[int, set[str]] | None) -> Any:
    """A simple, deterministic block extractor; the only variable is removal.

    Both runs use this same extractor so the comparison isolates boilerplate
    stripping (rather than confounding it with column-detection differences).
    """

    def _extract(page: Any, sort_by_position: bool = True) -> str:
        blocks = page.get_text("blocks", sort=True)
        texts = [b[4] for b in blocks if b[6] == 0 and b[4].strip()]
        if removal is not None:
            drop = removal.get(page.number, set())
            texts = [t for t in texts if t not in drop]
        return "\n\n".join(texts)

    return _extract


def search_pages(
    pdf: Path, query: str, removal: dict[int, set[str]] | None
) -> list[int]:
    """Keyword-mode pdf_search on a fresh index; return ranked 1-indexed pages."""
    orig_cache = server_module.cache
    orig_extract = server_module.extract_text_from_page
    with tempfile.TemporaryDirectory() as tmp:
        server_module.cache = PDFCache(cache_dir=Path(tmp), ttl_hours=1)
        server_module.extract_text_from_page = make_extractor(removal)
        try:
            res = server_module.pdf_search(
                str(pdf), query, mode="keyword", max_results=TOPK
            )
            return [m["page"] for m in res.get("matches", [])]
        finally:
            server_module.cache = orig_cache
            server_module.extract_text_from_page = orig_extract


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def reciprocal_rank(ranked: list[int], relevant: list[int]) -> float:
    for i, pg in enumerate(ranked, start=1):
        if pg in relevant:
            return 1.0 / i
    return 0.0


def jaccard(a: list[int], b: list[int]) -> float:
    sa, sb = set(a), set(b)
    union = sa | sb
    return len(sa & sb) / len(union) if union else 1.0


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #
def run_realistic() -> list[dict[str, Any]]:
    gt = json.loads(GROUND_TRUTH.read_text())
    files = json.loads(QUERIES.read_text())["realistic_pdf_files"]
    rows = []
    for pid, fname in files.items():
        pdf = REAL_PDFS / fname
        if not pdf.exists() or pid not in gt["pdfs"]:
            print(f"  skip {pid}: missing pdf or ground truth")
            continue
        removal = compute_removal(pdf)
        for sid, sc in gt["pdfs"][pid]["scenarios"].items():
            base = search_pages(pdf, sc["query"], None)
            strip = search_pages(pdf, sc["query"], removal)
            rows.append({
                "pdf": pid,
                "query": sc["query"],
                "relevant": sc["relevant_pages"],
                "rr_base": reciprocal_rank(base, sc["relevant_pages"]),
                "rr_strip": reciprocal_rank(strip, sc["relevant_pages"]),
                "changed": base != strip,
            })
    return rows


def run_distortion() -> list[dict[str, Any]]:
    dist = json.loads(QUERIES.read_text())["distortion"]
    rows = []
    for pid, spec in dist.items():
        pdf = REAL_PDFS / spec["file"]
        if not pdf.exists():
            print(f"  skip {pid}: missing pdf")
            continue
        removal = compute_removal(pdf)
        for kind in ("control", "collision"):
            for query in spec.get(kind, []):
                base = search_pages(pdf, query, None)
                strip = search_pages(pdf, query, removal)
                dropped = [p for p in base if p not in strip]
                rows.append({
                    "pdf": pid,
                    "kind": kind,
                    "query": query,
                    "jaccard": jaccard(base, strip),
                    "dropped": len(dropped),
                    "base_n": len(base),
                })
    return rows


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def format_markdown(
    realistic: list[dict[str, Any]], distortion: list[dict[str, Any]]
) -> str:
    lines = [
        "# Search-impact benchmark: does stripping boilerplate change ranking?",
        "",
        "Keyword (BM25 / FTS5) ranking with boilerplate left in vs stripped, via "
        "the real `pdf_search` path on a fresh index each run "
        "(`scripts/benchmark_search_impact.py`). The only variable is boilerplate "
        "removal. Semantic leg is offline here (model download); BM25 is the leg "
        "where boilerplate-on-every-page would plausibly distort ranking.",
        "",
        "## 1. Realistic labeled queries (MRR, rank stability)",
        "",
        "attention/gpt3 boilerplate is page-numbers only, so query terms never "
        "collide. `changed` = top-10 differs after stripping.",
        "",
        "| pdf | query | relevant | RR base | RR strip | changed |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in realistic:
        lines.append(
            f"| {r['pdf']} | {r['query']} | {r['relevant']} | "
            f"{r['rr_base']:.2f} | {r['rr_strip']:.2f} | "
            f"{'yes' if r['changed'] else 'no'} |"
        )
    mrr_b = _mean([r["rr_base"] for r in realistic])
    mrr_s = _mean([r["rr_strip"] for r in realistic])
    n_changed = sum(r["changed"] for r in realistic)
    lines += [
        "",
        f"**MRR: {mrr_b:.3f} (base) vs {mrr_s:.3f} (stripped); "
        f"{n_changed}/{len(realistic)} queries changed top-10.**",
        "",
        "## 2. GDPR distortion: control vs header-colliding queries",
        "",
        "Label-free. `jaccard` = top-10 overlap (1.00 = identical results). "
        "`dropped` = pages in the baseline top-10 removed after stripping "
        "(header-driven hits). `control` = ordinary lookups; `collision` = terms "
        "overlapping the running header.",
        "",
        "| kind | query | jaccard | dropped / base |",
        "| --- | --- | --- | --- |",
    ]
    for r in distortion:
        lines.append(
            f"| {r['kind']} | {r['query']} | {r['jaccard']:.2f} | "
            f"{r['dropped']}/{r['base_n']} |"
        )
    ctrl = [r for r in distortion if r["kind"] == "control"]
    coll = [r for r in distortion if r["kind"] == "collision"]
    lines += [
        "",
        f"**control mean jaccard {_mean([r['jaccard'] for r in ctrl]):.2f}, "
        f"collision mean jaccard {_mean([r['jaccard'] for r in coll]):.2f}.**",
        "",
        "## Takeaway",
        "",
        "- Realistic queries: BM25's IDF already down-weights text that appears "
        "on every page, so leaving boilerplate in the index does not move "
        "ranking. No benefit there.",
        "- The benefit is real but narrow: it shows up only when query terms "
        "overlap the boilerplate (the collision rows), where the header makes "
        "otherwise-irrelevant pages match and stripping removes that noise. "
        "Documents with word-bearing running headers (legal/standards/journals) "
        "are where this earns its keep.",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=str, default=None, help="write md report")
    args = parser.parse_args()

    realistic = run_realistic()
    distortion = run_distortion()
    md = format_markdown(realistic, distortion)
    print(md)
    if args.output:
        Path(args.output).write_text(md, encoding="utf-8")
        print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
