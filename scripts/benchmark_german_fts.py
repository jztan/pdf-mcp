#!/usr/bin/env python
"""
scripts/benchmark_german_fts.py

Before/after recall and MRR for `[fts] language = "de"` (the German-stemmed
FTS mirror index, `pdf_search_fts_de` in cache.py) versus the default
`porter unicode61` keyword index, over a small synthetic German corpus.

No external corpus is required for the quality benchmark: the "documents"
are short German paragraphs written directly into this script, each on its
own synthetic page, covering the gaps the option addresses:

- inflection: a query word and the relevant page use different forms of the
  same word (`kündigen` / `Kündigung` / `gekündigt`)
- ASCII-transliteration spelling: `ue`/`ss` for `ü`/`ß` on either side
  (`Kuendigung` / `Kündigung`, `Strasse` / `Straße`)
- numbers and statute citations: a query must still match a citation like
  "§ 626 BGB" and must not match every page that merely mentions "BGB"
- multi-word queries: the AND-semantics of `_escape_fts5_query_de` (every
  stem in the query must be present) against a distractor page carrying
  only one of the two words, and the OR retry (`_fts5_or_fallback_de`) for
  a query no page satisfies in full, mirroring the default keyword path
- distractor pages sharing surface vocabulary but not the query's topic, to
  keep recall/MRR honest rather than trivially 1.0 on a single-page corpus

Ground truth (`page` per query) is hand-authored here since the corpus is
hand-authored too — small enough to eyeball, unlike the CJK benchmark's
literal-substring auto-derivation over a real corpus.

`--latency` additionally times `search_fts` (porter vs. "de") on a real
100+ page 10-K from benchmark_data/financial_reports/manifest.json,
fetched with `scripts/fetch_financial_corpus.py` -- skipped, not failed,
when that corpus is absent locally, same as `benchmark_cjk_keyword.py`'s
`corpus_available()` pattern. The document's language does not matter for
a timing measurement (it is English): the "de" search path re-tokenizes
and stems whatever text is on the page regardless of what language it is
actually in, so timing it on any large document measures the real cost.

Usage:
    python scripts/benchmark_german_fts.py
    python scripts/benchmark_german_fts.py --json out.json
    python scripts/benchmark_german_fts.py --latency
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from pdf_mcp.cache import PDFCache  # noqa: E402
from pdf_mcp.extractor import extract_text_from_page  # noqa: E402

import bench_env  # noqa: E402

MANIFEST = REPO / "benchmark_data" / "financial_reports" / "manifest.json"
# googl-fy2023 is a 121-page 10-K -- the exact document size the PR #44
# review measured the pre-fix ~1s/query re-stemming cost against.
LATENCY_DOC_ID = "googl-fy2023"
LATENCY_QUERIES = ["revenue", "risk factors", "litigation"]

PAGES: dict[int, str] = {
    0: "Die Kündigung des Arbeitsvertrags war form- und fristgerecht.",
    1: "Der Arbeitgeber kündigte dem Mitarbeiter zum Monatsende.",
    2: "Nach zwei Kündigungen in Folge suchte er eine neue Anstellung.",
    3: "Die Straße vor dem Rathaus wurde neu asphaltiert.",
    4: "Der Fußball rollte über den Platz vor dem Stadion.",
    5: "Der Urlaubsanspruch entsteht anteilig im ersten Beschäftigungsjahr.",
    6: "Das Arbeitsgericht wies die Klage wegen Fristversäumnis ab.",
    7: "Am Wochenende fand ein Konzert in der Stadthalle statt.",
    8: "Der Schadensersatzanspruch verjährt gemäß § 626 BGB im Jahr 2023.",
    9: "Das BGB regelt zahlreiche Alltagsfragen ohne Bezug zu Fristen.",
    10: "Der Arbeitsvertrag ist befristet und endet automatisch.",
    11: "Der Arbeitsvertrag wurde heute unterschrieben.",
    # OR-fallback regression: no page carries both "622" and "bgb" (13 has
    # the number without the code name; 9 has the code name without this
    # number; 8 has the code name with a DIFFERENT number, "626"), so the
    # AND form of "§ 622 BGB" matches nothing and only the OR retry can
    # find the true citation, page 13. Page 12 doubles as a "626"/"1626"
    # discrimination guard alongside the existing page 8.
    12: "§ 1626 Elterliche Sorge der Eltern für das Kind.",
    13: "§ 622 Gesetzliche Fristen bei ordentlicher Beendigung.",
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
    # numbers and statute citations (regression guard for the digit-dropping
    # tokenizer bug: "626" must hit the citation page and not every page
    # that merely says "BGB")
    {"query": "626", "relevant": [8]},
    {"query": "§ 626 BGB", "relevant": [8]},
    {"query": "2023", "relevant": [8]},
    # multi-word query: both stems must be present (AND-semantics) -- page
    # 11 shares "Arbeitsvertrag" but not "befristet", so only page 10,
    # which has both, is relevant
    {"query": "befristeter Arbeitsvertrag", "relevant": [10]},
    # OR-fallback regression (PR #44 round 2): AND-only matches nothing
    # (no page has both "622" and "bgb"), so this query is only answered
    # via the OR retry -- exactly the case the maintainer reported against
    # a real BGB PDF.
    {"query": "§ 622 BGB", "relevant": [13]},
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
    width = max(len(q["query"]) for q in QUERIES) + 1
    print(
        f"\n{'query':<{width}} {'relevant':<10} {'porter recall':>14} {'de recall':>10}"
    )
    print("-" * (width + 38))
    for q in QUERIES:
        query = q["query"]
        print(
            f"{query:<{width}} {str(q['relevant']):<10} "
            f"{porter['per_query'][query]['recall']:>14.2f} "
            f"{de['per_query'][query]['recall']:>10.2f}"
        )
    print("-" * (width + 38))
    print(
        f"mean_recall: porter={porter['mean_recall']:.3f}  de={de['mean_recall']:.3f}"
    )
    print(f"mrr:         porter={porter['mrr']:.3f}  de={de['mrr']:.3f}")


def latency_corpus_available() -> bool:
    """True if the manifest and the one document `run_latency` uses are
    both present locally. Mirrors `benchmark_cjk_keyword.py`'s
    `corpus_available()`: skip (exit 0), don't fail, when the corpus
    hasn't been fetched."""
    if not MANIFEST.exists():
        return False
    docs = {d["id"]: d for d in json.loads(MANIFEST.read_text())["docs"]}
    doc = docs.get(LATENCY_DOC_ID)
    return bool(doc) and (REPO / doc["path"]).exists()


