#!/usr/bin/env python
"""
scripts/compare_semantic_search.py

Standalone comparison report: pdf_semantic_search vs pdf_search (FTS5).

Proves three properties of the tools:
  1. Meaning-based matching  — semantic finds synonyms/paraphrases FTS5 misses
  2. Exact-term search       — FTS5 finds precise identifiers semantic dilutes
  3. Performance             — cold-start cost vs warm query speed

Run:
    python scripts/compare_semantic_search.py

Requires: pip install 'pdf-mcp[semantic]'
"""

import os
import sys
import tempfile
import time
from pathlib import Path

# Allow running from either the project root or the scripts/ directory.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pdf_mcp.server as server_module  # noqa: E402
import pymupdf  # noqa: E402

from pdf_mcp.cache import PDFCache  # noqa: E402
from pdf_mcp.server import pdf_search, pdf_semantic_search  # noqa: E402

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
# Shared helpers
# ---------------------------------------------------------------------------


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
    print(f"  {label:<40} {value}{marker}")


def _result_table(label: str, results: list[dict]) -> None:
    """Print up to 3 results; handles both pdf_search and pdf_semantic_search shapes."""
    print(f"  {bold(label)}")
    if not results:
        print(f"    {yellow('(no matches)')}")
        return
    for r in results[:3]:
        score = f"  score={r['score']:.3f}" if r.get("score", 0) else ""
        text = (r.get("excerpt") or r.get("snippet") or "")[:60].replace("\n", " ")
        print(f"    page {r['page']:>3}{score}  │ {text}")


# ---------------------------------------------------------------------------
# Fastembed availability guard
# ---------------------------------------------------------------------------


