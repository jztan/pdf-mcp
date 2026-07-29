"""Multi-turn caller behavior: real agent sessions over the real server.

Every prior behavioral number (k median 2, field spread coverage 56%)
came from SINGLE-TURN planning: the caller listed follow-up calls
without seeing their results. This eval removes that floor: `claude -p`
runs full agentic sessions with pdf-mcp mounted as a real MCP server
(PDF_MCP_CACHE_DIR pointed at the warmed spike cache), starting from the
RAW benchmark question (the agent formulates its own queries). The
transcript's tool calls are then re-executed deterministically to grade
part coverage - no judge.

Measured per session: tool calls, distinct documents followed up (k),
distinct query strings (does iterative re-phrasing emerge?), and part
coverage = gold (doc,page) surfaced by ANY observed call
(corpus_search hit, pdf_search hit, or pdf_read_pages covering it).

Questions this answers: how much do real multi-turn sessions beat the
single-turn floor (56%), and does hop-conditioned re-querying (0/25 in
one-shot) emerge when the agent sees results?

Billed: 25 sessions, budget-capped per session, streams cached in
multiturn_cache/ so reruns are free.

Run:  uv run python scripts/eval_multiturn_fanout.py
Probe first: --ids spread-01
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

DATA = REPO / "benchmark_data" / "corpus_search"
OUT_DIR = DATA / "c2_rewrite"
STREAM_DIR = OUT_DIR / "multiturn_cache"
SPIKE_CACHE = REPO / "benchmark_data" / ".spike_confidence_cache"
CORPUS_DIR = Path(
    "/private/tmp/claude-501/-Users-jztan-src-pdf-mcp/"
    "46ff6b0a-dc24-458b-a7b2-9640c9ed99f5/scratchpad/corpus100"
)

DEFAULT_MODEL = "claude-opus-4-8"
SESSION_TIMEOUT_S = 420
PER_SESSION_BUDGET_USD = "0.75"
WORKERS = 3

ALLOWED = (
    "mcp__pdf-mcp__pdf_corpus_search,"
    "mcp__pdf-mcp__pdf_search,"
    "mcp__pdf-mcp__pdf_read_pages"
)

PROMPT = """You have PDF tools (MCP server "pdf-mcp") over a folder of \
about 100 research-paper PDFs at:
{corpus_dir}

Question: "{question}"

