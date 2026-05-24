#!/usr/bin/env python
"""
scripts/benchmark_embedding_models.py

Live benchmark: compare 4 fastembed models on the existing ground-truth
corpus and recommend whether to change the default embedding model.

Each of 4 fastembed models is run against the 7 hand-annotated scenarios
in benchmark_data/ground_truth.json. Metrics: per-scenario recall, RR;
aggregate MRR; p50 warm-cache query latency. Decision gate: a challenger
replaces the default iff its MRR is at least baseline + 0.05 AND its p50
latency is at most 1.5x the baseline's. The script does not edit docs;
it prints a copy-pasteable markdown block for docs/embedding-models.md.

Run:
    python scripts/benchmark_embedding_models.py
"""

from __future__ import annotations

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
from pdf_mcp.server import _resolve_path  # noqa: E402
from pdf_mcp.server import pdf_search  # noqa: E402

# ── Models under test ───────────────────────────────────────────────
MODELS = [
    {
        "name": "BAAI/bge-small-en-v1.5",
        "size_mb": 67,
        "dim": 384,
        "license": "MIT",
        "mteb": 51.68,
        "is_baseline": True,
    },
    {
        "name": "snowflake/snowflake-arctic-embed-s",
        "size_mb": 130,
        "dim": 384,
        "license": "Apache 2.0",
        "mteb": 51.98,
        "is_baseline": False,
    },
    {
        "name": "BAAI/bge-base-en-v1.5",
        "size_mb": 210,
        "dim": 768,
        "license": "MIT",
        "mteb": 53.25,
        "is_baseline": False,
    },
    {
        "name": "snowflake/snowflake-arctic-embed-m",
        "size_mb": 430,
        "dim": 768,
        "license": "Apache 2.0",
        "mteb": 54.90,
        "is_baseline": False,
    },
]
BASELINE = next(m["name"] for m in MODELS if m["is_baseline"])

# Decision gate (see spec §4)
MRR_LIFT_THRESHOLD = 0.05
LATENCY_RATIO_THRESHOLD = 1.5


# ── Ground truth loader ─────────────────────────────────────────────
def load_ground_truth(path: str = "benchmark_data/ground_truth.json") -> dict:
    """Load ground truth annotations from JSON. Raises FileNotFoundError if missing."""
    gt_path = Path(path)
    if not gt_path.exists():
        raise FileNotFoundError(f"Ground truth file not found: {path}")
    with open(gt_path, encoding="utf-8") as f:
        return json.load(f)


# ── ANSI / printing helpers (duplicated from benchmark_rrf.py) ──────
_OUTPUT: list[str] = []
_IS_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _IS_TTY else text


def green(t: str) -> str:
    return _c("32", t)


def red(t: str) -> str:
    return _c("31", t)


def yellow(t: str) -> str:
    return _c("33", t)


def cyan(t: str) -> str:
    return _c("36", t)


def bold(t: str) -> str:
    return _c("1", t)


