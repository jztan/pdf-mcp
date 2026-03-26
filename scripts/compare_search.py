#!/usr/bin/env python
"""
scripts/compare_search.py

Standalone comparison report: FTS5 search vs Python scan fallback.

Proves three improvements of the v1.6.0 FTS5 upgrade:
  1. Relevance ranking  — BM25 surfaces the most-relevant page first
  2. Porter stemming    — finds morphological variants the Python .find() misses
  3. Performance        — pre-indexed FTS5 vs load-all-pages + scan

Run:
    python scripts/compare_search.py
"""

import os
import sys
import tempfile
import time
from pathlib import Path

# Allow running from either the project root or the scripts/ directory.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pymupdf  # noqa: E402

from pdf_mcp.cache import PDFCache  # noqa: E402
from pdf_mcp.server import _python_search  # noqa: E402

# ---------------------------------------------------------------------------
# Terminal colours (degrade gracefully when not a tty)
# ---------------------------------------------------------------------------
_IS_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _IS_TTY else text


def green(t: str) -> str:
    return _c("32", t)


def red(t: str) -> str:
    return _c("31", t)


def bold(t: str) -> str:
    return _c("1", t)


def cyan(t: str) -> str:
    return _c("36", t)


def yellow(t: str) -> str:
    return _c("33", t)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPS = 20  # timing repetitions


