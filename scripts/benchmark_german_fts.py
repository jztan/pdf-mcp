#!/usr/bin/env python
"""
scripts/benchmark_german_fts.py

Before/after recall and MRR for `[fts] language = "de"` (the German-stemmed
FTS mirror index, `pdf_search_fts_de` in cache.py) versus the default
`porter unicode61` keyword index, over a small synthetic German corpus.

No external corpus is required: the "documents" are short German paragraphs
written directly into this script, each on its own synthetic page, covering
the three gaps the option addresses:

- inflection: a query word and the relevant page use different forms of the
  same word (`kündigen` / `Kündigung` / `gekündigt`)
- ASCII-transliteration spelling: `ue`/`ss` for `ü`/`ß` on either side
  (`Kuendigung` / `Kündigung`, `Strasse` / `Straße`)
- distractor pages sharing surface vocabulary but not the query's topic, to
  keep recall/MRR honest rather than trivially 1.0 on a single-page corpus

Ground truth (`page` per query) is hand-authored here since the corpus is
hand-authored too — small enough to eyeball, unlike the CJK benchmark's
literal-substring auto-derivation over a real corpus.

Usage:
    python scripts/benchmark_german_fts.py
    python scripts/benchmark_german_fts.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "src"))

from pdf_mcp.cache import PDFCache  # noqa: E402

PAGES: dict[int, str] = {
    0: "Die Kündigung des Arbeitsvertrags war form- und fristgerecht.",
    1: "Der Arbeitgeber kündigte dem Mitarbeiter zum Monatsende.",
    2: "Nach zwei Kündigungen in Folge suchte er eine neue Anstellung.",
    3: "Die Straße vor dem Rathaus wurde neu asphaltiert.",
    4: "Der Fußball rollte über den Platz vor dem Stadion.",
    5: "Der Urlaubsanspruch entsteht anteilig im ersten Beschäftigungsjahr.",
    6: "Das Arbeitsgericht wies die Klage wegen Fristversäumnis ab.",
    7: "Am Wochenende fand ein Konzert in der Stadthalle statt.",
}

QUERIES: list[dict] = [
    # inflection: query word differs from every relevant page's exact form
    {"query": "kündigen", "relevant": [0, 1, 2]},
    {"query": "Kündigung", "relevant": [0, 1, 2]},
    {"query": "gekündigt", "relevant": [0, 1, 2]},
    # ASCII-transliteration spelling, on both sides
    {"query": "Kuendigung", "relevant": [0, 1, 2]},
    {"query": "Straße", "relevant": [3]},
    {"query": "Strasse", "relevant": [3]},
    {"query": "Fussball", "relevant": [4]},
    # control: an exact-form query that matches under EITHER mode, to make
    # sure "de" mode is not just "return everything"
    {"query": "Urlaubsanspruch", "relevant": [5]},
    {"query": "Arbeitsgericht", "relevant": [6]},
]


def _run(fts_language: str | None) -> dict:
    tmp = tempfile.mkdtemp(prefix="german_fts_bench_")
    cache = PDFCache(cache_dir=Path(tmp), fts_language=fts_language)
    pdf_path = Path(tmp) / "synthetic.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    path = str(pdf_path)
    cache.save_pages_text(path, PAGES)

    per_query: dict[str, dict] = {}
    recalls: list[float] = []
    rrs: list[float] = []
    for q in QUERIES:
        matches = cache.search_fts(path, q["query"], 10, 200)
        ret_pages = [m["page"] - 1 for m in matches]  # back to 0-indexed
        relevant = set(q["relevant"])
        hit = ret_pages and set(ret_pages) & relevant
        recall = 1.0 if hit else 0.0
        rr = 0.0
        for i, p in enumerate(ret_pages, 1):
            if p in relevant:
                rr = 1.0 / i
                break
        per_query[q["query"]] = {
            "relevant": sorted(relevant),
            "returned": ret_pages,
            "recall": recall,
            "rr": rr,
        }
        recalls.append(recall)
        rrs.append(rr)

    return {
        "per_query": per_query,
        "mean_recall": sum(recalls) / len(recalls),
        "mrr": sum(rrs) / len(rrs),
    }


def run_benchmark() -> dict:
    """Return {"porter": {...}, "de": {...}} — see `_run`'s per-mode shape."""
    return {"porter": _run(None), "de": _run("de")}


def _print_table(results: dict) -> None:
    porter, de = results["porter"], results["de"]
    print(f"\n{'query':<14} {'relevant':<10} {'porter recall':>14} {'de recall':>10}")
    print("-" * 52)
    for q in QUERIES:
        query = q["query"]
        print(
            f"{query:<14} {str(q['relevant']):<10} "
            f"{porter['per_query'][query]['recall']:>14.2f} "
            f"{de['per_query'][query]['recall']:>10.2f}"
        )
    print("-" * 52)
    print(
        f"mean_recall: porter={porter['mean_recall']:.3f}  de={de['mean_recall']:.3f}"
    )
    print(f"mrr:         porter={porter['mrr']:.3f}  de={de['mrr']:.3f}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", help="write full results to this path")
    args = parser.parse_args()

    results = run_benchmark()
    _print_table(results)
    if args.json:
        Path(args.json).write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
