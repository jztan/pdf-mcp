"""FR2b-style behavioral eval for the C2 query-teaching text (billed).

Does a realistic calling agent, given only the tool description, emit
better `query` strings when the description teaches terms-of-art
rewriting? Two arms differ ONLY in the query-parameter docstring:

    old: the currently shipped text (keyword brevity advice)
    new: the C2 teaching text (terms of art, verbatim-distinctive,
         don't-guess-names)

For each of the 25 described + 14 needle benchmark questions, `claude -p`
simulates the caller and replies with ONLY the query string it would
pass. Grading is DETERMINISTIC, no judge: each emitted query runs through
the real `pdf_corpus_search` (hybrid, warmed cache) and is scored by gold
doc-hit@1/@3. Needle is the do-no-harm control: the new text must not
cause paraphrasing of already-distinctive queries.

Costs: 78 caller calls (one per question per arm, cached across reruns in
caller_eval_cache.jsonl). Each call carries ~9k fresh input tokens with
JUDGE_CONTEXT_FLAGS (measured on this project; see CLAUDE.md). Every call
is capped with --max-budget-usd.

Run:  uv run python scripts/eval_c2_query_teaching.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
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
CACHE_FILE = OUT_DIR / "caller_eval_cache.jsonl"
SPIKE_CACHE = REPO / "benchmark_data" / ".spike_confidence_cache"

DEFAULT_MODEL = "claude-opus-4-8"
CALL_TIMEOUT_S = 120
PER_CALL_BUDGET_USD = "0.50"
WORKERS = 4
TOP_K = 10

ARM_OLD = (
    "Text to search for. In keyword mode terms are AND-matched"
    " independently per document (FTS5); prefer short, specific terms"
    " (1-3 words) over a full question, and drop rare extra words that"
    " any single doc might not contain, or the result can come back"
    " empty."
)

ARM_NEW = (
    "Text to search for. Routing across documents works best when the"
    " query contains the words the answering document itself would use."
    " If you have a distinctive phrase, exact title, or rare term, use"
    " it verbatim, never paraphrase or generalize it. For conceptual"
    " questions that name nothing distinctive, first think of how the"
    " answering page would phrase the answer, then query with those"
    " terms of art (technique names, standard vocabulary, expanded"
    ' acronyms): "does normalizing inputs speed up training" routes'
    ' far better as "batch normalization convergence training steps".'
    " Add only terms you are confident belong; do not guess concrete"
    " names (datasets, systems) you are unsure of, and keep literal"
    " numbers and identifiers verbatim. Prefer 3-8 content-bearing terms"
    " over a full sentence. In keyword mode terms are AND-matched"
    " independently per document (FTS5); drop rare extra words that any"
    " single doc might not contain, or the result can come back empty."
)

CALLER_PROMPT = """You are an AI agent with access to MCP tools. One of them is:

pdf_corpus_search(paths, query, mode="auto", top_k=10)
  Search a corpus of local PDFs and fuse per-doc results into one
  cross-document ranking.
  query: {arm_text}

The user asks you to find, in a folder of about 100 research-paper PDFs:

"{question}"

