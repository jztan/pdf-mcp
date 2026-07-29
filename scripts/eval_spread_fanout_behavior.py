"""Measure the unobserved variable in the spread width curve: caller k.

The coverage-vs-width table (spread_fanout_verdict.md) runs 56% at 3
docs to 87% at everything-named, but how many documents a real agent
actually re-searches after a corpus response has never been observed
(the trap recorded in what-we-tried §6). This eval measures it.

For each of the 25 spread questions, `claude -p` simulates the caller:
it sees the REAL `pdf_corpus_search` docstring, the question, and the
real corpus response (matches with trimmed excerpts + doc_match_counts),
and lists the follow-up `pdf_search` calls it would make. Grading is
deterministic: k = distinct documents chosen; realized part coverage =
the caller's chosen (doc, query) pairs actually searched through the
real single-doc tool against gold pages. Re-phrased per-doc queries are
allowed and used verbatim (this measures one-shot planning, not
iterative hop-conditioning - a live agent could do better after reading
results; noted as a floor).

Registered prior (2026-07-29, before first run): k lands at 2-4,
putting field-realistic spread coverage near the bottom of the curve.

Billed: 25 caller calls, cached in fanout_behavior_cache.jsonl,
JUDGE_CONTEXT_FLAGS + per-call budget cap per CLAUDE.md eval rules.

Run:  uv run python scripts/eval_spread_fanout_behavior.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from eval_financial_answerability import JUDGE_CONTEXT_FLAGS  # noqa: E402

DATA = REPO / "benchmark_data" / "corpus_search"
OUT_DIR = DATA / "c2_rewrite"
CACHE_FILE = OUT_DIR / "fanout_behavior_cache.jsonl"
SPIKE_CACHE = REPO / "benchmark_data" / ".spike_confidence_cache"

DEFAULT_MODEL = "claude-opus-4-8"
CALL_TIMEOUT_S = 180
PER_CALL_BUDGET_USD = "0.50"
WORKERS = 4
EXCERPT_TRIM = 150

CALLER_PROMPT = """You are an AI agent with access to these MCP tools:

1. pdf_corpus_search - full description:
---
{corpus_doc}
---
2. pdf_search(path, query, mode="auto", max_results=10) - search ONE
   local PDF, returns matching pages with excerpts.

The user asks, about a folder of about 100 research-paper PDFs:

"{question}"

You already called pdf_corpus_search and received this response:

{response_json}

