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
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pdf_mcp.server as server_module  # noqa: E402
from pdf_mcp.cache import PDFCache  # noqa: E402
from pdf_mcp.server import _resolve_path, pdf_search  # noqa: E402

# Detect fastembed once at import time.
try:
    import fastembed  # type: ignore  # noqa: F401
    _FASTEMBED_AVAILABLE = True
except ImportError:
    _FASTEMBED_AVAILABLE = False


def load_ground_truth(path: str = "benchmark_data/ground_truth.json") -> dict:
    """Load ground truth annotations from JSON. Raises FileNotFoundError if missing."""
    gt_path = Path(path)
    if not gt_path.exists():
        raise FileNotFoundError(
            f"Ground truth file not found: {path}\n"
            "Run Task 1 to create benchmark_data/ground_truth.json"
        )
    with open(gt_path, encoding="utf-8") as f:
        return json.load(f)


_ROUTER_RE = re.compile(r"[A-Z0-9\-]{4,}")


def _router_api_mode(query: str) -> str:
    """Return the api_mode the regex router would choose for this query."""
    return "keyword" if _ROUTER_RE.search(query) else "semantic"


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


def _run_mode_timed(
    pdf_path: str, query: str, api_mode: str, max_results: int
) -> tuple[list[dict], float]:
    """Like _run_mode but also returns elapsed wall-clock time in milliseconds."""
    t0 = time.perf_counter()
    matches = _run_mode(pdf_path, query, api_mode, max_results)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return matches, elapsed_ms