def _build_pdf(page_texts: dict[int, str]) -> str:
    """Write a PDF to a temp file and return its resolved path."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        doc = pymupdf.open()
        for i in sorted(page_texts.keys()):
            page = doc.new_page()
            page.insert_text((50, 50), page_texts[i])
        doc.save(f.name)
        doc.close()
        return str(Path(f.name).resolve())


def _populate(cache: PDFCache, pdf_path: str, page_texts: dict[int, str]) -> None:
    for page_num, text in page_texts.items():
        cache.save_page_text(pdf_path, page_num, text)


def _section(title: str) -> None:
    width = 68
    print()
    print(bold(cyan("=" * width)))
    print(bold(cyan(f"  {title}")))
    print(bold(cyan("=" * width)))


def _row(label: str, value: str, ok: bool | None = None) -> None:
    marker = ""
    if ok is True:
        marker = green(" ✓")
    elif ok is False:
        marker = red(" ✗")
    print(f"  {label:<36} {value}{marker}")


def _result_table(label: str, results: list[dict]) -> None:
    print(f"  {bold(label)}")
    if not results:
        print(f"    {yellow('(no matches)')}")
        return
    for r in results:
        score = f"  score={r['score']:.3f}" if r.get("score", 0) else ""
        excerpt = r.get("excerpt", "")[:60].replace("\n", " ")
        print(f"    page {r['page']:>3}{score}  │ {excerpt}")


# ---------------------------------------------------------------------------
# 1. Relevance ranking
# ---------------------------------------------------------------------------

RELEVANCE_TEXTS = {
    0: "alpha appears once on this page",
    1: "no match here filler content for test",
    2: "more filler content nothing relevant here",
    3: "still nothing on this page at all",
    4: "alpha " * 10,
}


def run_ranking(tmpdir: Path) -> bool:
    _section("1. Relevance Ranking  (BM25 vs document order)")

    cache = PDFCache(cache_dir=tmpdir, ttl_hours=1)
    if not cache.fts_available:
        print(red("  FTS5 not available — skipping ranking comparison"))
        return False

    pdf_path = _build_pdf(RELEVANCE_TEXTS)
    _populate(cache, pdf_path, RELEVANCE_TEXTS)

    fts_results = cache.search_fts(pdf_path, "alpha", max_results=5, context_chars=80)
    py_matches, _ = _python_search(
        RELEVANCE_TEXTS, "alpha", max_results=5, context_chars=80
    )

    print()
    print(f"  Setup: 5-page PDF — page 1 has 1×'alpha', page 5 has 10×'alpha'")
    print(f"  Query: {bold('alpha')}")
    print()

    print(f"  {bold('FTS5 results')}  (ordered by BM25 relevance score)")
    _result_table("", fts_results)

    print()
    print(f"  {bold('Python scan results')}  (ordered by page number)")
    _result_table("", py_matches)

    fts_first = fts_results[0]["page"] if fts_results else None
    py_first = py_matches[0]["page"] if py_matches else None
    fts_score_p5 = next((r["score"] for r in fts_results if r["page"] == 5), 0.0)
    fts_score_p1 = next((r["score"] for r in fts_results if r["page"] == 1), 0.0)

    print()
    print(bold("  Verdict"))
    _row(
        "FTS5 returns most-relevant page first",
        f"page {fts_first}  (10 mentions)",
        fts_first == 5,
    )
    _row(
        "Python scan returns page in doc order",
        f"page {py_first}  (1 mention)",
        py_first == 1,
    )
    _row(
        "FTS5 BM25 score page-5 > page-1",
        f"{fts_score_p5:.3f} > {fts_score_p1:.3f}",
        fts_score_p5 > fts_score_p1,
    )
    _row(
        "Python scan scores",
        "0.000 for all results",
        all(m["score"] == 0.0 for m in py_matches),
    )

    os.unlink(pdf_path)
    return fts_first == 5 and py_first == 1


# ---------------------------------------------------------------------------
# 2. Porter stemming
# ---------------------------------------------------------------------------

STEMMING_CASES = [
    # (description, page_text, query, fts5_should_match, python_should_match)
    (
        "query='managing'  text has 'management'",
        "The management team approved the plan.",
        "managing",
        True,
        False,
    ),
    (
        "query='management' text has 'managing'",
        "The managing director signed off today.",
        "management",
        True,
        False,
    ),
    (
        "query='running'   text has 'run'",
        "I like to run every morning.",
        "running",
        True,
        False,
    ),
    (
        "query='managed'   text has 'management'",
        "The management decision was final.",
        "managed",
        True,
        False,
    ),
]


def run_stemming(tmpdir: Path) -> bool:
    _section("2. Porter Stemming  (FTS5 cross-form vs Python literal .find())")

    cache = PDFCache(cache_dir=tmpdir, ttl_hours=1)
    if not cache.fts_available:
        print(red("  FTS5 not available — skipping stemming comparison"))
        return False

    print()
    print(
        f"  {'Case':<44} {'FTS5':>6} {'Python':>8}"
    )
    print(f"  {'-'*44} {'-'*6} {'-'*8}")

    all_ok = True
    for desc, text, query, fts_expect, py_expect in STEMMING_CASES:
        page_texts = {0: text}
        pdf_path = _build_pdf(page_texts)
        _populate(cache, pdf_path, page_texts)

        fts_results = cache.search_fts(
            pdf_path, query, max_results=5, context_chars=60
        )
        py_matches, _ = _python_search(
            page_texts, query, max_results=5, context_chars=60
        )

        fts_hit = len(fts_results) > 0
        py_hit = len(py_matches) > 0

        fts_ok = fts_hit == fts_expect
        py_ok = py_hit == py_expect
        ok = fts_ok and py_ok
        all_ok = all_ok and ok

        fts_str = green("MATCH") if fts_hit else red("miss ")
        py_str = red("MATCH") if py_hit else green("miss ")
        marker = green("✓") if ok else red("✗")

        print(f"  {desc:<44} {fts_str}  {py_str}  {marker}")

        os.unlink(pdf_path)

    print()
    print(f"  {bold('Verdict')}")
    _row(
        "FTS5 finds all stemmed variants",
        green("yes") if all_ok else red("no"),
        all_ok,
    )
    _row(
        "Python scan: literal .find() misses cross-forms",
        green("confirmed") if all_ok else red("unexpected"),
        all_ok,
    )

    return all_ok


# ---------------------------------------------------------------------------
# 3. Performance
# ---------------------------------------------------------------------------

PERF_FILLER = (
    "The quick brown fox jumps over the lazy dog. "
    "Business processes improve efficiency across all departments. "
    "Strategic initiatives drive organizational growth. "
) * 20

PERF_PAGES = 200
PERF_RARE_PAGE = 99
PERF_RARE_WORD = "zephyr"


def _time_avg(fn, reps: int) -> float:
    t0 = time.perf_counter()
    for _ in range(reps):
        fn()
    return (time.perf_counter() - t0) / reps


def run_performance(tmpdir: Path) -> bool:
    _section("3. Performance  (pre-indexed FTS5 vs load-all + Python scan)")

    cache = PDFCache(cache_dir=tmpdir, ttl_hours=1)
    if not cache.fts_available:
        print(red("  FTS5 not available — skipping performance comparison"))
        return False

    print(f"\n  Building {PERF_PAGES}-page PDF and indexing…", end="", flush=True)
    texts = {}
    for i in range(PERF_PAGES):
        if i == PERF_RARE_PAGE:
            texts[i] = f"The {PERF_RARE_WORD} wind blew gently. " + PERF_FILLER
        else:
            texts[i] = PERF_FILLER

    pdf_path = _build_pdf(texts)
    _populate(cache, pdf_path, texts)
    page_nums = list(range(PERF_PAGES))
    print(" done")

    scenarios = [
        (
            f"Rare query   ('{PERF_RARE_WORD}', 1 match in {PERF_PAGES} pages)",
            PERF_RARE_WORD,
        ),
        (
            f"Absent query (no match in {PERF_PAGES} pages)",
            "zyxwvutsrqponmlkji",
        ),
    ]

    print()
    print(
        f"  {'Scenario':<46} {'FTS5':>8} {'Python':>10} {'Speedup':>8}"
    )
    print(f"  {'-'*46} {'-'*8} {'-'*10} {'-'*8}")

    all_ok = True
    for label, query in scenarios:
        # warm-up
        cache.search_fts(pdf_path, query, max_results=10, context_chars=200)
        cache.get_pages_text(pdf_path, page_nums)

        fts_avg = _time_avg(
            lambda q=query: cache.search_fts(
                pdf_path, q, max_results=10, context_chars=200
            ),
            REPS,
        )
        py_avg = _time_avg(
            lambda q=query: _python_search(
                cache.get_pages_text(pdf_path, page_nums),
                q,
                max_results=10,
                context_chars=200,
            ),
            REPS,
        )

        speedup = py_avg / fts_avg
        ok = speedup >= 3.0
        all_ok = all_ok and ok

        speedup_str = green(f"{speedup:.1f}x") if ok else red(f"{speedup:.1f}x")
        print(
            f"  {label:<46} {fts_avg*1000:>6.2f}ms "
            f"{py_avg*1000:>8.2f}ms  {speedup_str}"
        )

    print()
    print(f"  {bold('Verdict')}")
    _row("FTS5 ≥3x faster in all scenarios", green("yes") if all_ok else red("no"), all_ok)

    os.unlink(pdf_path)
    return all_ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    print(bold("\npdf-mcp FTS5 vs Python Scan — Comparison Report"))
    print(f"{'─' * 68}")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        r1 = run_ranking(tmp / "ranking")
        r2 = run_stemming(tmp / "stemming")
        r3 = run_performance(tmp / "perf")

    _section("Summary")
    print()
    _row("1. Relevance ranking (BM25 > page order)", green("PASS") if r1 else red("FAIL"), r1)
    _row("2. Porter stemming (cross-form matching)", green("PASS") if r2 else red("FAIL"), r2)
    _row("3. Performance (≥3x speedup)",             green("PASS") if r3 else red("FAIL"), r3)
    print()

    if r1 and r2 and r3:
        print(bold(green("  All comparisons passed — FTS5 is demonstrably better.")))
    else:
        print(bold(red("  Some comparisons failed — review output above.")))
    print()
    sys.exit(0 if (r1 and r2 and r3) else 1)


if __name__ == "__main__":
    main()
