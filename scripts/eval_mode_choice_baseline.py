"""Behavioral baseline for search-mode choice (billed).

Which `mode` does a calling agent pick for `pdf_corpus_search` when it sees
only the SHIPPED tool description, and does that pick beat leaving the
default? This is step 1 of the mode-choice guidance item: no description is
changed here.

For each graded corpus question, `claude -p` plays the caller. It is shown
the tool exactly as an MCP host lists it (name, description, input schema,
read from the live server) and replies with the JSON arguments of its first
call. Nothing in the prompt mentions modes. Grading is DETERMINISTIC, no
judge: the caller's own query runs through the real `pdf_corpus_search` on
a warmed cache in the mode it chose and in all three modes, scored against
the gold labels (page NDCG@10, doc NDCG@10, doc-hit@3).

Reads:
    chosen vs auto      same caller query, chosen mode minus auto. What
                        callers gain or lose by leaving the default.
    best explicit       same caller query, keyword and semantic minus auto.
                        The room a perfect teaching text could reach.

Costs: one caller call per question (cached in caller_cache.jsonl), about
9k fresh input tokens each with JUDGE_CONTEXT_FLAGS. Every call is capped
with --max-budget-usd.

Run:  PDF_MCP_CACHE_DIR=<clean warm cache> \
          uv run python scripts/eval_mode_choice_baseline.py
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import re
import subprocess
import sys

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from _query_classes import normalize_classes, normalize_rows  # noqa: E402
from eval_financial_answerability import JUDGE_CONTEXT_FLAGS  # noqa: E402

DATA = REPO / "benchmark_data" / "corpus_search"
OUT_DIR = DATA / "mode_choice"
CACHE_FILE = OUT_DIR / "caller_cache.jsonl"

DEFAULT_MODEL = "claude-opus-4-8"
CALL_TIMEOUT_S = 180
PER_CALL_BUDGET_USD = "0.50"
WORKERS = 4
TOP_K = 10
MODES = ("auto", "keyword", "semantic")
ALL_CLASSES = "paraphrase,exact_match,multi_doc,lexical_distractor"
METRICS = ("ndcg", "doc_ndcg", "dochit3")
BOOTSTRAP_N = 10000

CALLER_PROMPT = """You are an AI agent connected to an MCP server. One of \
its tools is listed below exactly as the server describes it.

Tool name: pdf_corpus_search

Description:
{description}

Input schema (JSON Schema):
{schema}

The user has a folder of about 100 research-paper PDFs at \
/Users/me/papers and asks:

"{question}"

