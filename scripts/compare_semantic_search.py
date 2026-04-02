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
    print(f"  {'Case':<52} {'FTS5':>6} {'Semantic':>10}")
    print(f"  {'-' * 52} {'-' * 6} {'-' * 10}")

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

        print(f"  {desc:<52} {fts_str}  {sem_str}  {marker}")

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
# Main (stub — replaced in Task 2)
# ---------------------------------------------------------------------------


def main() -> None:
    _check_fastembed()

    print(bold("\npdf-mcp Semantic vs FTS5 — Comparison Report"))
    print(f"{'─' * 68}")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        r1 = run_meaning_matching(tmp / "meaning")

    _section("Summary")
    print()
    _row(
        "1. Meaning-based matching (semantic wins)",
        green("PASS") if r1 else red("FAIL"),
        r1,
    )
    print()
    sys.exit(0 if r1 else 1)


if __name__ == "__main__":
    main()