def _old_de_search_time(conn, path: str, query: str) -> float:
    """Reproduce the PRE-FIX per-query cost of `_build_temp_page_fts(de=True)`:
    re-stem every page of the document from `page_text` on every call,
    instead of copying already-stemmed rows from `pdf_search_fts_de`. This
    is exactly the code the fix in cache.py deleted from the query path --
    inlined here (not imported) since deleting it from the query loop was
    the whole point of the fix, so it no longer exists there to call.
    """
    from pdf_mcp.cache import _escape_fts5_query_de, _german_normalize

    t0 = time.perf_counter()
    conn.execute("DROP TABLE IF EXISTS temp.doc_fts_old")
    conn.execute(
        "CREATE VIRTUAL TABLE temp.doc_fts_old USING fts5("
        "page_num UNINDEXED, text, tokenize='unicode61')"
    )
    rows = conn.execute(
        "SELECT page_num, text FROM page_text WHERE file_path = ?", (path,)
    ).fetchall()
    conn.executemany(
        "INSERT INTO temp.doc_fts_old (page_num, text) VALUES (?, ?)",
        [(pn, _german_normalize(txt)) for pn, txt in rows],
    )
    escaped = _escape_fts5_query_de(query)
    conn.execute(
        "SELECT page_num FROM doc_fts_old WHERE doc_fts_old MATCH ?"
        " ORDER BY bm25(doc_fts_old) LIMIT 10",
        (escaped,),
    ).fetchall()
    return time.perf_counter() - t0