def _check_fastembed() -> None:
    """Exit with a clear message if fastembed is not installed."""
    try:
        import fastembed  # noqa: F401
    except ImportError:
        print(
            red(
                "\nError: fastembed is not installed.\n"
                "Install it with: pip install 'pdf-mcp[semantic]'\n"
            )
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Section 1: Meaning-based matching (semantic wins)
# ---------------------------------------------------------------------------

_MEANING_FILLER = (
    "The committee reviewed the quarterly agenda and noted action items. "
    "Administrative procedures were discussed and approved by all attendees. "
    "Next steps include scheduling follow-up meetings across departments."
)

_MEANING_CASES = [
    (
        "search 'income growth'          → 'revenue increased'",
        "revenue increased 12% this quarter, driven by strong product sales.",
        "income growth",
        2,  # target is page 2 (1-indexed); built as key 1 in 0-indexed dict
    ),
    (
        "search 'staff were let go'      → 'workforce reduction'",
        "the firm announced a workforce reduction affecting 500 jobs this year.",
        "staff were let go",
        2,
    ),
    (
        "search 'poor financial results' → 'earnings disappointed'",
        "earnings disappointed investors as margins contracted sharply in Q3.",
        "poor financial results",
        2,
    ),
]


def run_meaning_matching(tmpdir: Path) -> bool:
    _section("1. Meaning-Based Matching  (semantic wins)")

    cache = PDFCache(cache_dir=tmpdir, ttl_hours=1)
    server_module.cache = cache

    if not cache.fts_available:
        print(red("  FTS5 not available — skipping section"))
        return False

    print()
    print(f"  {'Case':<58} {'FTS5':>6} {'Semantic':>10}")
    print(f"  {'-' * 58} {'-' * 6} {'-' * 10}")

    all_ok = True
    for desc, target_text, query, target_page in _MEANING_CASES:
        page_texts = {0: _MEANING_FILLER, 1: target_text, 2: _MEANING_FILLER}
        pdf_path = _build_pdf(page_texts)

        fts_result = pdf_search(pdf_path, query, max_results=5, context_chars=80)
        sem_result = pdf_semantic_search(pdf_path, query, top_k=5)

        fts_matches = fts_result.get("matches", [])
        sem_results = sem_result.get("results", [])

        fts_hit = any(m["page"] == target_page for m in fts_matches)
        sem_hit = bool(sem_results) and sem_results[0]["page"] == target_page

        fts_str = red("miss ") if not fts_hit else green("MATCH")
        sem_str = green("MATCH") if sem_hit else red("miss ")
        ok = (not fts_hit) and sem_hit
        all_ok = all_ok and ok
        marker = green("✓") if ok else red("✗")

        print(f"  {desc:<58} {fts_str}  {sem_str}  {marker}")

        os.unlink(pdf_path)

    print()
    print(bold("  Verdict"))
    _row(
        "Semantic finds synonym/paraphrase matches",
        green("3/3") if all_ok else red("not all"),
        all_ok,
    )
    _row(
        "FTS5 misses all synonym/paraphrase cases",
        green("confirmed") if all_ok else red("unexpected"),
        all_ok,
    )
    return all_ok


# ---------------------------------------------------------------------------
# Section 2: Exact-term search (FTS5 wins)
# ---------------------------------------------------------------------------

_EXACT_FILLER = (
    "The quarterly business report summarizes financial performance across divisions. "
    "Revenue targets were reviewed and expenditure budgets approved for the period. "
    "The executive team discussed strategic priorities and operational efficiency."
)

_EXACT_CASES = [
    (
        "query 'QX-7749-BRAVO'    (product code, planted on page 3)",
        "QX-7749-BRAVO",
        "The inventory system flagged product code QX-7749-BRAVO for urgent reorder.",
        3,  # 1-indexed target page
    ),
    (
        "query 'INV-2024-00847'   (invoice ref, planted on page 2)",
        "INV-2024-00847",
        "Payment for invoice INV-2024-00847 was processed and reconciled on the 15th.",
        2,
    ),
    (
        "query 'Amendment 7B'     (clause ref, planted on page 4)",
        "Amendment 7B",
        "Under the terms of Amendment 7B the liability cap is revised upward.",
        4,
    ),
]


def run_exact_term(tmpdir: Path) -> bool:
    _section("2. Exact-Term Search  (FTS5 wins)")

    cache = PDFCache(cache_dir=tmpdir, ttl_hours=1)
    server_module.cache = cache

    if not cache.fts_available:
        print(red("  FTS5 not available — skipping section"))
        return False

    print()
    print(f"  {'Case':<60} {'FTS5 top-1':>10} {'Semantic top-1':>15}")
    print(f"  {'-' * 60} {'-' * 10} {'-' * 15}")

    all_ok = True
    for desc, query, planted_text, planted_page in _EXACT_CASES:
        # 5 pages all with identical filler; planted_page has the unique identifier
        page_texts = {i: _EXACT_FILLER for i in range(5)}
        page_texts[planted_page - 1] = planted_text + " " + _EXACT_FILLER

        pdf_path = _build_pdf(page_texts)

        fts_result = pdf_search(pdf_path, query, max_results=5, context_chars=80)
        sem_result = pdf_semantic_search(pdf_path, query, top_k=5)

        fts_matches = fts_result.get("matches", [])
        sem_results = sem_result.get("results", [])

        fts_top = fts_matches[0]["page"] if fts_matches else None
        sem_top = sem_results[0]["page"] if sem_results else None

        fts_ok = fts_top == planted_page
        all_ok = all_ok and fts_ok

        fts_str = green(f"page {fts_top} ✓") if fts_ok else red(f"page {fts_top} ✗")
        sem_str = f"page {sem_top}" if sem_top else yellow("none")

        print(f"  {desc:<60} {fts_str:>10}  {sem_str:>14}")

        os.unlink(pdf_path)

    print()
    print(bold("  Verdict"))
    _row(
        "FTS5 finds exact identifier at rank 1",
        green("3/3") if all_ok else red("not all"),
        all_ok,
    )
    _row(
        "Semantic top-1 (informational — not scored)",
        "shown above",
        None,
    )
    return all_ok


# ---------------------------------------------------------------------------
# Section 3: Performance (cold start vs warm)
# ---------------------------------------------------------------------------

_PERF_PAGES = 200
_PERF_WARM_REPS = 5
_PERF_RARE_PAGE = 99  # 0-indexed
_PERF_RARE_WORD = "zyxuventa"  # invented word with no semantic meaning
_PERF_FILLER = (
    "The quarterly business review covered operational metrics and financial targets. "
    "Management approved budget allocations and reviewed headcount planning. "
    "Strategic objectives were discussed with a focus on market expansion."
) * 8


def run_performance(tmpdir: Path) -> bool:
    _section("3. Performance  (cold start vs warm queries)")

    cache = PDFCache(cache_dir=tmpdir, ttl_hours=1)
    server_module.cache = cache

    print(f"\n  Building {_PERF_PAGES}-page PDF…", end="", flush=True)
    page_texts: dict[int, str] = {}
    for i in range(_PERF_PAGES):
        if i == _PERF_RARE_PAGE:
            page_texts[i] = f"The term {_PERF_RARE_WORD} appears here. " + _PERF_FILLER
        else:
            page_texts[i] = _PERF_FILLER
    pdf_path = _build_pdf(page_texts)
    print(" done")

    # --- Cold start ---
    print("\n  Cold start (no cache) — this may take 30–90 s on CPU…")

    t0 = time.perf_counter()
    pdf_search(pdf_path, _PERF_RARE_WORD, max_results=5, context_chars=80)
    fts_cold = time.perf_counter() - t0

    t0 = time.perf_counter()
    pdf_semantic_search(pdf_path, "unusual invented terminology", top_k=5)
    sem_cold = time.perf_counter() - t0

    print()
    print(f"  {'Method':<20} {'Cold start':>12}")
    print(f"  {'-' * 20} {'-' * 12}")
    print(f"  {'FTS5':<20} {fts_cold * 1000:>10.0f}ms")
    print(
        f"  {'Semantic':<20} {sem_cold * 1000:>10.0f}ms"
        f"  (embeds {_PERF_PAGES} pages)"
    )

    # --- Warm queries ---
    print(f"\n  Warm queries (avg over {_PERF_WARM_REPS} reps)…")

    t0 = time.perf_counter()
    for _ in range(_PERF_WARM_REPS):
        pdf_search(pdf_path, _PERF_RARE_WORD, max_results=5, context_chars=80)
    fts_warm = (time.perf_counter() - t0) / _PERF_WARM_REPS

    t0 = time.perf_counter()
    for _ in range(_PERF_WARM_REPS):
        pdf_semantic_search(pdf_path, "unusual invented terminology", top_k=5)
    sem_warm = (time.perf_counter() - t0) / _PERF_WARM_REPS

    print()
    print(f"  {'Method':<20} {'Warm query':>12}")
    print(f"  {'-' * 20} {'-' * 12}")

    fts_warm_ok = fts_warm < 0.050
    sem_warm_ok = sem_warm < 0.050

    print(
        f"  {'FTS5':<20} {fts_warm * 1000:>10.1f}ms"
        + (green("  < 50 ms ✓") if fts_warm_ok else red("  ≥ 50 ms ✗"))
    )
    print(
        f"  {'Semantic':<20} {sem_warm * 1000:>10.1f}ms"
        + (green("  < 50 ms ✓") if sem_warm_ok else red("  ≥ 50 ms ✗"))
    )

    all_ok = fts_warm_ok and sem_warm_ok

    print()
    print(bold("  Verdict"))
    _row(
        "Both warm queries < 50 ms",
        green("yes") if all_ok else red("no"),
        all_ok,
    )

    os.unlink(pdf_path)
    return all_ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    _check_fastembed()

    print(bold("\npdf-mcp Semantic vs FTS5 — Comparison Report"))
    print(f"{'─' * 68}")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        r1 = run_meaning_matching(tmp / "meaning")
        r2 = run_exact_term(tmp / "exact")
        r3 = run_performance(tmp / "perf")

    _section("Summary")
    print()
    _row(
        "1. Meaning-based matching (semantic wins 3/3)",
        green("PASS") if r1 else red("FAIL"),
        r1,
    )
    _row(
        "2. Exact-term search (FTS5 correct 3/3)",
        green("PASS") if r2 else red("FAIL"),
        r2,
    )
    _row(
        "3. Performance (warm queries < 50 ms)",
        green("PASS") if r3 else red("FAIL"),
        r3,
    )
    print()

    if r1 and r2 and r3:
        print(bold(green("  All comparisons passed — choose your tool wisely.")))
    else:
        print(bold(red("  Some comparisons failed — review output above.")))
    print()
    sys.exit(0 if (r1 and r2 and r3) else 1)


if __name__ == "__main__":
    main()
