#!/usr/bin/env python
"""
scripts/eval_financial_answerability.py

Can a caller ANSWER a realistic 10-K question from what the tool returns?

The retrieval benchmark (scripts/benchmark_corpus_modes.py) asks "is the
gold page in the top 10". That is necessary but not sufficient: a page can
be retrieved while the excerpt shown quotes the neighbouring segment, and
an agent reading that excerpt reports the wrong number. Rank metrics score
such a result as a hit. This eval scores it as a failure.

Two layers, deliberately separated:

  OBJECTIVE (computed in code, no model involved)
    - doc coverage: fraction of the docs a complete answer needs that are
      represented in the payload at all
    - balance: for multi-document questions, the share held by the
      least-represented expected doc (a 9-vs-1 split cannot answer
      "compare A with B")

  JUDGED (`claude -p`, same shelling-out pattern as eval_coherence.py)
    Given the question, the HAND-VERIFIED reference facts, and the payload
    the caller would actually see, the judge reports whether the payload
    supports a correct and complete answer, and whether it contains a
    wrong-attribution trap. The judge never supplies ground truth -- it
    only compares against reference facts authored from extracted text.

Billed: one `claude -p` call per question. Not part of any test suite.

Run:  uv run python scripts/eval_financial_answerability.py
      uv run python scripts/eval_financial_answerability.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

DATA = REPO / "benchmark_data" / "financial_reports"
DEFAULT_MODEL = "claude-opus-4-8"
DENIED_TOOLS = "Bash,Read,Write,Edit,WebFetch,WebSearch"
JUDGE_TIMEOUT_S = 180
TOP_K = 10

RUBRIC = """You are grading a document-retrieval result, not writing an answer.

You are given: a QUESTION, a list of REFERENCE FACTS (verified by hand from
the source documents), and the PAYLOAD a caller received from a search tool.
The payload is all the caller sees -- treat it as the complete evidence.

Decide:

1. answerable:
   - "full"    : the payload contains enough to answer the question
                 correctly and completely, covering every reference fact
                 the question requires.
   - "partial" : the payload supports part of the answer but a required
                 piece is missing (e.g. one of three years, or one of two
                 companies in a comparison).
   - "no"      : the payload does not support answering the question.

2. wrong_attribution (true/false): does the payload prominently present a
   figure or statement that belongs to a DIFFERENT segment, company, or
   fiscal year in a position where a reader would reasonably take it as the
   answer? This is the dangerous failure -- worse than returning nothing --
   so judge it strictly. If the payload's top excerpts quote a confusable
   neighbour instead of the asked-about subject, that is true.

3. missing: brief note on what a complete answer still needs, or "" if none.

