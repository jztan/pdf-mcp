#!/usr/bin/env python3
"""Agent-loop eval: can a naive agent read handwriting from a scanned PDF?

Grades a number, so there is no LLM judge and the project's 13% judge-noise
floor does not apply. Remaining variance is agent variance, handled with k
repeats per item.

Two item classes are reported separately on purpose. An aggregate would let a
single-page win from the codec fix mask a failure of the escape hatches on
multi-page calls, which is the "aggregates masking the class that matters"
trap in docs_internal/what-we-tried.md section 6.

Usage:
    uv run python scripts/eval_render_legibility.py \\
        --server-dir . --label after --max-budget-usd 5
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "benchmark_data" / "render_legibility"

# Same rationale as JUDGE_CONTEXT_FLAGS in eval_financial_answerability.py:
# --setting-sources '' drops both CLAUDE.md files, which is the dominant
# saving AND correct on the merits here, since an agent under test must not
# inherit repo instructions that would coach it toward `clip`.
CONTEXT_FLAGS = ["--setting-sources", ""]


def mcp_config(server_dir: Path) -> str:
    # The console script is "pdf-mcp" (pyproject.toml [project.scripts] ->
    # pdf_mcp.server:main); there is no `python -m pdf_mcp` entry point.
    return json.dumps(
        {
            "mcpServers": {
                "pdf-mcp": {
                    "command": "uv",
                    "args": ["run", "--project", str(server_dir), "pdf-mcp"],
                }
            }
        }
    )


RENDER_TOOL_NAME = "mcp__pdf-mcp__pdf_render_pages"


def run_item(
    item: dict, server_dir: Path, corpus: Path, max_budget_usd: float
) -> tuple[bool, str, int, bool]:
    """Runs one eval item and returns (pass, raw_answer_text, render_calls,
    used_clip).

    render_calls and used_clip are the spec's secondary metrics: without
    them a run cannot tell "the codec fix made it legible on the first call"
    apart from "the suggestions taught the agent to crop". They come from
    --output-format stream-json, which emits one JSON line per assistant
    turn (including tool_use blocks) instead of only the final text; plain
    --output-format text (the prior behaviour) only ever gave us raw stdout,
    with no way to recover what tools were called.
    """
    prompt = (
        f"Use the pdf-mcp tools to answer from {corpus / item['doc']}.\n"
        f"Render page(s) {item['pages']}.\n"
        f"{item['question']}\n"
        "Reply with only the number."
    )
    proc = subprocess.run(
        [
            "claude",
            "-p",
            prompt,
            "--strict-mcp-config",
            "--mcp-config",
            mcp_config(server_dir),
            "--max-budget-usd",
            str(max_budget_usd),
            "--output-format",
            "stream-json",
            "--verbose",
            *CONTEXT_FLAGS,
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )

    render_calls = 0
    used_clip = False
    final_text = ""
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        if msg.get("type") == "assistant":
            for block in msg.get("message", {}).get("content", []):
                if block.get("type") != "tool_use":
                    continue
                if block.get("name") == RENDER_TOOL_NAME:
                    render_calls += 1
                    if "clip" in (block.get("input") or {}):
                        used_clip = True
        elif msg.get("type") == "result":
            final_text = msg.get("result", "") or ""

    out = final_text.strip() or proc.stdout.strip()
    found = re.findall(r"-?\d+(?:\.\d+)?", out)
    ok = bool(found) and found[-1] == item["answer"]
    return ok, out, render_calls, used_clip


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server-dir", type=Path, default=ROOT)
    ap.add_argument("--label", required=True, help="e.g. before / after")
    ap.add_argument("-k", type=int, default=3)
    ap.add_argument(
        "--max-budget-usd",
        type=float,
        default=5.0,
        help=(
            "forwarded as --max-budget-usd to each `claude -p` call (the CLI "
            "caps spend PER invocation, not across the whole run; the total "
            "spend ceiling is roughly this times len(items) * k)"
        ),
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    spec = json.loads((DATA / "questions.json").read_text(encoding="utf-8"))
    corpus = ROOT / spec["corpus_dir"]
    items = spec["items"]

    if args.dry_run:
        print(f"{len(items)} items x k={args.k} = {len(items) * args.k} calls")
        return

    scores: dict[str, list[bool]] = defaultdict(list)
    rows = []
    for item in items:
        for trial in range(args.k):
            ok, raw, render_calls, used_clip = run_item(
                item, args.server_dir, corpus, args.max_budget_usd
            )
            scores[item["klass"]].append(ok)
            rows.append(
                (
                    item["id"],
                    item["klass"],
                    trial,
                    ok,
                    render_calls,
                    used_clip,
                    raw[:120],
                )
            )
            print(
                f"{item['id']} trial{trial}: {'PASS' if ok else 'FAIL'}"
                f" (render_calls={render_calls}, used_clip={used_clip})"
            )

    sha = subprocess.run(
        ["git", "-C", str(args.server_dir), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()

    out = DATA / "results.md"
    with out.open("a") as fh:
        fh.write(f"\n## {args.label} (server {sha}, k={args.k})\n\n")
        fh.write("Grading: exact numeric match on the last number in the reply. ")
        fh.write("No LLM judge. Per-call budget cap: ")
        fh.write(f"--max-budget-usd {args.max_budget_usd}. ")
        fh.write(f"Context flags: {CONTEXT_FLAGS}\n\n")
        for klass, results in sorted(scores.items()):
            pct = 100.0 * sum(results) / len(results)
            fh.write(f"- **{klass}**: {sum(results)}/{len(results)} ({pct:.0f}%)\n")
        fh.write(
            "\n| item | class | trial | pass | render_calls | used_clip | reply |\n"
        )
        fh.write("|---|---|---|---|---|---|---|\n")
        for r in rows:
            fh.write(
                f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[5]} | {r[6]} |\n"
            )
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
