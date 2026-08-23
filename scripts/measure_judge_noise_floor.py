#!/usr/bin/env python
"""
scripts/measure_judge_noise_floor.py

How much does the LLM judge disagree with ITSELF?

Every delta in benchmark_data/financial_reports/RESULTS.md is a difference
between two judged runs. That difference only means something if it is
larger than the judge's own run-to-run variance -- and that variance cannot
be derived from a single run's ballots. It has to be measured: judge the
same stored payloads twice under identical configuration and count the
verdicts that move.

Payloads are read from a completed eval's results file, so retrieval is
held fixed by construction -- the judge is the only thing that varies.

Measured on 2026-07-26 (100 single-document payloads, claude-opus-4-8,
majority of 3): 13% of verdicts moved, and the headline "answerable in
full" count landed on 74, 71, and 67 across three passes. Differences
smaller than ~7 points on this metric are not interpretable.

Billed: ~2 judge calls per payload per pass. Not part of any test suite.

Run:  uv run python scripts/measure_judge_noise_floor.py
      uv run python scripts/measure_judge_noise_floor.py --results FILE
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from eval_financial_answerability import (  # noqa: E402
    DEFAULT_MODEL,
    JUDGE_CONTEXT_FLAGS,
    judge_majority,
)

DATA = REPO / "benchmark_data" / "financial_reports"


def compare(a: dict[str, str], b: dict[str, str]) -> dict[str, Any]:
    """Verdict-level disagreement between two passes over the same payloads."""
    moved = [(qid, a[qid], b[qid]) for qid in a if a[qid] != b[qid]]
    full_a = sum(1 for v in a.values() if v == "full")
    full_b = sum(1 for v in b.values() if v == "full")
    return {
        "n": len(a),
        "moved": moved,
        "noise_floor": round(len(moved) / len(a), 4) if a else 0.0,
        "full_a": full_a,
        "full_b": full_b,
        "headline_delta": full_b - full_a,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--results", default=str(DATA / "single_doc_auto_results.json"))
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--passes", type=int, default=2)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args(argv)

    if shutil.which("claude") is None:
        print("ERROR: the 'claude' CLI is not on PATH -- the judge cannot run.")
        return 2

    prior = json.loads(Path(args.results).read_text(encoding="utf-8"))
    questions = json.loads(
        (DATA / "answerability_questions.json").read_text(encoding="utf-8")
    )
    by_id = {q["id"]: q for q in questions["questions"]}
    rows = prior["per_question"]
    print(
        f"{len(rows)} payloads x {args.passes} passes,"
        f" identical config (model={args.model})\n"
    )

    passes: list[dict[str, str]] = []
    for i in range(args.passes):
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            verdicts = list(
                pool.map(
                    lambda r: judge_majority(by_id[r["id"]], r["payload"], args.model),
                    rows,
                )
            )
        passes.append({r["id"]: v["answerable"] for r, v in zip(rows, verdicts)})
        full = sum(1 for v in passes[-1].values() if v == "full")
        print(f"pass {i + 1}: answerable in full = {full}/{len(rows)}")

    print()
    results = []
    for i in range(len(passes) - 1):
        c = compare(passes[i], passes[i + 1])
        results.append(c)
        print(
            f"pass {i + 1} vs {i + 2}: {len(c['moved'])}/{c['n']}"
            f" verdicts moved = {c['noise_floor']:.0%}"
            f"   headline {c['full_a']} -> {c['full_b']}"
            f" ({c['headline_delta']:+d})"
        )
        for qid, x, y in c["moved"]:
            print(f"    {qid:30s} {x:8s} -> {y}")

    out = DATA / "judge_noise_floor.json"
    out.write_text(
        json.dumps(
            {
                "model": args.model,
                "flags": JUDGE_CONTEXT_FLAGS,
                "source_results": Path(args.results).name,
                "passes": passes,
                "comparisons": [
                    {k: v for k, v in c.items() if k != "moved"} for c in results
                ],
                "moved": [
                    {"id": q, "from": x, "to": y}
                    for c in results
                    for q, x, y in c["moved"]
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nwrote {out}")
    print(
        "\nRead every delta in RESULTS.md against this number:"
        " a difference smaller than the noise floor is not a finding."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