def run_latency(repeats: int = 5) -> dict:
    """Median per-query time on a real 100+ page document: the default
    porter path, the current "de" path (copies pre-stemmed rows from
    `pdf_search_fts_de` -- see `_build_temp_page_fts` in cache.py), and
    the pre-fix "de" path (`_old_de_search_time`, re-stems the whole
    document on every call).
    """
    import pymupdf

    docs = {d["id"]: d for d in json.loads(MANIFEST.read_text())["docs"]}
    pdf_path = str(REPO / docs[LATENCY_DOC_ID]["path"])

    doc = pymupdf.open(pdf_path)
    try:
        pages = {i: extract_text_from_page(doc[i]) for i in range(len(doc))}
    finally:
        doc.close()
    page_count = len(pages)

    tmp = tempfile.mkdtemp(prefix="german_fts_latency_")

    porter_cache = PDFCache(cache_dir=Path(tmp) / "porter")
    porter_cache.save_pages_text(pdf_path, pages)
    de_cache = PDFCache(cache_dir=Path(tmp) / "de", fts_language="de")
    de_cache.save_pages_text(pdf_path, pages)

    def _median(fn) -> float:
        times = [fn(q) for _ in range(repeats) for q in LATENCY_QUERIES]
        return statistics.median(times)

    def _timed_porter_search(query: str) -> float:
        t0 = time.perf_counter()
        porter_cache.search_fts(pdf_path, query, 10, 200)
        return time.perf_counter() - t0

    def _timed_de_search(query: str) -> float:
        t0 = time.perf_counter()
        de_cache.search_fts(pdf_path, query, 10, 200)
        return time.perf_counter() - t0

    with de_cache._connect() as old_conn:
        median_de_before = _median(lambda q: _old_de_search_time(old_conn, pdf_path, q))

    return {
        "doc_id": LATENCY_DOC_ID,
        "page_count": page_count,
        "queries": LATENCY_QUERIES,
        "repeats": repeats,
        "median_seconds": {
            "porter": _median(_timed_porter_search),
            "de_before_fix": median_de_before,
            "de_after_fix": _median(_timed_de_search),
        },
        "environment": bench_env.environment(),
    }


def _print_latency(result: dict) -> None:
    ms = result["median_seconds"]
    print(
        f"\nlatency ({result['doc_id']}, {result['page_count']} pages, "
        f"{len(result['queries'])} queries x {result['repeats']} repeats):"
    )
    print(f"  porter (default):    {ms['porter'] * 1000:.2f} ms/query (median)")
    print(f"  de, before this fix: {ms['de_before_fix'] * 1000:.2f} ms/query (median)")
    print(f"  de, after this fix:  {ms['de_after_fix'] * 1000:.2f} ms/query (median)")
    print(f"  env: {bench_env.markdown_line(result['environment'])}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", help="write full results to this path")
    parser.add_argument(
        "--latency",
        action="store_true",
        help=(
            "also (or only) time search_fts on a real 100+ page 10-K "
            "(skipped if benchmark_data/financial_reports isn't fetched "
            "-- run scripts/fetch_financial_corpus.py first)"
        ),
    )
    args = parser.parse_args()

    results = run_benchmark()
    _print_table(results)

    if args.latency:
        if not latency_corpus_available():
            print(
                "\nlatency: skipped (financial-report corpus not fetched; "
                "run scripts/fetch_financial_corpus.py)",
                file=sys.stderr,
            )
        else:
            latency = run_latency()
            _print_latency(latency)
            results["latency"] = latency

    if args.json:
        Path(args.json).write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