Reply with ONLY the exact string you would pass as the `query` argument.
No surrounding quotes, no explanation, one line."""


def _cache_key(arm: str, qid: str, model: str) -> str:
    text = f"{arm}|{qid}|{model}"
    return hashlib.sha256(text.encode()).hexdigest()[:24]


def _load_cache() -> dict[str, str]:
    cache: dict[str, str] = {}
    if CACHE_FILE.exists():
        for line in CACHE_FILE.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                cache[row["key"]] = row["query"]
    return cache


def _append_cache(key: str, query: str) -> None:
    with CACHE_FILE.open("a") as fh:
        fh.write(json.dumps({"key": key, "query": query}) + "\n")


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
    # first non-empty line, stripped of any stray quoting
    line = result.stdout.strip().splitlines()[0].strip()
    return line.strip("\"'") or None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args(argv)

    import pdf_mcp.server as server_module

    from pdf_mcp.cache import PDFCache
    from pdf_mcp.server import pdf_corpus_search

    server_module.cache = PDFCache(cache_dir=SPIKE_CACHE, ttl_hours=24 * 30)

    manifest = json.loads((DATA / "manifest.json").read_text())
    queries = json.loads((DATA / "queries.json").read_text())["queries"]
    id_by_path = {str(REPO / d["path"]): d["id"] for d in manifest["docs"]}
    paths = [p for p in id_by_path if Path(p).exists()]
    subjects = [q for q in queries if q["class"] in ("described", "needle")]

    cache = _load_cache()
    jobs: list[tuple[str, dict]] = [
        (arm, q) for arm in ("old", "new") for q in subjects
    ]

    def run_job(job: tuple[str, dict]) -> tuple[str, str, str | None]:
        arm, q = job
        key = _cache_key(arm, q["id"], args.model)
        if key in cache:
            return arm, q["id"], cache[key]
        arm_text = ARM_OLD if arm == "old" else ARM_NEW
        prompt = CALLER_PROMPT.format(arm_text=arm_text, question=q["query"])
        emitted = _ask_caller(prompt, args.model)
        if emitted is None:  # one retry on transient failure
            emitted = _ask_caller(prompt, args.model)
        if emitted is not None:
            _append_cache(key, emitted)
        return arm, q["id"], emitted

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        emissions = list(pool.map(run_job, jobs))
    emitted_by = {(arm, qid): text for arm, qid, text in emissions}
    n_errors = sum(1 for _a, _q, t in emissions if t is None)

    def route(query_text: str) -> list[str]:
        r = pdf_corpus_search(paths, query_text, mode="auto", top_k=TOP_K)
        if "error" in r and r.get("matches") is None:
            return []
        docs: list[str] = []
        for m in r.get("matches", []):
            did = id_by_path[m["path"]]
            if did not in docs:
                docs.append(did)
        return docs

    rows = []
    for q in subjects:
        gold = {lab["doc"] for lab in q["labels"] if lab.get("gain", 0) > 0}
        row: dict = {"id": q["id"], "class": q["class"], "original": q["query"]}
        for arm in ("original", "old", "new"):
            text = q["query"] if arm == "original" else emitted_by.get((arm, q["id"]))
            if text is None:
                row[f"{arm}_hit1"] = row[f"{arm}_hit3"] = None
                continue
            docs = route(text)
            row[f"{arm}_query"] = text
            row[f"{arm}_hit1"] = bool(gold & set(docs[:1]))
            row[f"{arm}_hit3"] = bool(gold & set(docs[:3]))
        rows.append(row)

    def agg(cls: str, arm: str, k: str) -> str:
        vals = [r[f"{arm}_{k}"] for r in rows if r["class"] == cls]
        good = sum(1 for v in vals if v)
        n = sum(1 for v in vals if v is not None)
        return f"{good}/{n}"

    summary = {
        "model": args.model,
        "judge_context_flags": JUDGE_CONTEXT_FLAGS,
        "per_call_budget_usd": PER_CALL_BUDGET_USD,
        "caller_calls": len(jobs),
        "call_errors": n_errors,
        "described": {
            arm: {k: agg("described", arm, k) for k in ("hit1", "hit3")}
            for arm in ("original", "old", "new")
        },
        "needle": {
            arm: {k: agg("needle", arm, k) for k in ("hit1", "hit3")}
            for arm in ("original", "old", "new")
        },
        "rows": rows,
    }
    out = OUT_DIR / "caller_eval_results.json"
    out.write_text(json.dumps(summary, indent=1))
    print(f"wrote {out}\n")
    print(f"caller model={args.model}  calls={len(jobs)}  errors={n_errors}")
    print(f"{'class':<11}{'arm':<10}{'doc-hit@1':>10}{'doc-hit@3':>10}")
    for cls in ("described", "needle"):
        for arm in ("original", "old", "new"):
            print(
                f"{cls:<11}{arm:<10}{agg(cls, arm, 'hit1'):>10}"
                f"{agg(cls, arm, 'hit3'):>10}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
