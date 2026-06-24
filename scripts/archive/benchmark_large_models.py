#!/usr/bin/env python
"""
scripts/benchmark_large_models.py

Retrieval re-benchmark of large mean-pooled embedding models vs the bge-small
default, on the 22-scenario corpus (benchmark_data/e5_prefix_corpus.json).

Quality-first: a backend (MLX) never justifies a model; only retrieval does.
This decides purely on MRR, with the project's MRR-lift gate (>= 0.05). The MLX
speedup is relevant ONLY if a large mean-pooled model wins here.

Honest caveat — raw embedding, no query prompt. The production path embeds
queries verbatim. Several of these models (arctic, mxbai, e5) are trained with
an asymmetric query prompt and underperform without it (this is why
arctic-embed-m collapsed in the earlier embedding-model benchmark). We do NOT
add prompt handling (that is the E5-prefix path, separately evaluated and
rejected), so the raw MRR measured here IS the production-relevant number for a
drop-in default swap. gte-* are symmetric and unaffected.

Results (JSON + text log) are saved to benchmark_results/ (gitignored).

Run (downloads ~3 GB of model weights on first use):
    python scripts/benchmark_large_models.py
"""

from __future__ import annotations

import contextlib
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from benchmark_embedding_models import (  # noqa: E402
    SCENARIO_K,
    load_ground_truth,
    run_model,
)
from benchmark_mlx_backend import _Tee, save_results  # noqa: E402

BASELINE = "BAAI/bge-small-en-v1.5"

# Large mean-pooled candidates from the fastembed catalog (MLX-compatible
# pooling). Loading many large models in one process can OOM, so the candidate
# set is overridable via argv (`python benchmark_large_models.py <model> ...`)
# to run in isolated batches; results accumulate in benchmark_results/.
# Default = the candidates that actually run cleanly via fastembed 0.8 here.
# Excluded (pass explicitly via argv to retry): `thenlper/gte-base` raises a
# ValueError (inhomogeneous embedding shape) and `nomic-ai/nomic-embed-text-v1.5`
# segfaults embedding the full corpus — both non-viable in this stack.
DEFAULT_CANDIDATES = [
    "thenlper/gte-large",
    "mixedbread-ai/mxbai-embed-large-v1",
    "snowflake/snowflake-arctic-embed-l",
]
# Models trained with an asymmetric query prompt — raw (no-prompt) score here is
# a lower bound (the production path embeds queries verbatim).
PROMPTED = {
    "mixedbread-ai/mxbai-embed-large-v1",
    "nomic-ai/nomic-embed-text-v1.5",
    "snowflake/snowflake-arctic-embed-l",
    "intfloat/multilingual-e5-large",
}


def candidates() -> list[dict]:
    """Candidate models from argv, else DEFAULT_CANDIDATES."""
    names = sys.argv[1:] or DEFAULT_CANDIDATES
    return [{"name": n, "prompted": n in PROMPTED} for n in names]


CORPUS = "benchmark_data/e5_prefix_corpus.json"
MRR_GATE = 0.05


def _c(code: str, t: str) -> str:
    return f"\033[{code}m{t}\033[0m" if sys.stdout.isatty() else t


def green(t: str) -> str:
    return _c("32", t)


def red(t: str) -> str:
    return _c("31", t)


def bold(t: str) -> str:
    return _c("1", t)


def build_report(
    results: list[dict], baseline_name: str, mrr_gate: float = MRR_GATE
) -> dict:
    """Build the saved report: per-model MRR, delta vs baseline, gate flag, winner.

    `results` items need keys: model, mrr, p50_query_ms.
    """
    base = next(r for r in results if r["model"] == baseline_name)
    rows = []
    for r in results:
        delta = r["mrr"] - base["mrr"]
        beats = r["model"] != baseline_name and delta >= mrr_gate
        rows.append(
            {
                "model": r["model"],
                "mrr": round(r["mrr"], 3),
                "p50_query_ms": round(r["p50_query_ms"], 1),
                "delta_vs_baseline": round(delta, 3),
                "beats_gate": bool(beats),
            }
        )
    winners = [row for row in rows if row["beats_gate"]]
    winner = max(winners, key=lambda x: x["mrr"], default=None)
    return {
        "benchmark": "large_models",
        "corpus": f"{CORPUS} (22 scenarios)",
        "baseline": baseline_name,
        "mrr_gate": mrr_gate,
        "note": "raw embedding, no query prompt — see script docstring",
        "models": rows,
        "winner": winner["model"] if winner else None,
    }


def _run() -> dict:
    print(bold("\npdf-mcp large-model retrieval benchmark (quality-first)"))
    print("─" * 70)
    print(f"  Corpus: {CORPUS} (22 scenarios)")
    print(f"  Baseline: {BASELINE}   Gate: MRR lift >= {MRR_GATE}\n")

    gt = load_ground_truth(CORPUS)
    sids: set[str] = set()
    for pdf in gt["pdfs"].values():
        sids.update(pdf["scenarios"].keys())
    scenario_k = {sid: SCENARIO_K.get(sid, 5) for sid in sids}

    cands = candidates()
    results = []
    meta = {BASELINE: {"prompted": False}}
    meta.update({c["name"]: {"prompted": c["prompted"]} for c in cands})
    for name in [BASELINE] + [c["name"] for c in cands]:
        try:
            r = run_model(name, gt, scenario_k)
        except Exception as e:  # noqa: BLE001 — surface download/model errors as a row
            print(red(f"  {name:<46} FAILED: {type(e).__name__}: {e}"))
            results.append({"model": name, "mrr": 0.0, "p50_query_ms": float("inf")})
            continue
        flag = "  (raw; needs query prompt)" if meta[name]["prompted"] else ""
        print(f"  {name:<46} MRR {r['mrr']:.3f}  p50 {r['p50_query_ms']:6.1f}ms{flag}")
        results.append(r)

    report = build_report(results, BASELINE)
    print()
    if report["winner"]:
        print(green(f"  WINNER: {report['winner']} clears the +{MRR_GATE} MRR gate"))
    else:
        best = max(report["models"], key=lambda x: x["mrr"])
        print(
            red(
                f"  No challenger clears the gate. Best: {best['model']} "
                f"(MRR {best['mrr']}, Δ {best['delta_vs_baseline']:+.3f}). "
                f"bge-small stays the default."
            )
        )
    return report


def main() -> None:
    tee = _Tee(sys.stdout)
    with contextlib.redirect_stdout(tee):
        report = _run()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = save_results("large_models", report, file_timestamp=ts, text=tee.getvalue())
    print(f"  Saved: {path}  (+ .txt)")


if __name__ == "__main__":
    main()