def run_latency_timing(
    pdf_path: str, query: str, k: int, n_runs: int = 3
) -> dict[str, float]:
    """
    Run all four modes n_runs times on a warm cache and return median latency (ms).

    Modes: keyword, semantic, hybrid, router.
    Router latency = latency of the single mode it selects for this query.
    Cache must already be warm before calling this.
    """
    samples: dict[str, list[float]] = {
        "keyword": [], "semantic": [], "hybrid": [], "router": []
    }
    router_api = _router_api_mode(query)

    for _ in range(n_runs):
        for mode, api_mode in [
            ("keyword", "keyword"),
            ("semantic", "semantic"),
            ("hybrid", "auto"),
        ]:
            _, ms = _run_mode_timed(pdf_path, query, api_mode, k)
            samples[mode].append(ms)
        _, ms = _run_mode_timed(pdf_path, query, router_api, k)
        samples["router"].append(ms)

    return {
        mode: sorted(times)[len(times) // 2]
        for mode, times in samples.items()
    }


def _print_latency_table(latency: dict[str, float], label: str) -> None:
    """Print the latency summary for one task group."""
    _p()
    _p(f"  {bold('Latency')} (median over 3 warm-cache runs — {label})")
    for mode in ("keyword", "semantic", "hybrid", "router"):
        ms = latency[mode]
        _p(f"  {mode:<12} {ms:.1f}ms")


def run_k_sensitivity(
    pdf_path: str,
    query: str,
    relevant_pages: set[int],
    k_values: list[int] | None = None,
) -> list[dict]:
    """
    Sweep k values and return per-k scenario results for all modes.

    Returns list of result dicts (one per k), suitable for printing a table.
    """
    if k_values is None:
        k_values = [10, 30, 60, 120]
    return [
        _run_scenario(f"k={k}", pdf_path, query, relevant_pages, k)
        for k in k_values
    ]


def _print_k_sensitivity_table(results: list[dict]) -> None:
    """Print the k-sensitivity sweep table."""
    _p()
    _p(f"  {bold('k-Sensitivity Sweep')} (Scenario 1b — conceptual Q&A)")
    _p()
    _p(
        f"  {'k':<6} {'kw recall':<12} {'sem recall':<12} "
        f"{'hybrid recall':<14} {'router recall'}"
    )
    _p(f"  {'─' * 5}  {'─' * 10}  {'─' * 10}  {'─' * 12}  {'─' * 12}")
    for r in results:
        k = r["k"]
        modes = r["modes"]
        kw = f"{modes['keyword']['recall'] * 100:.0f}%"
        sem = f"{modes['semantic']['recall'] * 100:.0f}%"
        hyb = f"{modes['hybrid']['recall'] * 100:.0f}%"
        rtr = f"{modes['router']['recall'] * 100:.0f}%"
        _p(f"  {k:<6} {kw:<12} {sem:<12} {hyb:<14} {rtr}")


def run_qa_group(gt: dict) -> list[dict]:
    """
    Task Group 1: Q&A — agent issues a query, reads the first hit, answers.
    Primary metric: RR (did agent get the right page fast?). MRR reported across group.

    Scenarios:
      1a — precise factual query (keyword-friendly)
      1b — conceptual query (semantic-friendly)
      1c — mixed query with codes (router misroutes to keyword, hybrid recovers)
    """
    pdf_data = gt["pdfs"]["attention"]
    pdf_path = _resolve_path(pdf_data["url"])
    k = 5

    _section("Task Group 1: Q&A")
    _p(f"  PDF: {pdf_data['title']}")
    _p("  Metric: RR per scenario, MRR across group")
    _p("  Agent behavior: issues query, acts on first hit")

    results = []
    for scenario_id, label in [
        ("1a", "1a — Precise factual"),
        ("1b", "1b — Conceptual"),
        ("1c", "1c — Mixed (router trap)"),
    ]:
        s = pdf_data["scenarios"][scenario_id]
        relevant = set(s["relevant_pages"])
        result = _run_scenario(label, pdf_path, s["query"], relevant, k)

        kw_rr = result["modes"]["keyword"]["rr"]
        sem_rr = result["modes"]["semantic"]["rr"]
        hy_rr = result["modes"]["hybrid"]["rr"]
        router_rr = result["modes"]["router"]["rr"]

        assertion_result: bool | None = (
            hy_rr >= max(kw_rr, sem_rr) if _FASTEMBED_AVAILABLE else None
        )
        result["assertions"] = {"hybrid_rr_gte_best_single": assertion_result}
        results.append(result)

        _p()
        _p(f"  {bold('Scenario ' + label)}")
        _print_scenario_table(result, result["assertions"])
        _p()
        _p(f"  {bold('Verdict')}")
        if assertion_result is None:
            _row(
                "hybrid RR >= best single (N/A: fastembed absent)",
                yellow("N/A"),
                None,
            )
        else:
            _row(
                f"hybrid RR ({hy_rr:.2f}) >= best single ({max(kw_rr, sem_rr):.2f})",
                green("✓") if assertion_result else red("✗"),
            )
        router_sel = result["modes"]["router"]["selected_mode"]
        _p(f"  router RR: {router_rr:.2f}  (routed to {router_sel})")

    # MRR across group
    mrr_hybrid = sum(r["modes"]["hybrid"]["rr"] for r in results) / len(results)
    mrr_keyword = sum(r["modes"]["keyword"]["rr"] for r in results) / len(results)
    mrr_semantic = sum(r["modes"]["semantic"]["rr"] for r in results) / len(results)
    mrr_router = sum(r["modes"]["router"]["rr"] for r in results) / len(results)
    _p()
    _p(f"  {bold('MRR Summary (Task Group 1)')}")
    _row("keyword MRR", f"{mrr_keyword:.2f}")
    _row("semantic MRR", f"{mrr_semantic:.2f}")
    _row("hybrid MRR", f"{mrr_hybrid:.2f}")
    _row("router MRR", f"{mrr_router:.2f}")

    # Latency (use 1b — representative conceptual query)
    s1b = pdf_data["scenarios"]["1b"]
    latency = run_latency_timing(pdf_path, s1b["query"], k=k)
    _print_latency_table(latency, "Task Group 1: Q&A")

    # k-sensitivity on 1b
    _p()
    _p(f"  {bold('k-Sensitivity (Scenario 1b — conceptual query)')}")
    k_results = run_k_sensitivity(
        pdf_path, s1b["query"], set(pdf_data["scenarios"]["1b"]["relevant_pages"])
    )
    _print_k_sensitivity_table(k_results)

    # Store group MRR in first result for summary table
    results[0]["group_mrr"] = {
        "hybrid": mrr_hybrid,
        "keyword": mrr_keyword,
        "semantic": mrr_semantic,
        "router": mrr_router,
    }

    return results


def run_context_group(gt: dict) -> list[dict]:
    """
    Task Group 2: Context Building — agent gathers ALL relevant pages on a topic.
    Primary metric: Recall@K (agent needs completeness, not just first hit).

    Scenarios:
      2a — single topic, relevant pages clustered (keyword sufficiency baseline)
      2b — broad theme, relevant pages scattered (TRUE FUSION scenario)
    """
    pdf_data = gt["pdfs"]["gpt3"]
    pdf_path = _resolve_path(pdf_data["url"])
    k = 10

    _section("Task Group 2: Context Building")
    _p(f"  PDF: {pdf_data['title']}")
    _p("  Metric: Recall@K")
    _p("  Agent behavior: issues query, reads all K results, synthesizes")

    results = []
    for scenario_id, label in [
        ("2a", "2a — Clustered pages"),
        ("2b", "2b — Scattered pages (true fusion)"),
    ]:
        s = pdf_data["scenarios"][scenario_id]
        relevant = set(s["relevant_pages"])
        result = _run_scenario(label, pdf_path, s["query"], relevant, k)

        kw_recall = result["modes"]["keyword"]["recall"]
        sem_recall = result["modes"]["semantic"]["recall"]
        hy_recall = result["modes"]["hybrid"]["recall"]
        router_recall = result["modes"]["router"]["recall"]

        assertion_result: bool | None = (
            hy_recall >= max(kw_recall, sem_recall) if _FASTEMBED_AVAILABLE else None
        )
        result["assertions"] = {"hybrid_recall_gte_best_single": assertion_result}
        results.append(result)

        _p()
        _p(f"  {bold('Scenario ' + label)}")
        _p(f"  Relevant pages: {sorted(relevant)}")
        _print_scenario_table(result, result["assertions"])
        _p()
        _p(f"  {bold('Verdict')}")
        if assertion_result is None:
            _row(
                "hybrid recall >= best single (N/A: fastembed absent)",
                yellow("N/A"),
                None,
            )
        else:
            hy_pct = f"{hy_recall * 100:.0f}%"
            best_pct = f"{max(kw_recall, sem_recall) * 100:.0f}%"
            _row(
                f"hybrid recall ({hy_pct}) >= best single ({best_pct})",
                green("✓") if assertion_result else red("✗"),
            )
        router_sel = result["modes"]["router"]["selected_mode"]
        _p(
            f"  router recall: {router_recall * 100:.0f}%"
            f"  (routed to {router_sel})"
        )

    # Latency on 2b (scattered query — more representative)
    s2b = pdf_data["scenarios"]["2b"]
    latency = run_latency_timing(pdf_path, s2b["query"], k=k)
    _print_latency_table(latency, "Task Group 2: Context Building")

    return results


def run_navigation_group(gt: dict) -> list[dict]:
    """
    Task Group 3: Navigation — agent follows a reference to a specific location.
    Primary metric: Recall@1 (exact page), secondary: RR.

    Scenarios:
      3a — exact section heading (keyword wins; academic headings don't trigger
            the router regex, so router picks semantic — which also finds it)
      3b — cross-reference by concept (semantic needed; keyword finds nothing)
    """
    pdf_data = gt["pdfs"]["attention"]
    pdf_path = _resolve_path(pdf_data["url"])
    k = 3  # Navigation: agent expects to land on the right page fast

    _section("Task Group 3: Navigation")
    _p(f"  PDF: {pdf_data['title']}")
    _p("  Metric: Recall@1 and RR")
    _p("  Agent behavior: follows a reference, needs exact location")

    results = []
    for scenario_id, label in [
        ("3a", "3a — Exact section heading"),
        ("3b", "3b — Cross-reference by concept"),
    ]:
        s = pdf_data["scenarios"][scenario_id]
        relevant = set(s["relevant_pages"])
        result = _run_scenario(label, pdf_path, s["query"], relevant, k)

        hy_rank = result["modes"]["hybrid"]["rank_first_hit"]
        kw_rank = result["modes"]["keyword"]["rank_first_hit"]
        router_rank = result["modes"]["router"]["rank_first_hit"]
        hy_recall = result["modes"]["hybrid"]["recall"]
        kw_recall = result["modes"]["keyword"]["recall"]

        hy_r1 = 1.0 if hy_rank == 1 else 0.0
        kw_r1 = 1.0 if kw_rank == 1 else 0.0

        # 3a: compare Recall@1 (exact navigation hit)
        # 3b: compare Recall@K (keyword misses entirely; hybrid finds via semantic)
        if scenario_id == "3a":
            assertion_result: bool | None = (
                hy_r1 >= kw_r1 if _FASTEMBED_AVAILABLE else None
            )
            assertion_key = "hybrid_recall_at_1_gte_keyword"
        else:
            assertion_result = (
                hy_recall > kw_recall if _FASTEMBED_AVAILABLE else None
            )
            assertion_key = "hybrid_recall_gt_keyword"
        result["assertions"] = {assertion_key: assertion_result}
        results.append(result)

        _p()
        _p(f"  {bold('Scenario ' + label)}")
        _print_scenario_table(result, result["assertions"])
        _p()
        _p(f"  {bold('Verdict')}")
        if assertion_result is None:
            metric_label = (
                "hybrid Recall@1 >= keyword"
                if scenario_id == "3a"
                else "hybrid recall > keyword"
            )
            _row(f"{metric_label} (N/A: fastembed absent)", yellow("N/A"), None)
        elif scenario_id == "3a":
            _row(
                f"hybrid Recall@1 ({hy_r1:.0f}) >= keyword ({kw_r1:.0f})",
                green("✓") if assertion_result else red("✗"),
            )
        else:
            hy_pct = f"{hy_recall * 100:.0f}%"
            kw_pct = f"{kw_recall * 100:.0f}%"
            _row(
                f"hybrid recall ({hy_pct}) > keyword ({kw_pct})",
                green("✓") if assertion_result else red("✗"),
            )
        router_sel = result["modes"]["router"]["selected_mode"]
        router_r1 = 1.0 if router_rank == 1 else 0.0
        _p(
            f"  router Recall@1: {router_r1:.0f}"
            f"  (routed to {router_sel})"
        )

    # Latency on 3a (section heading — short, representative)
    s3a = pdf_data["scenarios"]["3a"]
    latency = run_latency_timing(pdf_path, s3a["query"], k=k)
    _print_latency_table(latency, "Task Group 3: Navigation")

    return results


def _run_scenario(
    name: str,
    pdf_path: str,
    query: str,
    relevant_pages: set[int],
    k: int,
) -> dict:
    """
    Run keyword, semantic, hybrid, and router search on pdf_path and return metrics.

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

    # Router: pick one mode via regex, run it, record which mode was selected
    router_api = _router_api_mode(query)
    router_matches = _run_mode(pdf_path, query, router_api, max_results=k)
    router_metrics = _compute_metrics(router_matches, relevant_pages, k)
    mode_data["router"] = {
        "recall": router_metrics["recall"],
        "rr": router_metrics["rr"],
        "rank_first_hit": router_metrics["rank_first_hit"],
        "top_pages": [m["page"] for m in router_matches[:k]],
        "selected_mode": router_api,
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
    Print the mode comparison table for one scenario, including router column.

    result: dict from _run_scenario
    assertions: {key: bool | None}  — None means N/A (fastembed absent)
    """
    k = result["k"]
    n_relevant = len(result["relevant_pages"])

    _p()
    _p(f"  Query: {bold(repr(result['query']))}   K={k}")
    _p()
    top_col = "Top-" + str(k) + " pages"
    _p(
        f"  {'Mode':<14} {'Recall@' + str(k):<12} {'RR':<8} "
        f"{'Rank-1st':<10} {top_col}"
    )
    _p(f"  {'─' * 13}  {'─' * 10}  {'─' * 6}  {'─' * 8}  {'─' * 20}")

    for mode in ("keyword", "semantic", "hybrid", "router"):
        d = result["modes"][mode]
        recall = d["recall"]
        rr = d["rr"]
        rank = d["rank_first_hit"]
        top = ", ".join(str(p) for p in d["top_pages"]) or "(none)"

        hits = int(round(recall * n_relevant))
        recall_str = f"{hits}/{n_relevant}  {recall * 100:.0f}%"
        rr_str = f"{rr:.2f}"
        rank_str = f"rank {rank}" if rank is not None else "∞"

        # Show which mode the router selected, in parentheses
        mode_label = mode
        if mode == "router":
            mode_label = f"router({d.get('selected_mode', '?')[:3]})"

        suffix = ""
        if mode == "hybrid":
            hybrid_assertions = {
                key: val for key, val in assertions.items() if "hybrid" in key
            }
            if hybrid_assertions and all(
                v is True for v in hybrid_assertions.values()
            ):
                suffix = f"  {green('✓')}"

        _p(
            f"  {mode_label:<14} {recall_str:<12} {rr_str:<8} "
            f"{rank_str:<10} {top}{suffix}"
        )


def _print_summary(
    qa_results: list[dict],
    context_results: list[dict],
    nav_results: list[dict],
    file_timestamp: str,
) -> None:
    """Print the summary table across all three task groups."""
    _section("Summary")
    _p()

    # Q&A group — MRR per mode
    _p(f"  {bold('Task Group 1: Q&A')}  (primary metric: MRR)")
    mrr = qa_results[0].get("group_mrr", {})
    for mode in ("keyword", "semantic", "hybrid", "router"):
        val = mrr.get(mode)
        label = f"  MRR {mode}"
        _p(f"  {label:<20} {val:.2f}" if val is not None else f"  {label:<20} N/A")

    _p()

    # Context building — Recall@K per scenario and mode
    _p(f"  {bold('Task Group 2: Context Building')}  (primary metric: Recall@K)")
    _p(f"  {'Scenario':<36} {'kw':<8} {'sem':<8} {'hybrid':<8} {'router'}")
    for r in context_results:
        modes = r["modes"]
        row = f"  {r['name']:<36}"
        for mode in ("keyword", "semantic", "hybrid", "router"):
            pct = f"{modes[mode]['recall'] * 100:.0f}%"
            row += f" {pct:<8}"
        _p(row)

    _p()

    # Navigation — Recall@1 per scenario and mode
    # Note: ANSI codes inflate string length, so pad the raw symbol first then colorize.
    _p(f"  {bold('Task Group 3: Navigation')}  (primary metric: Recall@1)")
    _p(f"  {'Scenario':<36} {'kw R@1':<8} {'sem R@1':<8} {'hyb R@1':<8} {'rtr R@1'}")
    for r in nav_results:
        modes = r["modes"]
        row = f"  {r['name']:<36}"
        for mode in ("keyword", "semantic", "hybrid", "router"):
            rank = modes[mode]["rank_first_hit"]
            symbol = "✓" if rank == 1 else "✗"
            colored = green("✓") if rank == 1 else "✗"
            row += " " + colored + " " * (7 - len(symbol))
        _p(row)

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


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark RRF hybrid search vs keyword-only, semantic-only, and router. "
            "Runs 3 agentic task groups (Q&A, Context Building, Navigation) on real "
            "public PDFs using ground truth from benchmark_data/ground_truth.json."
        )
    )
    parser.add_argument(
        "--ground-truth",
        default="benchmark_data/ground_truth.json",
        help="Path to ground truth JSON (default: benchmark_data/ground_truth.json)",
    )
    args = parser.parse_args()

    now = datetime.now()
    file_ts = now.strftime("%Y%m%d_%H%M%S")
    iso_ts = now.strftime("%Y-%m-%dT%H:%M:%S")

    _p(bold("\npdf-mcp RRF Hybrid Search — Agentic Benchmark Report"))
    _p("─" * 68)

    if not _FASTEMBED_AVAILABLE:
        _p(yellow(
            "  Note: fastembed not installed — "
            "semantic and hybrid running in keyword-fallback mode"
        ))

    gt = load_ground_truth(args.ground_truth)

    with tempfile.TemporaryDirectory() as tmp:
        original_cache = server_module.cache
        server_module.cache = PDFCache(cache_dir=Path(tmp), ttl_hours=1)
        try:
            qa_results = run_qa_group(gt)
            context_results = run_context_group(gt)
            nav_results = run_navigation_group(gt)
        finally:
            server_module.cache = original_cache

    all_results = qa_results + context_results + nav_results

    _print_summary(qa_results, context_results, nav_results, file_ts)
    _save_results(all_results, file_ts, iso_ts)

    sys.exit(0)


if __name__ == "__main__":
    main()
