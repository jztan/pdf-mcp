#!/usr/bin/env python
"""
scripts/benchmark_e5_prefix.py

Benchmark-first evaluation of an E5 query:/passage: prefix change, BEFORE
adopting it in production code.

The intfloat E5 family is trained with asymmetric instruction prefixes:
passages must be embedded as "passage: <text>" and queries as "query: <text>".
fastembed's .embed() does NOT apply these. This script measures the retrieval
gap on the existing ground-truth corpus by running the SAME E5 model two ways:

    * no-prefix : current production behaviour (texts embedded verbatim)
    * prefix    : with passage:/query: prefixes applied

It does this by monkeypatching pdf_mcp.embedder.encode / encode_query for the
"prefix" run, so NO production code is touched. Each run uses a fresh cache
(separate tempdir), so page embeddings never leak across conditions.

Metric: per-scenario recall@k and RR; aggregate MRR; MRR delta. The decision
gate mirrors the project quality loop — adopt the prefix change iff it lifts
MRR by at least MRR_LIFT_THRESHOLD without a latency regression (prefixes are
~2 tokens, so latency is informational only).

Run (downloads intfloat/multilingual-e5-large ~2GB on first use):
    python scripts/benchmark_e5_prefix.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pdf_mcp.embedder as embedder  # noqa: E402

from benchmark_embedding_models import (  # noqa: E402
    SCENARIO_K,
    bold,
    cyan,
    green,
    load_ground_truth,
    red,
    run_model,
    yellow,
)

# Only E5 model fastembed currently exposes.
E5_MODEL = "intfloat/multilingual-e5-large"

_E5_QUERY_PREFIX = "query: "
_E5_PASSAGE_PREFIX = "passage: "

# Adopt the change iff it lifts MRR by at least this much.
MRR_LIFT_THRESHOLD = 0.05


def _patched_encode(texts: list[str], model_name: str):
    """encode() with the E5 passage: prefix applied (prefix variant)."""
    return _ORIG_ENCODE([_E5_PASSAGE_PREFIX + t for t in texts], model_name)


def _patched_encode_query(text: str, model_name: str):
    """encode_query() with the E5 query: prefix applied (prefix variant)."""
    return _ORIG_ENCODE([_E5_QUERY_PREFIX + text], model_name)[0]


_ORIG_ENCODE = embedder.encode
_ORIG_ENCODE_QUERY = embedder.encode_query


def run_condition(label: str, gt: dict, scenario_k: dict, *, prefix: bool) -> dict:
    """Run the full corpus under one condition; restore patches on exit."""
    if prefix:
        embedder.encode = _patched_encode
        embedder.encode_query = _patched_encode_query
    try:
        result = run_model(E5_MODEL, gt, scenario_k)
        result["label"] = label
        return result
    finally:
        embedder.encode = _ORIG_ENCODE
        embedder.encode_query = _ORIG_ENCODE_QUERY


def _print_scenarios(r: dict) -> None:
    print(f"\n  {bold(r['label'])}   (MRR {r['mrr']:.3f})")
    print(
        f"  {'Scenario':<10} {'PDF':<12} {'k':<4} "
        f"{'Recall':<8} {'RR':<6} {'Top-K pages'}"
    )
    print(f"  {'─'*9} {'─'*11} {'─'*3} {'─'*6} {'─'*5} {'─'*22}")
    for s in r["scenarios"]:
        top = ", ".join(str(p) for p in s["top_pages"]) or "(none)"
        print(
            f"  {s['id']:<10} {s['pdf']:<12} {s['k']:<4} "
            f"{s['recall']*100:>4.0f}%    {s['rr']:.2f}   {top}"
        )


def main() -> None:
    print(bold(cyan("\npdf-mcp E5 prefix benchmark")))
    print("─" * 68)
    print(f"  Model: {E5_MODEL}")
    print(f"  Gate:  adopt iff MRR lift ≥ {MRR_LIFT_THRESHOLD}")
    print("  First run downloads ~2GB; subsequent runs use the HF cache.\n")

    # Expanded corpus: original 7 scenarios (Attention, GPT-3) + 15 hand-verified
    # scenarios across BERT, ResNet, Adam. URLs already point at locally-cached
    # PDFs under /tmp/e5_pdfs to avoid the SSRF-hardened URLFetcher, which pins
    # to a resolved IP and breaks TLS SNI in some sandboxes.
    gt = load_ground_truth("benchmark_data/e5_prefix_corpus.json")

    seen = set()
    for pdf in gt["pdfs"].values():
        seen.update(pdf["scenarios"].keys())
    scenario_k = {sid: SCENARIO_K.get(sid, 5) for sid in seen}

    print(cyan("  Running condition: no-prefix (current production) ..."))
    no_prefix = run_condition("no-prefix (production)", gt, scenario_k, prefix=False)
    print(cyan("  Running condition: prefix ..."))
    prefix = run_condition("prefix", gt, scenario_k, prefix=True)

    _print_scenarios(no_prefix)
    _print_scenarios(prefix)

    lift = prefix["mrr"] - no_prefix["mrr"]
    lat_ratio = prefix["p50_query_ms"] / max(no_prefix["p50_query_ms"], 1e-9)

    print(bold(cyan("\n" + "=" * 68)))
    print(bold(cyan("  Verdict")))
    print(bold(cyan("=" * 68)))
    print(f"  no-prefix MRR : {no_prefix['mrr']:.3f}")
    print(f"  prefix    MRR : {prefix['mrr']:.3f}")
    color = green if lift >= MRR_LIFT_THRESHOLD else (red if lift < 0 else yellow)
    print(f"  MRR delta     : {color(f'{lift:+.3f}')}")
    print(f"  p50 latency   : {lat_ratio:.2f}x  (prefix adds ~2 tokens)")
    print()
    if lift >= MRR_LIFT_THRESHOLD:
        print(green(f"  ADOPT: prefix lifts MRR by {lift:+.3f} ≥ {MRR_LIFT_THRESHOLD}"))
    elif lift > 0:
        print(
            yellow(
                f"  MARGINAL: +{lift:.3f} below {MRR_LIFT_THRESHOLD} gate — "
                "expand corpus before deciding"
            )
        )
    else:
        print(red(f"  NO BENEFIT on this corpus (delta {lift:+.3f})"))
    print(f"\n  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
