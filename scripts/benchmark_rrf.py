#!/usr/bin/env python
"""
scripts/benchmark_rrf.py

Benchmark: RRF hybrid search vs keyword-only vs semantic-only.

Run synthetic scenarios (always):
    python scripts/benchmark_rrf.py

Run with a real PDF (optional — appends a "Real PDF" section):
    python scripts/benchmark_rrf.py --pdf path/to/doc.pdf \\
        --query "your query" --relevant-pages "1,3,5"

--pdf accepts a local path or a URL.
Always exits 0 (informational report, no CI gate).
"""

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pymupdf  # noqa: E402
import pdf_mcp.server as server_module  # noqa: E402
from pdf_mcp.cache import PDFCache  # noqa: E402
from pdf_mcp.server import _resolve_path, pdf_search  # noqa: E402

# Detect fastembed once at import time.
try:
    import fastembed  # type: ignore  # noqa: F401
    _FASTEMBED_AVAILABLE = True
except ImportError:
    _FASTEMBED_AVAILABLE = False

# Accumulated output lines (with ANSI) for saving to files.
_OUTPUT: list[str] = []

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


def _p(text: str = "") -> None:
    """Print a line and append to output buffer for file saving."""
    _OUTPUT.append(text)
    print(text)


def _section(title: str) -> None:
    width = 68
    _p()
    _p(bold(cyan("=" * width)))
    _p(bold(cyan(f"  {title}")))
    _p(bold(cyan("=" * width)))


def _row(label: str, value: str, ok: bool | None = None) -> None:
    marker = ""
    if ok is True:
        marker = green(" ✓")
    elif ok is False:
        marker = red(" ✗")
    _p(f"  {label:<36} {value}{marker}")


FILLER = "The ancient oak tree stood beside the quiet mountain stream."