Reply with ONLY a JSON object, no prose:
{"answerable": "full|partial|no", "wrong_attribution": true|false,
 "missing": "...", "reason": "one sentence"}"""


def build_payload(matches: list[dict[str, Any]], id_by_path: dict[str, str]) -> str:
    lines = []
    for i, m in enumerate(matches, 1):
        doc = id_by_path.get(m["path"], Path(m["path"]).stem)
        excerpt = " ".join((m.get("excerpt") or "").split())
        lines.append(f"{i}. [{doc} page {m['page']}] {excerpt}")
    return "\n".join(lines) if lines else "(no results returned)"


def extract_json(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return {"answerable": "error", "wrong_attribution": False, "reason": "no JSON"}
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {
            "answerable": "error",
            "wrong_attribution": False,
            "reason": "bad JSON",
        }


def judge_one(question: dict[str, Any], payload: str, model: str) -> dict[str, Any]:
    facts = "\n".join(f"- {f}" for f in question["reference_facts"])
    confusable = question.get("confusable_with", "")
    prompt = (
        f"{RUBRIC}\n\n"
        f"QUESTION: {question['question']}\n\n"
        f"REFERENCE FACTS (ground truth):\n{facts}\n\n"
        f"COMMONLY CONFUSED WITH: {confusable}\n\n"
        f"PAYLOAD THE CALLER RECEIVED:\n{payload}\n"
    )
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", model, "--disallowedTools", DENIED_TOOLS],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=JUDGE_TIMEOUT_S,
            check=False,
        )
    except (FileNotFoundError, OSError) as exc:
        return {"answerable": "error", "wrong_attribution": False, "reason": str(exc)}
    except subprocess.TimeoutExpired:
        return {"answerable": "error", "wrong_attribution": False, "reason": "timeout"}
    if result.returncode != 0 or not result.stdout.strip():
        return {
            "answerable": "error",
            "wrong_attribution": False,
            "reason": f"exit {result.returncode}",
        }
    return extract_json(result.stdout)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--mode", default="auto", choices=["auto", "keyword", "semantic"])
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="run retrieval and objective metrics only; no billed judge calls",
    )
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args(argv)

    if not args.dry_run and shutil.which("claude") is None:
        print("ERROR: the 'claude' CLI is not on PATH — the judge cannot run.")
        return 2

    import pdf_mcp.server as server_module

    from pdf_mcp.cache import PDFCache
    from pdf_mcp.server import pdf_corpus_search, pdf_corpus_warm

    manifest = json.loads((DATA / "manifest.json").read_text())
    questions = json.loads((DATA / "answerability_questions.json").read_text())
    id_by_path = {str(REPO / d["path"]): d["id"] for d in manifest["docs"]}
    paths = [p for p in id_by_path if Path(p).exists()]
    if not paths:
        print("ERROR: no corpus docs available; run scripts/fetch_financial_corpus.py")
        return 2

    cache_dir = REPO / "benchmark_data" / ".answerability_cache"
    server_module.cache = PDFCache(cache_dir=cache_dir, ttl_hours=24 * 30)
    warm = pdf_corpus_warm(paths, budget_seconds=600, embeddings=True)
    while warm.get("unprocessed"):
        warm = pdf_corpus_warm(paths, budget_seconds=600, embeddings=True)
    print(f"corpus warm ({len(paths)} docs)\n")

    rows: list[dict[str, Any]] = []
    for q in questions["questions"]:
        res = pdf_corpus_search(paths, q["question"], mode=args.mode, top_k=TOP_K)
        matches = res.get("matches", [])
        got_docs = [id_by_path.get(m["path"], "") for m in matches]
        expect = q["expect_docs"]
        present = [d for d in expect if d in got_docs]
        counts = {d: got_docs.count(d) for d in expect}
        balance = (
            min(counts.values()) / max(1, len(matches)) if len(expect) > 1 else 1.0
        )
        rows.append(
            {
                "id": q["id"],
                "type": q["type"],
                "question": q["question"],
                "doc_coverage": len(present) / len(expect),
                "expect_docs": expect,
                "doc_counts": counts,
                "balance": round(balance, 3),
                "n_matches": len(matches),
                "payload": build_payload(matches, id_by_path),
            }
        )

    if not args.dry_run:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            verdicts = list(
                pool.map(
                    lambda pair: judge_one(pair[0], pair[1]["payload"], args.model),
                    zip(questions["questions"], rows),
                )
            )
        for row, verdict in zip(rows, verdicts):
            row["verdict"] = verdict

    n = len(rows)
    full = sum(1 for r in rows if r.get("verdict", {}).get("answerable") == "full")
    partial = sum(
        1 for r in rows if r.get("verdict", {}).get("answerable") == "partial"
    )
    none_ = sum(1 for r in rows if r.get("verdict", {}).get("answerable") == "no")
    wrong = sum(1 for r in rows if r.get("verdict", {}).get("wrong_attribution"))
    cov = sum(r["doc_coverage"] for r in rows) / n

    print(f"{'id':28s} {'cov':>5s} {'bal':>5s}  answerable  wrong-attrib")
    for r in rows:
        v = r.get("verdict", {})
        print(
            f"{r['id']:28s} {r['doc_coverage']:5.2f} {r['balance']:5.2f}"
            f"  {str(v.get('answerable', '-')):10s}"
            f"  {'YES' if v.get('wrong_attribution') else '-'}"
        )
    print()
    print(f"questions            : {n}")
    print(f"mean doc coverage    : {cov:.2f}")
    if not args.dry_run:
        print(f"answerable in full   : {full}/{n} ({full/n:.0%})")
        print(f"partial              : {partial}/{n} ({partial/n:.0%})")
        print(f"not answerable       : {none_}/{n} ({none_/n:.0%})")
        print(f"WRONG ATTRIBUTION    : {wrong}/{n} ({wrong/n:.0%})")

    out = {
        "mode": args.mode,
        "top_k": TOP_K,
        "corpus_docs": len(paths),
        "summary": {
            "questions": n,
            "mean_doc_coverage": round(cov, 4),
            "full": full,
            "partial": partial,
            "no": none_,
            "wrong_attribution": wrong,
        },
        "per_question": rows,
    }
    (DATA / "answerability_results.json").write_text(
        json.dumps(out, indent=2, sort_keys=True) + "\n"
    )
    print(f"\nwrote {DATA / 'answerability_results.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
