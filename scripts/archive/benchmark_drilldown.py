#!/usr/bin/env python
"""
scripts/benchmark_drilldown.py

Per-scenario drill-down: bge-small (baseline) vs thenlper/gte-large (the clean,
no-query-prompt large candidate) on the 22-scenario corpus. The aggregate
re-benchmark showed gte-large 0.052 MRR below bge — this shows WHERE, so we can
tell a couple of flukes from a systematic difference.

Prints per-scenario reciprocal rank for both, the delta (>0 = bge ranked the
gold page higher), and each model's top pages. Saves JSON+txt to
benchmark_results/ (gitignored).

Run:
    python scripts/benchmark_drilldown.py
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
CHALLENGER = "thenlper/gte-large"
CORPUS = "benchmark_data/e5_prefix_corpus.json"


def _c(code: str, t: str) -> str:
    return f"\033[{code}m{t}\033[0m" if sys.stdout.isatty() else t


def green(t: str) -> str:
    return _c("32", t)


def red(t: str) -> str:
    return _c("31", t)


def bold(t: str) -> str:
    return _c("1", t)


def compare_scenarios(baseline: list[dict], challenger: list[dict]) -> list[dict]:
    """Pair scenarios by id; delta = baseline_rr - challenger_rr (>0 = baseline)."""
    chal = {s["id"]: s for s in challenger}
    rows = []
    for b in baseline:
        c = chal.get(b["id"], {})
        b_rr = b.get("rr", 0.0)
        c_rr = c.get("rr", 0.0)
        rows.append(
            {
                "id": b["id"],
                "pdf": b.get("pdf", ""),
                "query": b.get("query", ""),
                "gold": sorted(b.get("relevant_pages", [])),
                "baseline_rr": b_rr,
                "challenger_rr": c_rr,
                "delta": round(b_rr - c_rr, 4),
                "baseline_top": b.get("top_pages", []),
                "challenger_top": c.get("top_pages", []),
            }
        )
    return rows


def summarize(rows: list[dict]) -> dict:
    """Count baseline wins / challenger wins / ties from per-scenario deltas."""
    bw = sum(1 for r in rows if r["delta"] > 0)
    cw = sum(1 for r in rows if r["delta"] < 0)
    return {
        "scenarios": len(rows),
        "baseline_wins": bw,
        "challenger_wins": cw,
        "ties": len(rows) - bw - cw,
    }


def main() -> None:
    tee = _Tee(sys.stdout)
    with contextlib.redirect_stdout(tee):
        print(bold("\npdf-mcp per-scenario drill-down"))
        print("─" * 78)
        print(f"  baseline   = {BASELINE}")
        print(f"  challenger = {CHALLENGER}\n")

        gt = load_ground_truth(CORPUS)
        sids: set[str] = set()
        for pdf in gt["pdfs"].values():
            sids.update(pdf["scenarios"].keys())
        scenario_k = {sid: SCENARIO_K.get(sid, 5) for sid in sids}

        b = run_model(BASELINE, gt, scenario_k)
        c = run_model(CHALLENGER, gt, scenario_k)
        rows = compare_scenarios(b["scenarios"], c["scenarios"])
        rows.sort(key=lambda r: r["delta"])  # challenger's biggest wins first

        print(f"  {'scen':<6}{'pdf':<11}{'bge RR':>7}{'gte RR':>8}  {'Δ':>6}  query")
        print(f"  {'─'*5} {'─'*10} {'─'*6} {'─'*6}  {'─'*5}  {'─'*32}")
        for r in rows:
            d = r["delta"]
            mark = (
                green(f"{d:+.2f}")
                if d > 0
                else (red(f"{d:+.2f}") if d < 0 else "  .00")
            )
            print(
                f"  {r['id']:<6}{r['pdf']:<11}{r['baseline_rr']:>7.2f}"
                f"{r['challenger_rr']:>8.2f}  {mark:>6}  {r['query'][:40]}"
            )

        s = summarize(rows)
        print(
            f"\n  bge wins {green(str(s['baseline_wins']))}  |  "
            f"gte wins {red(str(s['challenger_wins']))}  |  ties {s['ties']}"
            f"   (of {s['scenarios']})"
        )
        print(f"  MRR: bge {b['mrr']:.3f}  vs  gte-large {c['mrr']:.3f}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    data = {
        "benchmark": "drilldown",
        "baseline": BASELINE,
        "challenger": CHALLENGER,
        "baseline_mrr": round(b["mrr"], 3),
        "challenger_mrr": round(c["mrr"], 3),
        "summary": s,
        "scenarios": rows,
    }
    path = save_results("drilldown", data, file_timestamp=ts, text=tee.getvalue())
    print(f"  Saved: {path}  (+ .txt)")


if __name__ == "__main__":
    main()
