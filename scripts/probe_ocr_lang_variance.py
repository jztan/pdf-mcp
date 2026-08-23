#!/usr/bin/env python3
"""Do LLM callers actually vary the ocr_lang string? (issue #27)

SPIKE OUTPUT, AND THIS RUN IS BILLED. It shells out to `claude -p` once
per trial and each trial really calls pdf_read_pages over MCP.

The whole justification for widening page_text's primary key is that the
main caller of this server is a model, and a model regenerates its tool
arguments from scratch each time rather than reusing a fixed string. That
is an assumption. This measures it.

Each trial is an INDEPENDENT session: no shared context, which is the
realistic analogue of two conversations touching the same file, or of one
agent resuming after its context was compacted. Within a single session a
model can simply copy its own earlier call, so within-session consistency
would prove nothing about the case that thrashes the cache.

Two arms differ only in how the user's request orders the languages,
since a user's phrasing also varies between conversations:

    ru_first  "... contains Russian and English text ..."
    en_first  "... contains English and Russian text ..."

What is being counted is the number of DISTINCT ocr_lang strings across
trials. One distinct string overall means models are consistent and the
wider primary key is fixing a case that does not occur. More than one
means the thrash is reachable without anyone doing anything wrong.

Per CLAUDE.md: --setting-sources '' drops both CLAUDE.md files (a caller
should not inherit this repo's commit conventions), and --strict-mcp-config
loads only pdf-mcp instead of all ten configured servers. Do NOT add
--system-prompt: it busts the cached prefix and bills more.

    python scripts/probe_ocr_lang_variance.py --trials 10 --budget 5
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from measure_ocr_lang_thrash import build_bilingual_scan  # noqa: E402

MODEL = "claude-opus-4-8"
TOOL = "mcp__pdf-mcp__pdf_read_pages"

# Only pdf-mcp, launched from this checkout so the probe measures the
# tool schema this repo actually ships.
MCP_CONFIG = json.dumps(
    {
        "mcpServers": {
            "pdf-mcp": {
                "type": "stdio",
                "command": "/Users/jztan/src/pdf-mcp/.venv/bin/python",
                "args": ["-m", "pdf_mcp.server"],
                "env": {},
            }
        }
    }
)

# Dropping CLAUDE.md and the other MCP servers; see module docstring.
CONTEXT_FLAGS = [
    "--setting-sources",
    "",
    "--strict-mcp-config",
    "--mcp-config",
    MCP_CONFIG,
]

PROMPTS = {
    "ru_first": (
        "The file at {path} is a scanned PDF containing Russian and English "
        "text. It has no text layer, so it needs OCR. Read page 1 and tell "
        "me what the first sentence says."
    ),
    "en_first": (
        "The file at {path} is a scanned PDF containing English and Russian "
        "text. It has no text layer, so it needs OCR. Read page 1 and tell "
        "me what the first sentence says."
    ),
}


def run_trial(prompt: str, budget: float) -> dict:
    """One independent claude -p session; returns the ocr_lang it passed."""
    proc = subprocess.run(
        [
            "claude",
            "-p",
            prompt,
            "--model",
            MODEL,
            "--output-format",
            "stream-json",
            "--verbose",
            "--allowedTools",
            TOOL,
            "--max-turns",
            "6",
            "--max-budget-usd",
            str(budget),
            *CONTEXT_FLAGS,
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    calls = []
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        # `message` is a dict on assistant events but a bare string on some
        # others, so this cannot assume a shape.
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == TOOL
            ):
                calls.append(block.get("input", {}))
    return {
        "returncode": proc.returncode,
        "tool_calls": calls,
        # An explicit ocr_lang is what keys the cache. A call that omits it
        # takes the "eng" default, which is a different (and also real)
        # behaviour, so record the distinction rather than flattening it.
        "ocr_langs": [c.get("ocr_lang", "<omitted>") for c in calls],
        "stderr_tail": proc.stderr[-300:] if proc.returncode != 0 else "",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=10, help="trials per arm")
    ap.add_argument("--budget", type=float, default=0.50, help="max USD per trial")
    ap.add_argument(
        "--out",
        default="benchmark_data/ocr_lang_variance_results.json",
    )
    args = ap.parse_args()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as workdir:
        pdf_path = str(pathlib.Path(workdir) / "bilingual_scan.pdf")
        build_bilingual_scan(pdf_path, 1)

        out = pathlib.Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)

        arms: dict[str, list] = {}
        for arm, template in PROMPTS.items():
            prompt = template.format(path=pdf_path)
            trials: list[dict] = []
            arms[arm] = trials
            for i in range(args.trials):
                result = run_trial(prompt, args.budget)
                trials.append(result)
                langs = ", ".join(result["ocr_langs"]) or "(no tool call)"
                print(f"{arm} {i + 1:>2}/{args.trials}: {langs}", flush=True)
                # Persist after every trial: these calls cost money, and a
                # crash in trial 7 must not throw away trials 1-6.
                out.write_text(json.dumps({"arms": arms}, indent=2), encoding="utf-8")

    payload = {
        "config": {
            "model": MODEL,
            "trials_per_arm": args.trials,
            "max_budget_usd_per_trial": args.budget,
            "context_flags": CONTEXT_FLAGS[:3] + ["--mcp-config", "<pdf-mcp only>"],
        },
        "arms": arms,
    }

    print("\n" + "=" * 58)
    all_langs: list[str] = []
    for arm, trials in arms.items():
        langs = [lang for t in trials for lang in t["ocr_langs"]]
        all_langs.extend(langs)
        counts = collections.Counter(langs)
        print(f"{arm}: {len(set(langs))} distinct")
        for lang, n in counts.most_common():
            print(f"    {n:>3d}x  {lang!r}")
    distinct = sorted(set(all_langs))
    print(f"\noverall distinct ocr_lang strings: {len(distinct)}  {distinct}")
    payload["summary"] = {
        "distinct_overall": distinct,
        "counts_overall": dict(collections.Counter(all_langs)),
    }

    pathlib.Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