Decide your follow-up tool calls now. Reply with ONLY the calls, one
per line, in exactly this format (no numbering, no commentary):
pdf_search("<path>", "<query>")
If you would make no follow-up calls, reply with the single word: none"""


def _cache_key(qid: str, model: str) -> str:
    return hashlib.sha256(f"fanout|{qid}|{model}".encode()).hexdigest()[:24]


def _load_cache() -> dict[str, str]:
    cache: dict[str, str] = {}
    if CACHE_FILE.exists():
        for line in CACHE_FILE.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                cache[row["key"]] = row["reply"]
    return cache


def _append_cache(key: str, reply: str) -> None:
    with CACHE_FILE.open("a") as fh:
        fh.write(json.dumps({"key": key, "reply": reply}) + "\n")


def _ask_caller(prompt: str, model: str) -> str | None:
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
    return result.stdout.strip()


CALL_RE = re.compile(r'pdf_search\(\s*"([^"]+)"\s*,\s*"([^"]+)"')


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args(argv)

    import pdf_mcp.server as server_module

    from pdf_mcp.cache import PDFCache
    from pdf_mcp.server import pdf_corpus_search, pdf_search

    server_module.cache = PDFCache(cache_dir=SPIKE_CACHE, ttl_hours=24 * 30)
    corpus_doc = (pdf_corpus_search.__doc__ or "").strip()

    manifest = json.loads((DATA / "manifest.json").read_text())
    id_by_path = {str(REPO / d["path"]): d["id"] for d in manifest["docs"]}
    paths = [p for p in id_by_path if Path(p).exists()]
    queries = [
        q
        for q in json.loads((DATA / "queries.json").read_text())["queries"]
        if q["class"] == "spread"
    ]
    emitted = {
        r["id"]: r["old_query"]
        for r in json.loads((OUT_DIR / "caller_eval_spread_results.json").read_text())[
            "rows"
        ]
    }

    cache = _load_cache()

    def run_one(q: dict) -> tuple[str, str | None, dict]:
        text = emitted[q["id"]]
        r = pdf_corpus_search(paths, text, mode="auto", top_k=10)
        compact = {
            "matches": [
                {
                    "path": m["path"],
                    "page": m["page"],
                    "excerpt": m["excerpt"][:EXCERPT_TRIM],
                }
                for m in r["matches"]
            ],
            "doc_match_counts": r["doc_match_counts"],
            "total_matches": r["total_matches"],
            "search_mode": r["search_mode"],
        }
        key = _cache_key(q["id"], args.model)
        if key in cache:
            return q["id"], cache[key], compact
        prompt = CALLER_PROMPT.format(
            corpus_doc=corpus_doc,
            question=text,
            response_json=json.dumps(compact, indent=1),
        )
        reply = _ask_caller(prompt, args.model) or _ask_caller(prompt, args.model)
        if reply is not None:
            _append_cache(key, reply)
        return q["id"], reply, compact

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        replies = list(pool.map(run_one, queries))

    rows = []
    total_parts = 0
    covered = 0
    for q, (qid, reply, _compact) in zip(queries, replies):
        gold_pages: dict[str, set[int]] = {}
        for lab in q["labels"]:
            if lab.get("gain", 0) > 0:
                gold_pages.setdefault(lab["doc"], set()).add(lab["page"])
        total_parts += len(gold_pages)

        calls = CALL_RE.findall(reply or "")
        chosen: dict[str, str] = {}
        for path, query_text in calls:
            doc = id_by_path.get(path)
            if doc is None:  # tolerate basename-only paths
                base = Path(path).name
                doc = next(
                    (i for p, i in id_by_path.items() if Path(p).name == base),
                    None,
                )
            if doc is not None and doc not in chosen:
                chosen[doc] = query_text

        found = []
        for doc, qtext in chosen.items():
            if doc not in gold_pages:
                continue
            p = next(p for p, i in id_by_path.items() if i == doc)
            s = pdf_search(p, qtext, mode="auto", max_results=10)
            pages = {m["page"] for m in s.get("matches", [])}
            if pages & gold_pages[doc]:
                found.append(doc)
        covered += len(found)
        rows.append(
            {
                "id": qid,
                "k": len(chosen),
                "chosen": sorted(chosen),
                "rephrased": sorted(set(chosen.values()) - {emitted[qid]}),
                "gold": sorted(gold_pages),
                "parts_found": sorted(found),
                "reply_missing": reply is None,
            }
        )

    ks = sorted(r["k"] for r in rows)
    out = OUT_DIR / "fanout_behavior_results.json"
    out.write_text(
        json.dumps(
            {
                "model": args.model,
                "judge_context_flags": JUDGE_CONTEXT_FLAGS,
                "per_call_budget_usd": PER_CALL_BUDGET_USD,
                "rows": rows,
            },
            indent=1,
        )
    )
    print(f"wrote {out}\n")
    n = len(rows)
    print(f"CALLER FAN-OUT BEHAVIOR (n={n} spread questions)")
    print(
        f"  k distribution: min={ks[0]} median={ks[n // 2]} max={ks[-1]}"
        f"  mean={sum(ks) / n:.1f}"
    )
    print(f"  errors: {sum(1 for r in rows if r['reply_missing'])}")
    rephrase = sum(1 for r in rows if r["rephrased"])
    print(f"  questions where caller re-phrased per-doc queries: {rephrase}/{n}")
    print(
        f"  REALIZED part coverage (their docs, their queries):"
        f" {covered}/{total_parts} = {covered / total_parts:.0%}"
    )
    print("  width-curve reference: 56% @3 / 65% @5 / 79% @10 / 87% all-named")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