def _build_pdf(page_texts: dict[int, str]) -> str:
    """Write a PDF to a temp file and return its absolute path. Keys are 0-indexed."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        doc = pymupdf.open()
        for i in sorted(page_texts.keys()):
            page = doc.new_page()
            page.insert_text((50, 50), page_texts[i])
        doc.save(f.name)
        doc.close()
        return str(Path(f.name).resolve())


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes for plain-text file output."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _compute_metrics(
    matches: list[dict], relevant_pages: set[int], k: int
) -> dict:
    """
    Compute recall@K, RR (Reciprocal Rank), and rank-of-first-hit.

    matches: list of {"page": N, ...} from pdf_search (page is 1-indexed)
    relevant_pages: 1-indexed page numbers that are ground-truth relevant
    k: cutoff — only the first k entries in matches are considered

    Returns:
        {"recall": float, "rr": float, "rank_first_hit": int | None}
        recall = |relevant ∩ top_k| / |relevant|
        rr = 1/rank_first_hit if a relevant page is found, else 0.0
             (RR per-scenario; aggregates to MRR across multiple queries)
        rank_first_hit = 1-indexed position of first relevant page, or None
    """
    if not relevant_pages:
        return {"recall": 0.0, "rr": 0.0, "rank_first_hit": None}
    top_k_pages = [m["page"] for m in matches[:k]]
    recall = len(set(top_k_pages) & relevant_pages) / len(relevant_pages)
    rank_first_hit = None
    for i, page in enumerate(top_k_pages, 1):
        if page in relevant_pages:
            rank_first_hit = i
            break
    rr = 1.0 / rank_first_hit if rank_first_hit is not None else 0.0
    return {"recall": recall, "rr": rr, "rank_first_hit": rank_first_hit}


def _run_mode(
    pdf_path: str, query: str, api_mode: str, max_results: int
) -> list[dict]:
    """
    Call pdf_search for one mode and return the matches list.

    api_mode: "keyword", "semantic", or "auto" (hybrid).
    Returns [] on error (e.g. fastembed not installed for semantic mode).
    max_results is passed directly as the api parameter — recall@K is
    enforced by slicing matches[:K] in the caller.
    """
    result = pdf_search(pdf_path, query, mode=api_mode, max_results=max_results)
    if "error" in result:
        return []
    return result.get("matches", [])


def _run_scenario(
    name: str,
    pdf_path: str,
    query: str,
    relevant_pages: set[int],
    k: int,
) -> dict:
    """
    Run keyword, semantic, and hybrid search on pdf_path and return metrics.

    relevant_pages: 1-indexed page numbers that are ground-truth relevant
    k: recall cutoff (passed as max_results to pdf_search)

    Returns a scenario result dict ready for JSON serialization.
    Does NOT print anything — callers handle display.
    """
    mode_data: dict[str, dict] = {}
    for mode, api_mode in [
        ("keyword", "keyword"),
        ("semantic", "semantic"),
        ("hybrid", "auto"),
    ]:
        matches = _run_mode(pdf_path, query, api_mode, max_results=k)
        metrics = _compute_metrics(matches, relevant_pages, k)
        mode_data[mode] = {
            "recall": metrics["recall"],
            "rr": metrics["rr"],
            "rank_first_hit": metrics["rank_first_hit"],
            "top_pages": [m["page"] for m in matches[:k]],
        }
    return {
        "name": name,
        "query": query,
        "k": k,
        "relevant_pages": sorted(relevant_pages),
        "modes": mode_data,
    }


def _print_scenario_table(result: dict, assertions: dict) -> None:
    """
    Print the mode comparison table for one scenario.

    result: dict from _run_scenario
    assertions: {key: bool | None}  — None means N/A (fastembed absent)
    """
    k = result["k"]
    n_relevant = len(result["relevant_pages"])

    _p()
    _p(f"  Query: {bold(repr(result['query']))}   K={k}")
    _p()
    top_col = "Top-" + str(k) + " pages"
    _p(f"  {'Mode':<10} {'Recall@' + str(k):<12} {'RR':<8} {'Rank-1st':<10} {top_col}")
    _p(f"  {'─' * 9}  {'─' * 10}  {'─' * 6}  {'─' * 8}  {'─' * 20}")

    for mode in ("keyword", "semantic", "hybrid"):
        d = result["modes"][mode]
        recall = d["recall"]
        rr = d["rr"]
        rank = d["rank_first_hit"]
        top = ", ".join(str(p) for p in d["top_pages"]) or "(none)"

        hits = int(round(recall * n_relevant))
        recall_str = f"{hits}/{n_relevant}  {recall * 100:.0f}%"
        rr_str = f"{rr:.2f}"
        rank_str = f"rank {rank}" if rank is not None else "∞"

        suffix = ""
        if mode == "hybrid":
            hybrid_assertions = {
                key: val for key, val in assertions.items() if "hybrid" in key
            }
            if hybrid_assertions and all(
                v is True for v in hybrid_assertions.values()
            ):
                suffix = f"  {green('✓')}"

        _p(f"  {mode:<10} {recall_str:<12} {rr_str:<8} {rank_str:<10} {top}{suffix}")


def run_scenario_1() -> dict:
    """
    Scenario 1: Keyword strength.
    Claim: Hybrid preserves exact-match ranking regardless of what semantic does.

    10-page PDF. Page 3 contains the rare token ZXQVP-7821.
    Pages 1-2, 4-10: filler (nature text, no tech/finance vocabulary).
    Query: "ZXQVP-7821"   K=3   Relevant: {3}

    Assertions:
      hybrid rank_first_hit == 1  (keyword contribution via RRF keeps page 3 at top)
      keyword rank_first_hit == 1  (direct BM25 exact match)
      semantic rank: reported as observed data only — no pass/fail
    """
    page_texts = {i: FILLER for i in range(10)}
    # page 3 (0-indexed=2): rare token for exact keyword match
    page_texts[2] = "The project identifier ZXQVP-7821 is the primary key."
    query = "ZXQVP-7821"
    relevant_pages = {3}  # 1-indexed
    k = 3

    pdf_path = _build_pdf(page_texts)
    try:
        result = _run_scenario("Keyword strength", pdf_path, query, relevant_pages, k)
    finally:
        os.unlink(pdf_path)

    kw_rank = result["modes"]["keyword"]["rank_first_hit"]
    hy_rank = result["modes"]["hybrid"]["rank_first_hit"]
    assertions = {
        "hybrid_rank_first_hit_eq_1": hy_rank == 1,
        "keyword_rank_first_hit_eq_1": kw_rank == 1,
    }
    result["assertions"] = assertions

    _section("Scenario 1: Keyword strength")
    _p("  PDF: 10 pages — page 3 has exact token ZXQVP-7821")
    _print_scenario_table(result, assertions)
    sem_rank = result["modes"]["semantic"]["rank_first_hit"]
    sem_rank_str = str(sem_rank) if sem_rank is not None else "∞"
    _p()
    _p(f"  {bold('Verdict')}")
    _row(
        "hybrid rank = 1",
        green("✓") if hy_rank == 1 else red(f"rank {hy_rank}"),
    )
    _row(
        "keyword rank = 1",
        green("✓") if kw_rank == 1 else red(f"rank {kw_rank}"),
    )
    _row("semantic rank (observed)", f"[rank {sem_rank_str}]", None)

    return result


def run_scenario_2() -> dict:
    """
    Scenario 2: Semantic strength.
    Claim: Hybrid preserves conceptual recall when keyword search misses.

    10-page PDF. Page 7: "Sales surged and profit margins expanded dramatically."
    Pages 1-6, 8-10: filler (nature text).
    Query: "revenue growth" (no literal word overlap with page 7)
    K=5   Relevant: {7}

    Assertion: hybrid recall@5 > keyword recall@5  (expected 1.0 > 0.0)
    When fastembed absent: assertion is N/A (both modes fall back to keyword,
    making 0.0 > 0.0 an unfair test — skipped, not failed).
    """
    page_texts = {i: FILLER for i in range(10)}
    # page 7 (0-indexed=6): conceptual match for "revenue growth"
    page_texts[6] = "Sales surged and profit margins expanded dramatically."
    query = "revenue growth"
    relevant_pages = {7}  # 1-indexed
    k = 5

    pdf_path = _build_pdf(page_texts)
    try:
        result = _run_scenario("Semantic strength", pdf_path, query, relevant_pages, k)
    finally:
        os.unlink(pdf_path)

    kw_recall = result["modes"]["keyword"]["recall"]
    hy_recall = result["modes"]["hybrid"]["recall"]

    assertion_result: bool | None = (
        hy_recall > kw_recall if _FASTEMBED_AVAILABLE else None
    )
    assertions = {"hybrid_recall_gt_keyword_recall": assertion_result}
    result["assertions"] = assertions

    _section("Scenario 2: Semantic strength")
    _p("  PDF: 10 pages — page 7 has conceptual match (no literal overlap with query)")
    _print_scenario_table(result, assertions)
    _p()
    _p(f"  {bold('Verdict')}")
    if assertion_result is None:
        _row("hybrid recall > keyword (N/A: fastembed absent)", yellow("N/A"), None)
    else:
        hy_pct = f"{hy_recall * 100:.0f}%"
        kw_pct = f"{kw_recall * 100:.0f}%"
        _row(
            f"hybrid recall ({hy_pct}) > keyword ({kw_pct})",
            green("✓") if assertion_result else red("✗"),
        )

    return result


def run_scenario_3() -> dict:
    """
    Scenario 3: Semantic preservation.
    Claim: When keyword contributes nothing (FTS5 phrase query matches no page),
    hybrid recall is still >= semantic recall. RRF fusion with a dead keyword
    leg does not degrade the result.

    Why keyword fails here: pdf-mcp wraps all queries in FTS5 double-quote
    phrase syntax, requiring ALL query tokens in sequence. No page in this PDF
    contains the full phrase "QXJM-4419 performance degradation" verbatim, so
    keyword returns 0% recall.

    10-page PDF.
    Page 7: "Memory consumption spiked and throughput degraded under sustained load."
            (conceptual match for "performance degradation" part of query)
    Pages 1-6, 8-10: nature filler (FILLER constant).
    Query: "QXJM-4419 performance degradation"
    K=5   Relevant: {7}

    Assertion: hybrid_recall_gte_semantic_recall
    When fastembed absent: N/A (hybrid == keyword without fastembed, making
    hybrid >= semantic trivially True but meaningless).
    """
    page_texts = {i: FILLER for i in range(10)}
    # page 7 (0-indexed=6): conceptual match for "performance degradation"
    page_texts[6] = (
        "Memory consumption spiked and throughput degraded under sustained load."
    )
    query = "QXJM-4419 performance degradation"
    relevant_pages = {7}  # 1-indexed
    k = 5

    pdf_path = _build_pdf(page_texts)
    try:
        result = _run_scenario(
            "Semantic preservation", pdf_path, query, relevant_pages, k
        )
    finally:
        os.unlink(pdf_path)

    sem_recall = result["modes"]["semantic"]["recall"]
    hy_recall = result["modes"]["hybrid"]["recall"]

    assertion_result: bool | None = (
        hy_recall >= sem_recall if _FASTEMBED_AVAILABLE else None
    )
    assertions = {"hybrid_recall_gte_semantic_recall": assertion_result}
    result["assertions"] = assertions

    _section("Scenario 3: Semantic preservation")
    _p(
        "  PDF: 10 pages — page 7 (conceptual match); "
        "keyword finds nothing (phrase query)"
    )
    _print_scenario_table(result, assertions)
    _p()
    _p(f"  {bold('Verdict')}")
    if assertion_result is None:
        _row(
            "hybrid recall >= semantic (N/A: fastembed absent)",
            yellow("N/A"),
            None,
        )
    else:
        hy_pct = f"{hy_recall * 100:.0f}%"
        sem_pct = f"{sem_recall * 100:.0f}%"
        _row(
            f"hybrid recall ({hy_pct}) >= semantic ({sem_pct})",
            green("✓") if assertion_result else red("✗"),
        )

    return result


def run_synthetic_scenarios() -> list[dict]:
    """Run all three synthetic scenarios inside an isolated temp cache."""
    with tempfile.TemporaryDirectory() as tmp:
        original_cache = server_module.cache
        server_module.cache = PDFCache(cache_dir=Path(tmp), ttl_hours=1)
        try:
            results = []
            results.append(run_scenario_1())
            results.append(run_scenario_2())
            results.append(run_scenario_3())
        finally:
            server_module.cache = original_cache
    return results


def _print_summary(all_results: list[dict], file_timestamp: str) -> None:
    """Print the summary table and saved-files notice."""
    _section("Summary")
    _p()
    _p(f"  {'Scenario':<34} {'Assertion':<34} {'Result'}")
    _p(f"  {'─' * 34} {'─' * 34} {'─' * 10}")

    for result in all_results:
        name = result["name"]
        assertions = result.get("assertions", {})
        prefix = f"  {name:<34}"

        first = True
        for key, val in assertions.items():
            label = key.replace("_", " ")
            if val is None:
                val_str = yellow("N/A")
            elif val:
                val_str = green("✓")
            else:
                val_str = red("✗")

            indent = prefix if first else f"  {'':34}"
            _p(f"{indent} {label:<34} {val_str}")
            first = False

        # Scenario 1: also report observed semantic rank
        if result["name"] == "Keyword strength":
            sem_rank = result["modes"]["semantic"]["rank_first_hit"]
            sem_rank_str = str(sem_rank) if sem_rank is not None else "∞"
            _p(f"  {'':34} {'semantic rank (observed)':<34} [rank {sem_rank_str}]")

    _p()
    base = f"rrf_{file_timestamp}"
    _p(f"  Results saved to benchmark_results/{base}.txt")
    _p(f"               benchmark_results/{base}.json")
    _p()


def _save_results(
    all_results: list[dict],
    file_timestamp: str,
    iso_timestamp: str,
) -> None:
    """Save .txt (ANSI-stripped) and .json to benchmark_results/."""
    out_dir = Path("benchmark_results")
    out_dir.mkdir(exist_ok=True)
    base = out_dir / f"rrf_{file_timestamp}"

    # .txt — plain text, same content as terminal (ANSI stripped)
    txt_content = _strip_ansi("\n".join(_OUTPUT))
    base.with_suffix(".txt").write_text(txt_content, encoding="utf-8")

    # .json — structured data
    data = {
        "timestamp": iso_timestamp,
        "fastembed_available": _FASTEMBED_AVAILABLE,
        "scenarios": all_results,
    }
    base.with_suffix(".json").write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )


def run_real_pdf_scenario(pdf_arg: str, query: str, relevant_pages: set[int]) -> dict:
    """
    Run the optional real-PDF section.

    Uses the normal global cache (no isolation) — this is the user's own document.
    pdf_arg may be a local path or a URL; _resolve_path handles both.
    K=10 by default (same as pdf_search default max_results).
    """
    pdf_path = _resolve_path(pdf_arg)
    k = 10

    result = _run_scenario(
        f"Real PDF: {Path(pdf_arg).name}",
        pdf_path,
        query,
        relevant_pages,
        k,
    )

    kw_recall = result["modes"]["keyword"]["recall"]
    sem_recall = result["modes"]["semantic"]["recall"]
    hy_recall = result["modes"]["hybrid"]["recall"]

    assertion_result: bool | None = (
        hy_recall >= max(kw_recall, sem_recall) if _FASTEMBED_AVAILABLE else None
    )
    assertions = {"hybrid_recall_gte_max_kw_sem": assertion_result}
    result["assertions"] = assertions

    _section(f"Real PDF: {Path(pdf_arg).name}")
    _p(f"  Relevant pages: {sorted(relevant_pages)}")
    _print_scenario_table(result, assertions)
    _p()
    _p(f"  {bold('Verdict')}")
    if assertion_result is None:
        _row(
            "hybrid recall >= max(keyword, semantic) (N/A: fastembed absent)",
            yellow("N/A"),
            None,
        )
    else:
        hy_pct = f"{hy_recall * 100:.0f}%"
        kw_pct = f"{kw_recall * 100:.0f}%"
        sem_pct = f"{sem_recall * 100:.0f}%"
        _row(
            f"hybrid ({hy_pct}) >= keyword ({kw_pct}), semantic ({sem_pct})",
            green("✓") if assertion_result else red("✗"),
        )

    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark RRF hybrid search vs keyword-only vs semantic-only."
    )
    parser.add_argument("--pdf", help="Path or URL to a real PDF (optional)")
    parser.add_argument("--query", help="Search query for the real PDF")
    parser.add_argument(
        "--relevant-pages",
        metavar="PAGES",
        help='Comma-separated 1-indexed relevant page numbers, e.g. "1,3,5"',
    )
    args = parser.parse_args()

    now = datetime.now()
    file_ts = now.strftime("%Y%m%d_%H%M%S")
    iso_ts = now.strftime("%Y-%m-%dT%H:%M:%S")

    _p(bold("\npdf-mcp RRF Hybrid Search — Benchmark Report"))
    _p("─" * 68)

    if not _FASTEMBED_AVAILABLE:
        _p(yellow(
            "  Note: fastembed not installed — "
            "semantic and hybrid running in keyword-fallback mode"
        ))

    scenario_results = run_synthetic_scenarios()

    # Optional real PDF section — requires all three flags; skipped otherwise
    real_pdf_result: dict | None = None
    if args.pdf and not args.relevant_pages:
        _p(yellow(
            "  Note: --pdf supplied but --relevant-pages missing"
            " — real PDF section skipped"
        ))
    if args.pdf and args.query and args.relevant_pages:
        try:
            relevant = {int(p.strip()) for p in args.relevant_pages.split(",")}
            real_pdf_result = run_real_pdf_scenario(args.pdf, args.query, relevant)
        except Exception as exc:
            _p(red(f"\n  Real PDF error: {exc}"))

    all_results = scenario_results[:]
    if real_pdf_result is not None:
        all_results.append(real_pdf_result)

    _print_summary(all_results, file_ts)
    _save_results(all_results, file_ts, iso_ts)

    sys.exit(0)


if __name__ == "__main__":
    main()