Reply with ONLY a JSON object holding the arguments of the first \
pdf_corpus_search call you would make. Include only the arguments you \
would actually set. No explanation, no code fence."""


def shipped_tool() -> tuple[str, str]:
    """(description, input schema JSON) as the live server lists the tool."""
    from pdf_mcp import _core
    from pdf_mcp import server  # noqa: F401  (registers the tools)

    async def _get() -> tuple[str, str]:
        tools = await _core.mcp.list_tools()
        tool = next(t for t in tools if t.name == "pdf_corpus_search")
        return tool.description or "", json.dumps(tool.parameters, indent=1)

    return asyncio.run(_get())


def parse_arguments(text: str) -> dict | None:
    """The first JSON object in a caller reply, or None."""
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("query"), str):
        return None
    return parsed


def _ask_caller(prompt: str, model: str) -> dict | None:
    try:
        result = subprocess.run(
            [
                "claude",
                "-p",
                "--model",
                model,
                "--disallowedTools",
                "Bash,Read,Write,Edit,WebFetch,WebSearch",
                "--max-budget-usd",
                PER_CALL_BUDGET_USD,
                *JUDGE_CONTEXT_FLAGS,
            ],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=CALL_TIMEOUT_S,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return parse_arguments(result.stdout)


def _load_cache() -> dict[str, dict]:
    cache: dict[str, dict] = {}
    if CACHE_FILE.exists():
        for line in CACHE_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                cache[row["key"]] = row["arguments"]
    return cache


def paired_ci(diffs: list[float], rng: random.Random) -> dict:
    """Mean paired difference with a 95% bootstrap interval."""
    n = len(diffs)
    if n == 0:
        return {"n": 0, "mean": None, "lo": None, "hi": None}
    means = sorted(sum(rng.choices(diffs, k=n)) / n for _ in range(BOOTSTRAP_N))
    return {
        "n": n,
        "mean": round(sum(diffs) / n, 4),
        "lo": round(means[int(0.025 * BOOTSTRAP_N)], 4),
        "hi": round(means[int(0.975 * BOOTSTRAP_N)], 4),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--classes", default=ALL_CLASSES)
    ap.add_argument("--limit", type=int, default=0, help="first N per class (smoke)")
    ap.add_argument("--out", default="baseline_results.json")
    ap.add_argument("--warm-only", action="store_true", help="warm the cache and exit")
    args = ap.parse_args(argv)
    classes = tuple(normalize_classes(args.classes))

    import benchmark_corpus_search as bcs
    from bench_env import environment
    from benchmark_corpus_modes import (
        build_ranked,
        degraded_mode_error,
        grade_query,
        warm_corpus,
        warm_incomplete_error,
    )
    from pdf_mcp import _core
    from pdf_mcp.server import pdf_corpus_search

    cache_dir = bcs.active_cache_dir()
    if cache_dir is None:
        print("ERROR: set PDF_MCP_CACHE_DIR to a cache holding only this corpus")
        return 2
    _core.cache = bcs.open_validation_cache(cache_dir)

    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    queries = normalize_rows(
        json.loads((DATA / "queries.json").read_text(encoding="utf-8"))
    )["queries"]
    id_by_path = {str(REPO / d["path"]): d["id"] for d in manifest["docs"]}
    paths = [p for p in id_by_path if Path(p).exists()]
    if len(paths) != len(id_by_path):
        print(f"ERROR: {len(id_by_path) - len(paths)} corpus PDFs missing")
        return 2

    err = warm_incomplete_error(warm_corpus(paths))
    if err:
        print(f"ERROR: {err}")
        return 2
    # Corpus BM25 takes IDF from every cached document, so a cache holding
    # anything else scores a different ranking (mixed-cache trap).
    conn = _core.cache._connect()
    try:
        cached = {r[0] for r in conn.execute("SELECT file_path FROM pdf_metadata")}
    finally:
        conn.close()
    extra = cached - set(paths)
    if extra:
        print(f"ERROR: cache holds {len(extra)} documents outside the corpus")
        return 2
    if args.warm_only:
        print(f"warm complete: {len(paths)} docs in {cache_dir}")
        return 0

    subjects = []
    for cls in classes:
        rows = [q for q in queries if q["class"] == cls]
        subjects.extend(rows[: args.limit] if args.limit else rows)

    description, schema = shipped_tool()
    OUT_DIR.mkdir(exist_ok=True)
    cache = _load_cache()

    def run_job(q: dict) -> tuple[str, dict | None]:
        prompt = CALLER_PROMPT.format(
            description=description, schema=schema, question=q["query"]
        )
        key = hashlib.sha256(f"{args.model}|{prompt}".encode()).hexdigest()[:24]
        if key in cache:
            return q["id"], cache[key]
        arguments = _ask_caller(prompt, args.model) or _ask_caller(prompt, args.model)
        if arguments is not None:
            with CACHE_FILE.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"key": key, "arguments": arguments}) + "\n")
        return q["id"], arguments

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        emitted = dict(pool.map(run_job, subjects))
    n_errors = sum(1 for v in emitted.values() if v is None)

    def grade(q: dict, text: str, mode: str) -> dict:
        res = pdf_corpus_search(paths, text, mode=mode, top_k=TOP_K)
        if "error" in res and res.get("matches") is None:
            raise RuntimeError(f"{q['id']} {mode}: {res['error']}")
        degraded = degraded_mode_error(mode, res)
        if degraded:
            raise RuntimeError(degraded)
        return grade_query(q, build_ranked(res["matches"], id_by_path), TOP_K)

    rows = []
    for q in subjects:
        arguments = emitted[q["id"]]
        if arguments is None:
            continue
        chosen = arguments.get("mode", "auto")
        if chosen not in MODES:
            chosen = "auto"
        row = {
            "id": q["id"],
            "class": q["class"],
            "arguments": {k: v for k, v in arguments.items() if k != "paths"},
            "chosen_mode": chosen,
            "query_words": len(arguments["query"].split()),
            "original_auto": grade(q, q["query"], "auto"),
        }
        for mode in MODES:
            row[mode] = grade(q, arguments["query"], mode)
        row["chosen"] = row[chosen]
        rows.append(row)

    rng = random.Random(0)

    def diffs(sel: list[dict], a: str, b: str, metric: str) -> list[float]:
        return [
            r[a][metric] - r[b][metric]
            for r in sel
            if r[a][metric] is not None and r[b][metric] is not None
        ]

    def mean(sel: list[dict], arm: str, metric: str) -> float | None:
        vals = [r[arm][metric] for r in sel if r[arm][metric] is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    per_class = {}
    for cls in (*classes, "all"):
        sel = [r for r in rows if cls == "all" or r["class"] == cls]
        per_class[cls] = {
            "n": len(sel),
            "chosen_modes": dict(Counter(r["chosen_mode"] for r in sel)),
            "median_query_words": (
                sorted(r["query_words"] for r in sel)[len(sel) // 2] if sel else None
            ),
            "means": {
                arm: {m: mean(sel, arm, m) for m in METRICS}
                for arm in ("original_auto", "chosen", *MODES)
            },
            "paired_vs_auto": {
                arm: {m: paired_ci(diffs(sel, arm, "auto", m), rng) for m in METRICS}
                for arm in ("chosen", "keyword", "semantic")
            },
        }

    summary = {
        "model": args.model,
        "judge_context_flags": JUDGE_CONTEXT_FLAGS,
        "per_call_budget_usd": PER_CALL_BUDGET_USD,
        "top_k": TOP_K,
        "corpus_docs": len(paths),
        "caller_calls": len(subjects),
        "call_errors": n_errors,
        "description_sha256": hashlib.sha256(
            (description + schema).encode()
        ).hexdigest(),
        "environment": environment(),
        "per_class": per_class,
        "rows": rows,
    }
    out = OUT_DIR / args.out
    out.write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(f"wrote {out}")
    print(f"caller model={args.model} calls={len(subjects)} errors={n_errors}")
    for cls, block in per_class.items():
        print(f"\n{cls} (n={block['n']}) chosen modes: {block['chosen_modes']}")
        for arm in ("original_auto", "chosen", *MODES):
            m = block["means"][arm]
            print(
                f"  {arm:<14} ndcg={m['ndcg']} doc_ndcg={m['doc_ndcg']}"
                f" dochit3={m['dochit3']}"
            )
        for arm in ("chosen", "keyword", "semantic"):
            c = block["paired_vs_auto"][arm]["ndcg"]
            d = block["paired_vs_auto"][arm]["dochit3"]
            print(
                f"  {arm}-auto ndcg {c['mean']} [{c['lo']}, {c['hi']}]"
                f"  dochit3 {d['mean']} [{d['lo']}, {d['hi']}]"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
