"""Diagnose arm P's span-recall gap: retrieval miss or excerpt-picker miss.

Reads `benchmark_data/bedrock_kb/results.json` (arm P's per-query rows from
`benchmark_bedrock_kb.py`) and `benchmark_data/corpus_search/queries.json`.
For every non-flagged query where arm P's containment status was `missing`,
checks whether P's kept units already include the graded (doc, page) pair,
and if so, re-extracts that page from the source PDF and re-checks the
evidence span against it with the harness's own `contain()` rule. This is
independent of what the excerpt text inside P's kept unit said: it answers
"was the right page in front of P" separately from "did P's selected
excerpt happen to carry the span".

No AWS calls. Requires `results.json` to already exist (run
`benchmark_bedrock_kb.py` first).

Run:  uv run python scripts/diagnose_arm_p_excerpt_gap.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

RESULTS = REPO / "benchmark_data" / "bedrock_kb" / "results.json"
DATA_DIR = REPO / "benchmark_data" / "corpus_search"

_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    return _WS.sub(" ", text).strip().lower()


def contain(context: str, evidence: str) -> str:
    """Mirrors benchmark_bedrock_kb.contain: exact / normalized / missing."""
    if evidence in context:
        return "exact"
    if normalize(evidence) in normalize(context):
        return "normalized"
    return "missing"


def diagnose(results: dict, queries: list[dict], manifest: dict) -> dict:
    import fitz

    from pdf_mcp.extractor import extract_text_from_page

    qs = {q["id"]: q for q in queries}
    docs = {d["id"]: d["path"] for d in manifest["docs"]}
    flagged = set(results["summary"]["flagged"])
    rows_p = results["per_query"]["P"]

    cases = []
    for qid, row in rows_p.items():
        if qid in flagged or row["containment"]["status"] != "missing":
            continue
        q = qs[qid]
        kept = {(d, p) for d, p in row["kept"]}
        page_returned = False
        span_present = False
        for lb in q["labels"]:
            if "page" not in lb or "evidence" not in lb:
                continue
            key = (lb["doc"], lb["page"])
            if key not in kept:
                continue
            page_returned = True
            path = REPO / docs[lb["doc"]]
            doc = fitz.open(str(path))
            try:
                text = extract_text_from_page(doc[lb["page"] - 1])
            finally:
                doc.close()
            if contain(text, lb["evidence"]) != "missing":
                span_present = True
        cases.append(
            {"id": qid, "page_returned": page_returned, "span_present": span_present}
        )

    page_returned = sum(1 for c in cases if c["page_returned"])
    span_present = sum(1 for c in cases if c["page_returned"] and c["span_present"])
    span_absent = page_returned - span_present
    return {
        "n_missing": len(cases),
        "page_returned": page_returned,
        "span_present_on_returned_page": span_present,
        "span_absent_from_returned_page": span_absent,
        "page_not_returned": len(cases) - page_returned,
        "cases": cases,
    }


def main(argv: list[str] | None = None) -> int:
    if not RESULTS.exists():
        print(f"ERROR: {RESULTS} not found; run benchmark_bedrock_kb.py first")
        return 2
    results = json.loads(RESULTS.read_text(encoding="utf-8"))
    queries = json.loads((DATA_DIR / "queries.json").read_text(encoding="utf-8"))
    manifest = json.loads((DATA_DIR / "manifest.json").read_text(encoding="utf-8"))
    out = diagnose(results, queries["queries"], manifest)

    print(f"Arm P containment=missing, non-flagged: {out['n_missing']}")
    print(f"  page returned by P:             {out['page_returned']}")
    print(f"    span present on that page:    {out['span_present_on_returned_page']}")
    print(f"    span absent from that page:   {out['span_absent_from_returned_page']}")
    print(f"  page NOT returned by P:         {out['page_not_returned']}")
    for c in out["cases"]:
        print(
            f"  {c['id']}: page_returned={c['page_returned']} "
            f"span_present={c['span_present']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
