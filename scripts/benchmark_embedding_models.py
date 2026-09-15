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
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pdf_mcp.server as server_module  # noqa: E402
from bench_env import environment  # noqa: E402
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


def _run_scenario(
    pdf_path: str,
    query: str,
    relevant_pages: set[int],
    k: int,
    mode: str = "semantic",
) -> dict:
    """
    Run one scenario in the given mode and return per-scenario metrics.

    Returns dict with: recall, rr, rank_first_hit, top_pages.
    On pdf_search error, returns zero metrics with empty top_pages.
    """
    result = pdf_search(pdf_path, query, mode=mode, max_results=k)
    if "error" in result:
        return {"recall": 0.0, "rr": 0.0, "rank_first_hit": None, "top_pages": []}
    matches = result.get("matches", [])
    metrics = _compute_metrics(matches, relevant_pages, k)
    return {**metrics, "top_pages": [m["page"] for m in matches[:k]]}


def run_latency_probe(
    pdf_path: str, query: str, k: int, n_runs: int = 3, mode: str = "semantic"
) -> float:
    """
    Run pdf_search n_runs times and return the median wall-clock time (ms).

    Caller must ensure the embedding cache is warm before invoking
    (one prior pdf_search call on this PDF is sufficient).
    """
    samples: list[float] = []
    for _ in range(n_runs):
        t0 = time.perf_counter()
        pdf_search(pdf_path, query, mode=mode, max_results=k)
        samples.append((time.perf_counter() - t0) * 1000)
    samples.sort()
    return samples[len(samples) // 2]


class _ConfigStub:
    """Minimal stand-in for PDFConfig that returns a fixed embedding model.

    Used to swap server_module.pdf_config per-run. Path/URL access checks
    are no-ops because the benchmark only reads public arxiv PDFs that the
    real config already permits.

    The stub has to carry every attribute server.py reads off `pdf_config`
    on the pdf_search path. A missing one raises inside pdf_search, which
    returns it as an `{"error": ...}` dict rather than propagating -- so the
    benchmark used to score such a model 0.0 and report it as a real,
    successful measurement. `run_model` now checks the warm-up search's
    result (see below) so that failure mode is loud, and
    `confidence_threshold` mirrors server.py's own default so the
    semantic/hybrid confidence annotation behaves as it does in production.
    """

    def __init__(self, model_name: str) -> None:
        self.embedding_model = model_name
        self.confidence_threshold = server_module._SEMANTIC_CONFIDENCE_THRESHOLD

    def check_path(self, path: str) -> None:  # noqa: D401
        pass

    def check_url_host(self, hostname: str) -> None:  # noqa: D401
        pass


def run_model(
    model_name: str,
    gt: dict,
    scenario_k: dict[str, int],
    mode: str = "semantic",
    score_pages: str = "relevant",
) -> dict:
    """
    Run all scenarios in the ground truth against a single embedding model.

    Side-effects: swaps server_module.pdf_config and server_module.cache
    for the duration of the call; both are restored on exit (even on error).

    score_pages: which ground-truth field to score against.
        "relevant" (default) -- scenario["relevant_pages"], unchanged
            behaviour for every existing corpus.
        "target" -- scenario["target_pages"] when present, falling back to
            "relevant_pages" for scenarios that don't distinguish the two
            (e.g. keyword_control). Some ground truths (semantic_xref) also
            carry a "referrer_page" inside "relevant_pages" -- scoring
            "relevant" there rewards finding the sentence the query was
            lifted from as much as finding the actual answer; "target"
            scores the answer only. See benchmark_data/german_embedding_
            results.md for why target is the headline for that arm.

    Returns:
        {
          "model": str,
          "mode": str,
          "score_pages": str,
          "embed_ms": {pdf_key: float, ...},   # cold-cache first-search time
          "p50_query_ms": float,               # warm-cache median over 3 runs
          "scenarios": [{"id": ..., "recall": ..., ...}, ...],
          "mrr": float,                        # mean RR across all scenarios
        }
    """
    if score_pages not in ("relevant", "target"):
        raise ValueError(
            f"score_pages must be 'relevant' or 'target', got {score_pages!r}"
        )
    original_config = server_module.pdf_config
    original_cache = server_module.cache
    try:
        server_module.pdf_config = _ConfigStub(model_name)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            server_module.cache = PDFCache(cache_dir=Path(tmp), ttl_hours=1)

            # Pre-resolve paths and warm embed cache per PDF (cold-time recorded).
            # A PDF with no scenarios yet (e.g. ground_truth.json entries
            # reserved for a corpus not wired into this harness) is skipped
            # here -- there's no query to warm with -- but its path is still
            # resolved so any scenarios elsewhere referencing it still work.
            embed_ms: dict[str, float] = {}
            pdf_paths: dict[str, str] = {}
            first_query: dict[str, tuple[str, int]] = {}
            for pdf_key, pdf in gt["pdfs"].items():
                _path, _err = _resolve_path(pdf["url"])
                if _err is not None:
                    raise RuntimeError(_err["error"])
                pdf_paths[pdf_key] = _path
                if not pdf["scenarios"]:
                    continue
                first_sid = next(iter(pdf["scenarios"]))
                s = pdf["scenarios"][first_sid]
                k = scenario_k[first_sid]
                first_query[pdf_key] = (s["query"], k)
                t0 = time.perf_counter()
                warm = pdf_search(
                    pdf_paths[pdf_key],
                    s["query"],
                    mode=mode,
                    max_results=k,
                )
                # pdf_search reports failures as a result dict, not an
                # exception. Left unchecked, a model that cannot search at all
                # (bad model name, unreadable PDF, a pdf_config attribute this
                # stub is missing) would score 0.0 on every scenario and be
                # reported as a genuine measurement. Fail the model instead.
                if "error" in warm:
                    raise RuntimeError(
                        f"warm-up search on {pdf_key} failed: {warm['error']}"
                    )
                embed_ms[pdf_key] = (time.perf_counter() - t0) * 1000

            # Run all scenarios
            scenarios = []
            for pdf_key, pdf in gt["pdfs"].items():
                for sid, s in pdf["scenarios"].items():
                    k = scenario_k[sid]
                    scored = s.get("target_pages") if score_pages == "target" else None
                    if scored is None:
                        scored = s["relevant_pages"]
                    metrics = _run_scenario(
                        pdf_paths[pdf_key],
                        s["query"],
                        set(scored),
                        k,
                        mode=mode,
                    )
                    scenarios.append(
                        {
                            "id": sid,
                            "pdf": pdf_key,
                            "query": s["query"],
                            "k": k,
                            "relevant_pages": sorted(s["relevant_pages"]),
                            "scored_pages": sorted(scored),
                            **metrics,
                        }
                    )

            # Latency probe on the first scenario of the first PDF that
            # actually has one (skips past any scenario-less entries).
            if not first_query:
                raise ValueError(
                    "Ground truth has no scenarios at all (every PDF entry "
                    "has an empty 'scenarios' dict) -- nothing to benchmark."
                )
            first_pdf_key = next(iter(first_query))
            probe_query, probe_k = first_query[first_pdf_key]
            p50 = run_latency_probe(
                pdf_paths[first_pdf_key], probe_query, probe_k, mode=mode
            )

            mrr = sum(s["rr"] for s in scenarios) / len(scenarios)
            return {
                "model": model_name,
                "mode": mode,
                "score_pages": score_pages,
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


def compute_ci_vs_baseline(results: list[dict], baseline_name: str) -> dict[str, dict]:
    """Paired bootstrap 95% CI of (challenger MRR - baseline MRR) per model.

    Pairs scenarios by "id" so the comparison is over the same queries even
    if a model's `scenarios` list isn't in the same order. A model missing
    a scenario the baseline has (or vice versa) drops that id from the
    pairing rather than erroring -- keeps this usable on partial runs.

    Returns {model_name: {"mean_diff", "lo", "hi", "includes_zero", "n"}},
    one entry per non-baseline model in `results`.
    """
    from benchmark_bedrock_kb import bootstrap_diff_ci

    baseline = next((r for r in results if r["model"] == baseline_name), None)
    if baseline is None:
        return {}
    baseline_rr = {s["id"]: s["rr"] for s in baseline.get("scenarios", [])}

    out: dict[str, dict] = {}
    for r in results:
        if r["model"] == baseline_name:
            continue
        challenger_rr = {s["id"]: s["rr"] for s in r.get("scenarios", [])}
        shared_ids = [sid for sid in baseline_rr if sid in challenger_rr]
        if not shared_ids:
            continue
        a = [challenger_rr[sid] for sid in shared_ids]
        b = [baseline_rr[sid] for sid in shared_ids]
        out[r["model"]] = bootstrap_diff_ci(a, b)
    return out


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
    mode: str = "semantic",
    score_pages: str = "relevant",
    models: list[str] | None = None,
    ground_truth: str = "benchmark_data/ground_truth.json",
) -> None:
    """Write the .txt (ANSI-stripped) and .json reports to benchmark_results/."""
    out_dir = Path("benchmark_results")
    out_dir.mkdir(exist_ok=True)
    base = out_dir / f"embedding_models_{file_timestamp}"

    txt_content = _strip_ansi("\n".join(_OUTPUT))
    base.with_suffix(".txt").write_text(txt_content, encoding="utf-8")

    data = {
        "timestamp": iso_timestamp,
        "mode": mode,
        "score_pages": score_pages,
        "models_run": models or [r["model"] for r in results],
        "ground_truth": ground_truth,
        "environment": environment(),
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


def _filter_ground_truth_by_arm(gt: dict, arms: list[str] | None) -> dict:
    """Return a copy of gt with only scenarios whose 'arm' key is in arms.

    Scenarios without an 'arm' key are kept (the original arxiv corpus has
    none). No-op when arms is None.
    """
    if not arms:
        return gt
    arm_set = set(arms)
    filtered: dict = {"pdfs": {}}
    for pdf_key, pdf in gt["pdfs"].items():
        scenarios = {
            sid: s
            for sid, s in pdf["scenarios"].items()
            if "arm" not in s or s["arm"] in arm_set
        }
        if scenarios:
            filtered["pdfs"][pdf_key] = {**pdf, "scenarios": scenarios}
    return filtered


def _patch_onnx_graph_optimization_level() -> None:
    """Downgrade ORT_ENABLE_ALL to ORT_ENABLE_EXTENDED, process-wide.

    fastembed's OnnxModel._load_onnx_model hardcodes
    ort.GraphOptimizationLevel.ORT_ENABLE_ALL with no way to override it
    per model. Some models' exported ONNX graphs fail a specific fusion
    pass only at that level (verified for jina-embeddings-v2-base-de:
    ORT_DISABLE_ALL / ORT_ENABLE_BASIC / ORT_ENABLE_EXTENDED all load and
    embed correctly; only ORT_ENABLE_ALL raises
    "SimplifiedLayerNormFusion ... itr != node_args.end()"). Monkeypatching
    onnxruntime.InferenceSession.__init__ to downgrade just that one
    level is the smallest intervention that doesn't touch fastembed's
    installed package. Only affects this benchmark process (opt-in via
    --patch-onnx-graph-opt), never pdf-mcp's own embedder.py.
    """
    import onnxruntime as ort

    orig_init = ort.InferenceSession.__init__

    def patched_init(
        self: Any, path_or_bytes: Any, sess_options: Any = None, **kw: Any
    ) -> None:
        if (
            sess_options is not None
            and sess_options.graph_optimization_level
            == ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        ):
            sess_options.graph_optimization_level = (
                ort.GraphOptimizationLevel.ORT_ENABLE_EXTENDED
            )
        orig_init(self, path_or_bytes, sess_options=sess_options, **kw)

    ort.InferenceSession.__init__ = patched_init  # type: ignore[method-assign]


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
    parser.add_argument(
        "--models",
        default=None,
        help=(
            "Comma-separated fastembed model names to run "
            "(default: the built-in MODELS list)"
        ),
    )
    parser.add_argument(
        "--baseline",
        default=None,
        help="Model name to use as the verdict baseline (default: MODELS' baseline)",
    )
    parser.add_argument(
        "--mode",
        default="semantic",
        choices=["semantic", "keyword", "auto"],
        help="pdf_search mode to run every scenario in (default: semantic)",
    )
    parser.add_argument(
        "--score-pages",
        default="relevant",
        choices=["relevant", "target"],
        help=(
            "Which ground-truth field to score against: 'relevant' (default, "
            "unchanged behaviour) or 'target' -- scores semantic_xref-style "
            "scenarios against scenario['target_pages'] instead of "
            "['relevant_pages'], so finding the referrer page alone no "
            "longer counts as a hit. Scenarios without 'target_pages' fall "
            "back to 'relevant_pages' unchanged."
        ),
    )
    parser.add_argument(
        "--arms",
        default=None,
        help=(
            "Comma-separated 'arm' values to include (scenarios without an "
            "'arm' key always run). Default: all scenarios."
        ),
    )
    parser.add_argument(
        "--patch-onnx-graph-opt",
        action="store_true",
        help=(
            "Downgrade onnxruntime's graph optimization level from its "
            "fastembed-hardcoded ORT_ENABLE_ALL to ORT_ENABLE_EXTENDED "
            "for models whose exported ONNX graph a specific fusion pass "
            "chokes on (e.g. jinaai/jina-embeddings-v2-base-de -- "
            "SimplifiedLayerNormFusion fails to find a renamed node under "
            "onnxruntime 1.24.4, but the model loads and embeds correctly "
            "one optimization level down). No effect on models that "
            "already load under ORT_ENABLE_ALL."
        ),
    )
    args = parser.parse_args()

    # Resolve compute_ci_vs_baseline's import before any model is embedded:
    # a broken import here should fail in under a second, not after a
    # multi-hour run with nothing written to benchmark_results/.
    import benchmark_bedrock_kb  # noqa: F401

    if args.patch_onnx_graph_opt:
        _patch_onnx_graph_optimization_level()

    now = datetime.now()
    file_ts = now.strftime("%Y%m%d_%H%M%S")
    iso_ts = now.strftime("%Y-%m-%dT%H:%M:%S")

    model_names = [m.strip() for m in args.models.split(",")] if args.models else None
    models_under_test = (
        [_model_meta(name) for name in model_names] if model_names else MODELS
    )
    baseline_name = args.baseline or BASELINE
    arms = [a.strip() for a in args.arms.split(",")] if args.arms else None

    model_names_under_test = [m["name"] for m in models_under_test]
    if baseline_name not in model_names_under_test:
        parser.error(
            f"baseline {baseline_name!r} is not among the models being run "
            f"({', '.join(model_names_under_test)}). Pass --baseline "
            "explicitly when using --models with a non-default baseline."
        )

    _p(bold("\npdf-mcp Embedding-Model Live Benchmark"))
    _p("─" * 68)
    _p(
        f"  Models under test: {len(models_under_test)}  "
        f"(baseline: {baseline_name})"
    )
    _p(
        f"  Mode: {args.mode}  Score pages: {args.score_pages}"
        + (f"  Arms: {', '.join(arms)}" if arms else "")
    )
    _p(
        f"  Gate: MRR lift ≥ {MRR_LIFT_THRESHOLD} "
        f"AND p50 latency ≤ {LATENCY_RATIO_THRESHOLD}x baseline"
    )

    gt = load_ground_truth(args.ground_truth)
    gt = _filter_ground_truth_by_arm(gt, arms)
    total_scenarios = sum(len(pdf["scenarios"]) for pdf in gt["pdfs"].values())
    if total_scenarios == 0:
        parser.error(
            f"--arms {args.arms!r} matched zero scenarios in "
            f"{args.ground_truth!r} -- check the arm name(s) against the "
            "ground truth's 'arm' values (nothing to benchmark)."
        )

    # Build scenario_k: prefer each scenario's own "k", else SCENARIO_K, else 5
    scenario_k: dict[str, int] = {}
    for pdf in gt["pdfs"].values():
        for sid, s in pdf["scenarios"].items():
            scenario_k[sid] = s.get("k", SCENARIO_K.get(sid, 5))

    results = []
    for m in models_under_test:
        _section(f"Running model: {m['name']}")
        try:
            r = run_model(
                m["name"], gt, scenario_k, mode=args.mode, score_pages=args.score_pages
            )
            results.append(r)
        except Exception as e:  # network/HF outage on first download
            _p(red(f"  Failed: {e}"))
            results.append(
                {
                    "model": m["name"],
                    "mode": args.mode,
                    "score_pages": args.score_pages,
                    "mrr": 0.0,
                    "p50_query_ms": float("inf"),
                    "embed_ms": {},
                    "scenarios": [],
                    "error": str(e),
                }
            )

    verdict = compute_verdict(results, baseline_name)
    print_summary(results, verdict)
    ci = compute_ci_vs_baseline(results, baseline_name)
    if ci:
        _section(f"95% CI of MRR lift vs baseline ({baseline_name})")
        for name, c in ci.items():
            flag = "excludes zero" if not c["includes_zero"] else "includes zero"
            _p(
                f"  {name}: {c['mean_diff']:+.3f} "
                f"[{c['lo']:+.3f}, {c['hi']:+.3f}] ({flag}, n={c['n']})"
            )
    verdict["ci_vs_baseline"] = ci
    _save_results(
        results,
        verdict,
        file_ts,
        iso_ts,
        mode=args.mode,
        score_pages=args.score_pages,
        models=[m["name"] for m in models_under_test],
        ground_truth=args.ground_truth,
    )

    _p()
    _p(f"  Saved: benchmark_results/embedding_models_{file_ts}.txt")
    _p(f"         benchmark_results/embedding_models_{file_ts}.json")


if __name__ == "__main__":
    main()