Investigate using the tools as needed and give a complete answer. The \
answer may draw on one or several documents; cite document and page for \
every part of it."""


def mcp_config(project_dir: str) -> str:
    return json.dumps(
        {
            "mcpServers": {
                "pdf-mcp": {
                    "command": "uv",
                    "args": ["run", "--project", project_dir, "pdf-mcp"],
                    "env": {"PDF_MCP_CACHE_DIR": str(SPIKE_CACHE)},
                }
            }
        }
    )


def run_session(question: str, model: str, project_dir: str) -> str | None:
    try:
        result = subprocess.run(
            [
                "claude",
                "-p",
                "--model",
                model,
                "--setting-sources",
                "",
                "--strict-mcp-config",
                "--mcp-config",
                mcp_config(project_dir),
                "--allowedTools",
                ALLOWED,
                "--max-budget-usd",
                PER_SESSION_BUDGET_USD,
                "--output-format",
                "stream-json",
                "--verbose",
            ],
            input=PROMPT.format(corpus_dir=CORPUS_DIR, question=question),
            capture_output=True,
            text=True,
            timeout=SESSION_TIMEOUT_S,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if not result.stdout.strip():
        return None
    return result.stdout


def tool_uses(stream: str) -> list[tuple[str, dict]]:
    """Extract (tool_name, input) for every tool_use in a stream-json log."""
    uses: list[tuple[str, dict]] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            if node.get("type") == "tool_use" and "name" in node:
                uses.append((node["name"], node.get("input", {}) or {}))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for line in stream.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                walk(json.loads(line))
            except json.JSONDecodeError:
                continue
    return uses


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--ids", help="comma-separated query ids (default: all)")
    ap.add_argument(
        "--project-dir",
        default=str(REPO),
        help="repo/worktree the MCP server runs from (description arm)",
    )
    ap.add_argument(
        "--tag",
        default="",
        help="suffix for stream cache dir and results file (e.g. v1)",
    )
    args = ap.parse_args(argv)

    import pdf_mcp.server as server_module

    from pdf_mcp.cache import PDFCache
    from pdf_mcp.server import parse_page_range  # noqa: F401  (via extractor)
    from pdf_mcp.server import pdf_corpus_search, pdf_search

    server_module.cache = PDFCache(cache_dir=SPIKE_CACHE, ttl_hours=24 * 30)
    stream_dir = STREAM_DIR if not args.tag else Path(str(STREAM_DIR) + "_" + args.tag)
    stream_dir.mkdir(exist_ok=True)

    manifest = json.loads((DATA / "manifest.json").read_text())
    id_by_name = {Path(d["path"]).name: d["id"] for d in manifest["docs"]}
    queries = [
        q
        for q in json.loads((DATA / "queries.json").read_text())["queries"]
        if q["class"] == "spread"
    ]
    if args.ids:
        wanted = {i.strip() for i in args.ids.split(",")}
        queries = [q for q in queries if q["id"] in wanted]

    def get_stream(q: dict) -> tuple[str, str | None]:
        path = stream_dir / f"{q['id']}.jsonl"
        if path.exists():
            return q["id"], path.read_text()
        stream = run_session(q["query"], args.model, args.project_dir)
        if stream is not None:
            path.write_text(stream)
        return q["id"], stream

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        streams = dict(pool.map(get_stream, queries))

    def doc_of(path_str: str) -> str | None:
        return id_by_name.get(Path(path_str).name)

    rows = []
    total = covered_n = 0
    for q in queries:
        gold: dict[str, set[int]] = {}
        for lab in q["labels"]:
            if lab.get("gain", 0) > 0:
                gold.setdefault(lab["doc"], set()).add(lab["page"])
        total += len(gold)
        stream = streams[q["id"]]
        if stream is None:
            rows.append({"id": q["id"], "error": True})
            continue
        uses = tool_uses(stream)

        got: set[str] = set()
        followed: set[str] = set()
        queries_used: set[str] = set()
        for name, inp in uses:
            if name.endswith("pdf_corpus_search"):
                queries_used.add(inp.get("query", ""))
                r = pdf_corpus_search(
                    inp.get("paths", str(CORPUS_DIR)),
                    inp.get("query", ""),
                    mode=inp.get("mode", "auto"),
                    top_k=inp.get("top_k", 10),
                )
                for m in r.get("matches", []):
                    d = doc_of(m["path"])
                    if d in gold and m["page"] in gold[d]:
                        got.add(d)
            elif name.endswith("pdf_search"):
                d = doc_of(inp.get("path", ""))
                queries_used.add(inp.get("query", ""))
                if d is not None:
                    followed.add(d)
                if d in gold:
                    s = pdf_search(
                        inp["path"],
                        inp.get("query", ""),
                        mode=inp.get("mode", "auto"),
                        max_results=inp.get("max_results", 10),
                    )
                    pages = {m["page"] for m in s.get("matches", [])}
                    if pages & gold[d]:
                        got.add(d)
            elif name.endswith("pdf_read_pages"):
                d = doc_of(inp.get("path", ""))
                if d is not None:
                    followed.add(d)
                if d in gold:
                    from pdf_mcp.extractor import parse_page_range as ppr

                    try:
                        pages0 = ppr(inp.get("pages"), 10_000)
                    except Exception:
                        pages0 = []
                    if {p + 1 for p in pages0} & gold[d]:
                        got.add(d)
        covered_n += len(got & set(gold))
        rows.append(
            {
                "id": q["id"],
                "n_tool_calls": len(uses),
                "k_followed": len(followed),
                "n_distinct_queries": len(queries_used),
                "gold": sorted(gold),
                "covered": sorted(got & set(gold)),
                "complete": set(gold) <= got,
                "error": False,
            }
        )

    ok = [r for r in rows if not r.get("error")]
    name = "multiturn_results" + (f"_{args.tag}" if args.tag else "") + ".json"
    out = OUT_DIR / name
    out.write_text(
        json.dumps(
            {"model": args.model, "budget": PER_SESSION_BUDGET_USD, "rows": rows},
            indent=1,
        )
    )
    print(f"wrote {out}\n")
    n = len(ok)
    if not n:
        print("no successful sessions")
        return 1
    ks = sorted(r["k_followed"] for r in ok)
    calls = sorted(r["n_tool_calls"] for r in ok)
    nq = sorted(r["n_distinct_queries"] for r in ok)
    print(f"MULTI-TURN SESSIONS (n={n}, errors={len(rows) - n})")
    print(
        f"  tool calls/session: median {calls[n // 2]} max {calls[-1]}"
        f" | docs followed up: median {ks[n // 2]} mean {sum(ks) / n:.1f}"
    )
    print(
        f"  distinct query strings/session: median {nq[n // 2]}"
        f" (single-turn baseline: 1, re-phrasing never occurred)"
    )
    print(
        f"  part coverage: {covered_n}/{total} = {covered_n / total:.0%}"
        f"  complete: {sum(r['complete'] for r in ok)}/{n}"
    )
    print("  single-turn floor: 56% coverage, k median 2, complete 6/25")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