def _p(text: str = "") -> None:
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
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _compute_metrics(matches: list[dict], relevant_pages: set[int], k: int) -> dict:
    """
    Compute recall@K, RR (Reciprocal Rank), and rank-of-first-hit.

    matches: list of {"page": N, ...} from pdf_search (page is 1-indexed)
    relevant_pages: 1-indexed page numbers that are ground-truth relevant
    k: cutoff — only the first k entries in matches are considered
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


def _run_scenario(pdf_path: str, query: str, relevant_pages: set[int], k: int) -> dict:
    """
    Run one scenario in semantic mode and return per-scenario metrics.

    Returns dict with: recall, rr, rank_first_hit, top_pages.
    On pdf_search error, returns zero metrics with empty top_pages.
    """
    result = pdf_search(pdf_path, query, mode="semantic", max_results=k)
    if "error" in result:
        return {"recall": 0.0, "rr": 0.0, "rank_first_hit": None, "top_pages": []}
    matches = result.get("matches", [])
    metrics = _compute_metrics(matches, relevant_pages, k)
    return {**metrics, "top_pages": [m["page"] for m in matches[:k]]}


def run_latency_probe(pdf_path: str, query: str, k: int, n_runs: int = 3) -> float:
    """
    Run pdf_search n_runs times and return the median wall-clock time (ms).

    Caller must ensure the embedding cache is warm before invoking
    (one prior pdf_search call on this PDF is sufficient).
    """
    samples: list[float] = []
    for _ in range(n_runs):
        t0 = time.perf_counter()
        pdf_search(pdf_path, query, mode="semantic", max_results=k)
        samples.append((time.perf_counter() - t0) * 1000)
    samples.sort()
    return samples[len(samples) // 2]


class _ConfigStub:
    """Minimal stand-in for PDFConfig that returns a fixed embedding model.

    Used to swap server_module.pdf_config per-run. Path/URL access checks
    are no-ops because the benchmark only reads public arxiv PDFs that the
    real config already permits.
    """

    def __init__(self, model_name: str) -> None:
        self.embedding_model = model_name

    def check_path(self, path: str) -> None:  # noqa: D401
        pass

    def check_url_host(self, hostname: str) -> None:  # noqa: D401
        pass


def run_model(
    model_name: str,
    gt: dict,
    scenario_k: dict[str, int],
) -> dict:
    """
    Run all scenarios in the ground truth against a single embedding model.

    Side-effects: swaps server_module.pdf_config and server_module.cache
    for the duration of the call; both are restored on exit (even on error).

    Returns:
        {
          "model": str,
          "embed_ms": {pdf_key: float, ...},   # cold-cache first-search time
          "p50_query_ms": float,               # warm-cache median over 3 runs
          "scenarios": [{"id": ..., "recall": ..., ...}, ...],
          "mrr": float,                        # mean RR across all scenarios
        }
    """
    original_config = server_module.pdf_config
    original_cache = server_module.cache
    try:
        server_module.pdf_config = _ConfigStub(model_name)
        with tempfile.TemporaryDirectory() as tmp:
            server_module.cache = PDFCache(cache_dir=Path(tmp), ttl_hours=1)

            # Pre-resolve paths and warm embed cache per PDF (cold-time recorded)
            embed_ms: dict[str, float] = {}
            pdf_paths: dict[str, str] = {}
            first_query: dict[str, tuple[str, int]] = {}
            for pdf_key, pdf in gt["pdfs"].items():
                _path, _err = _resolve_path(pdf["url"])
                if _err is not None:
                    raise RuntimeError(_err["error"])
                pdf_paths[pdf_key] = _path
                first_sid = next(iter(pdf["scenarios"]))
                s = pdf["scenarios"][first_sid]
                k = scenario_k[first_sid]
                first_query[pdf_key] = (s["query"], k)
                t0 = time.perf_counter()
                pdf_search(
                    pdf_paths[pdf_key],
                    s["query"],
                    mode="semantic",
                    max_results=k,
                )
                embed_ms[pdf_key] = (time.perf_counter() - t0) * 1000

            # Run all scenarios
            scenarios = []
            for pdf_key, pdf in gt["pdfs"].items():
                for sid, s in pdf["scenarios"].items():
                    k = scenario_k[sid]
                    metrics = _run_scenario(
                        pdf_paths[pdf_key],
                        s["query"],
                        set(s["relevant_pages"]),
                        k,
                    )
                    scenarios.append(
                        {
                            "id": sid,
                            "pdf": pdf_key,
                            "query": s["query"],
                            "k": k,
                            "relevant_pages": sorted(s["relevant_pages"]),
                            **metrics,
                        }
                    )

            # Latency probe on the first scenario of the first PDF
            first_pdf_key = next(iter(gt["pdfs"]))
            probe_query, probe_k = first_query[first_pdf_key]
            p50 = run_latency_probe(pdf_paths[first_pdf_key], probe_query, probe_k)

            mrr = sum(s["rr"] for s in scenarios) / len(scenarios)
            return {
                "model": model_name,
                "embed_ms": embed_ms,
                "p50_query_ms": p50,
                "scenarios": scenarios,
                "mrr": mrr,
            }
    finally:
        server_module.pdf_config = original_config
        server_module.cache = original_cache


def compute_verdict(
    results: list[dict],
    baseline_name: str,
    mrr_lift_threshold: float = MRR_LIFT_THRESHOLD,
    latency_ratio_threshold: float = LATENCY_RATIO_THRESHOLD,
) -> dict:
    """
    Apply the design doc §4 gate to per-model results and pick a verdict.

    A challenger passes iff:
        challenger.mrr >= baseline.mrr + mrr_lift_threshold AND
        challenger.p50_query_ms <= baseline.p50_query_ms * latency_ratio_threshold

    If multiple challengers pass, pick highest MRR (tiebreak: smaller p50 latency).
    If none pass, keep the default and explain which gate failed in `reason`.

    Returns:
        {
          "default_changed": bool,
          "winner": str | None,
          "reason": str,
          "baseline": str,
          "thresholds": {"mrr_lift": float, "latency_ratio": float},
        }
    """
    baseline = next((r for r in results if r["model"] == baseline_name), None)
    if baseline is None:
        raise ValueError(
            f"Baseline model {baseline_name!r} not in results: "
            f"{[r['model'] for r in results]}"
        )

    challengers = [r for r in results if r["model"] != baseline_name]
    passing = []
    blocked_by_latency = []
    for c in challengers:
        lift = c["mrr"] - baseline["mrr"]
        ratio = c["p50_query_ms"] / max(baseline["p50_query_ms"], 1e-9)
        if lift < mrr_lift_threshold:
            continue
        if ratio > latency_ratio_threshold:
            blocked_by_latency.append((c, ratio))
            continue
        passing.append(c)

    base = {
        "baseline": baseline_name,
        "thresholds": {
            "mrr_lift": mrr_lift_threshold,
            "latency_ratio": latency_ratio_threshold,
        },
    }
    if passing:
        winner = sorted(passing, key=lambda r: (-r["mrr"], r["p50_query_ms"]))[0]
        lift = winner["mrr"] - baseline["mrr"]
        ratio = winner["p50_query_ms"] / max(baseline["p50_query_ms"], 1e-9)
        return {
            **base,
            "default_changed": True,
            "winner": winner["model"],
            "reason": (
                f"{winner['model']} passes both gates "
                f"(MRR +{lift:.3f}, latency {ratio:.2f}x baseline)"
            ),
        }
    if blocked_by_latency:
        c, ratio = blocked_by_latency[0]
        return {
            **base,
            "default_changed": False,
            "winner": None,
            "reason": (
                f"{c['model']} hit MRR gate but failed latency "
                f"({ratio:.2f}x > {latency_ratio_threshold}x threshold)"
            ),
        }
    return {
        **base,
        "default_changed": False,
        "winner": None,
        "reason": "No challenger met the mrr_lift threshold",
    }


def _model_meta(name: str) -> dict:
    """Look up MODELS metadata by name, or return a stub if missing."""
    for m in MODELS:
        if m["name"] == name:
            return m
    return {
        "name": name,
        "size_mb": "?",
        "dim": "?",
        "license": "?",
        "mteb": "?",
        "is_baseline": False,
    }


def print_summary(results: list[dict], verdict: dict) -> None:
    """Print per-model scenario tables, cross-model summary, and verdict."""
    _section("Per-Model Scenario Results")
    for r in results:
        meta = _model_meta(r["model"])
        _p()
        _p(bold(f"  {r['model']}"))
        _row("  Size", f"{meta['size_mb']} MB / {meta['dim']}-dim")
        _row("  MTEB Retrieval", str(meta["mteb"]))
        _row("  License", str(meta["license"]))
        _p()
        _p(
            f"  {'Scenario':<10} {'PDF':<14} {'k':<4} "
            f"{'Recall':<8} {'RR':<8} {'Top-K pages'}"
        )
        _p(f"  {'─' * 9} {'─' * 12} {'─' * 3} " f"{'─' * 6} {'─' * 6} {'─' * 20}")
        for s in r["scenarios"]:
            top = ", ".join(str(p) for p in s["top_pages"]) or "(none)"
            _p(
                f"  {s['id']:<10} {s['pdf']:<14} {s['k']:<4} "
                f"{s['recall'] * 100:.0f}%      {s['rr']:.2f}     {top}"
            )
        _p()
        _row("  MRR", f"{r['mrr']:.3f}")
        _row("  p50 query latency", f"{r['p50_query_ms']:.1f} ms")
        for pdf_key, ms in r["embed_ms"].items():
            _row(f"  embed-all-pages ({pdf_key})", f"{ms:.0f} ms")

    _section("Cross-Model Summary")
    _p()
    _p(f"  {'Model':<42} {'MRR':<8} {'p50':<10} " f"{'Size':<10} {'MTEB'}")
    _p(f"  {'─' * 41} {'─' * 6} {'─' * 8} {'─' * 8} {'─' * 6}")
    for r in results:
        meta = _model_meta(r["model"])
        marker = " (baseline)" if meta.get("is_baseline") else ""
        size_str = f"{meta['size_mb']} MB" if meta["size_mb"] != "?" else "?"
        _p(
            f"  {r['model'] + marker:<42} {r['mrr']:.3f}   "
            f"{r['p50_query_ms']:>5.1f} ms   {size_str:<10} {meta['mteb']}"
        )

    _section("Verdict")
    _p()
    if verdict["default_changed"]:
        _row(
            "Decision",
            green(f"CHANGE default → {verdict['winner']}"),
            ok=True,
        )
    else:
        _row("Decision", yellow("KEEP default unchanged"), ok=None)
    _p(f"  Reason: {verdict['reason']}")
    _p(f"  Baseline: {verdict['baseline']}")
    th = verdict.get(
        "thresholds",
        {"mrr_lift": MRR_LIFT_THRESHOLD, "latency_ratio": LATENCY_RATIO_THRESHOLD},
    )
    _p(
        f"  Gate: MRR lift ≥ {th['mrr_lift']:.2f} AND "
        f"p50 ≤ {th['latency_ratio']}x baseline"
    )

    _section("Copy-pasteable Markdown for docs/embedding-models.md")
    _p()
    _p(format_markdown_table(results, verdict))


def format_markdown_table(results: list[dict], verdict: dict) -> str:
    """Return the Live Benchmark Results section as plain markdown."""
    lines = [
        "## Live Benchmark Results",
        "",
        (
            "Measured on the existing arxiv ground-truth corpus "
            "(Attention paper + GPT-3 paper, 7 hand-annotated scenarios). "
            "MRR aggregated across all 7 scenarios at each scenario's "
            "native k. Latency = p50 query time on a warm embedding cache. "
            "Run via `scripts/benchmark_embedding_models.py`."
        ),
        "",
        "| Model | MRR | p50 latency | Size | MTEB |",
        "|-------|-----|-------------|------|------|",
    ]
    for r in results:
        meta = _model_meta(r["model"])
        marker = " *(baseline)*" if meta.get("is_baseline") else ""
        size_str = f"{meta['size_mb']} MB" if meta["size_mb"] != "?" else "?"
        lines.append(
            f"| `{r['model']}`{marker} | {r['mrr']:.3f} "
            f"| {r['p50_query_ms']:.1f} ms | {size_str} | {meta['mteb']} |"
        )
    lines.append("")
    if verdict["default_changed"]:
        decision = f"changed to `{verdict['winner']}`"
    else:
        decision = "kept (no challenger passed the gate)"
    today = datetime.now().strftime("%Y-%m-%d")
    lines.append(f"**Default decision ({today}):** {decision} — {verdict['reason']}.")
    lines.append("")
    return "\n".join(lines)


def _save_results(
    results: list[dict],
    verdict: dict,
    file_timestamp: str,
    iso_timestamp: str,
) -> None:
    """Write the .txt (ANSI-stripped) and .json reports to benchmark_results/."""
    out_dir = Path("benchmark_results")
    out_dir.mkdir(exist_ok=True)
    base = out_dir / f"embedding_models_{file_timestamp}"

    txt_content = _strip_ansi("\n".join(_OUTPUT))
    base.with_suffix(".txt").write_text(txt_content, encoding="utf-8")

    data = {
        "timestamp": iso_timestamp,
        "baseline": verdict["baseline"],
        "gate": verdict["thresholds"],
        "models": results,
        "verdict": verdict,
    }
    base.with_suffix(".json").write_text(json.dumps(data, indent=2), encoding="utf-8")


SCENARIO_K = {
    "1a": 5,
    "1b": 5,
    "1c": 5,  # Q&A
    "2a": 10,
    "2b": 10,  # Context
    "3a": 3,
    "3b": 3,  # Navigation
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Live benchmark of fastembed models for pdf-mcp default selection."
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

    _p(bold("\npdf-mcp Embedding-Model Live Benchmark"))
    _p("─" * 68)
    _p(f"  Models under test: {len(MODELS)}  " f"(baseline: {BASELINE})")
    _p(
        f"  Gate: MRR lift ≥ {MRR_LIFT_THRESHOLD} "
        f"AND p50 latency ≤ {LATENCY_RATIO_THRESHOLD}x baseline"
    )

    gt = load_ground_truth(args.ground_truth)

    # Build scenario_k by intersecting SCENARIO_K with the loaded ground truth
    seen_sids: set[str] = set()
    for pdf in gt["pdfs"].values():
        seen_sids.update(pdf["scenarios"].keys())
    scenario_k = {sid: SCENARIO_K.get(sid, 5) for sid in seen_sids}

    results = []
    for m in MODELS:
        _section(f"Running model: {m['name']}")
        try:
            r = run_model(m["name"], gt, scenario_k)
            results.append(r)
        except Exception as e:  # network/HF outage on first download
            _p(red(f"  Failed: {e}"))
            results.append(
                {
                    "model": m["name"],
                    "mrr": 0.0,
                    "p50_query_ms": float("inf"),
                    "embed_ms": {},
                    "scenarios": [],
                    "error": str(e),
                }
            )

    verdict = compute_verdict(results, BASELINE)
    print_summary(results, verdict)
    _save_results(results, verdict, file_ts, iso_ts)

    _p()
    _p(f"  Saved: benchmark_results/embedding_models_{file_ts}.txt")
    _p(f"         benchmark_results/embedding_models_{file_ts}.json")


if __name__ == "__main__":
    main()
