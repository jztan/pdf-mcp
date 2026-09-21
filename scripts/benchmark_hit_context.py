#!/usr/bin/env python3
"""Does a search hit carry the words that tell it apart from a look-alike?

Each query targets a table or passage with a look-alike elsewhere in the
same filing (segment vs consolidated, pro forma vs actual, quarterly vs
annual, GAAP vs non-GAAP, parent-only vs consolidated). Each labels the gold
page and a `context_marker`, the words printed on that page that tell the
two apart. For the gold-page hit of `pdf_search` (paragraph excerpts):

    before = marker in the excerpt
    after  = marker in the excerpt, `section_path` or `lead_in`

reported with a paired bootstrap CI overall and split by whether the filing
has an outline (only those can carry `section_path`). Deterministic and
free. Point PDF_MCP_CACHE_DIR at a scratch directory: a cold run extracts
every filing once.

Exit codes: 0 ran, 2 setup error (missing query set or PDF).

Usage:
    PDF_MCP_CACHE_DIR=/tmp/hc uv run python scripts/benchmark_hit_context.py
"""

import argparse
import json
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DATA = REPO / "benchmark_data" / "hit_context"


def _bootstrap_ci(
    before: list[int], after: list[int], n: int = 10000
) -> tuple[float, float]:
    rng = random.Random(0)
    diffs = []
    for _ in range(n):
        idx = [rng.randrange(len(before)) for _ in before]
        diffs.append(sum(after[i] - before[i] for i in idx) / len(before))
    diffs.sort()
    return diffs[int(0.025 * n)], diffs[int(0.975 * n)]


def score(queries: list[dict], corpus: Path) -> list[dict]:
    from pdf_mcp.server import pdf_search

    rows = []
    for q in queries:
        res = pdf_search(str(corpus / q["pdf"]), q["query"], max_results=5)
        hit = next(
            (m for m in res.get("matches", []) if m["page"] == q["gold_page"]), None
        )
        marker = q["context_marker"].lower()
        row = {
            "id": q["id"],
            "class": q["class"],
            "has_outline": q["has_outline"],
            "page_hit": hit is not None,
            "before": 0,
            "after": 0,
        }
        if hit is not None:
            in_excerpt = marker in hit["excerpt"].lower()
            context = " ".join(hit.get("section_path") or []) + " "
            context += hit.get("lead_in") or ""
            row["before"] = int(in_excerpt)
            row["after"] = int(in_excerpt or marker in context.lower())
            row["section_path"] = hit.get("section_path")
            row["lead_in"] = hit.get("lead_in")
        rows.append(row)
    return rows


def summarize(rows: list[dict]) -> list[dict]:
    slices = [("all", rows)]
    slices.append(("outline", [r for r in rows if r["has_outline"]]))
    slices.append(("no outline", [r for r in rows if not r["has_outline"]]))
    for cls in sorted({r["class"] for r in rows}):
        slices.append((cls, [r for r in rows if r["class"] == cls]))
    out = []
    for name, rs in slices:
        if not rs:
            continue
        before = [r["before"] for r in rs]
        after = [r["after"] for r in rs]
        lo, hi = _bootstrap_ci(before, after)
        out.append(
            {
                "slice": name,
                "n": len(rs),
                "page_hit": sum(r["page_hit"] for r in rs),
                "before": sum(before),
                "after": sum(after),
                "diff": round((sum(after) - sum(before)) / len(rs), 3),
                "ci": [round(lo, 3), round(hi, 3)],
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    args = ap.parse_args(argv)
    qfile = args.data_dir / "queries.json"
    if not qfile.exists():
        print(f"ERROR: no query set at {qfile}", file=sys.stderr)
        return 2
    data = json.loads(qfile.read_text(encoding="utf-8"))
    corpus = REPO / data["corpus"]
    queries = [q for q in data["queries"] if not q.get("excluded")]
    missing = sorted({q["pdf"] for q in queries if not (corpus / q["pdf"]).exists()})
    if missing:
        print(f"ERROR: missing PDFs under {corpus}: {missing}", file=sys.stderr)
        return 2

    rows = score(queries, corpus)
    summary = summarize(rows)
    for s in summary:
        print(
            f"{s['slice']:40} n={s['n']:2} page_hit={s['page_hit']:2}"
            f" before={s['before']:2} after={s['after']:2}"
            f" diff={s['diff']:+.3f} CI[{s['ci'][0]:+.3f},{s['ci'][1]:+.3f}]"
        )
    out = args.data_dir / "results.json"
    out.write_text(
        json.dumps(
            {"version": data.get("version"), "summary": summary, "rows": rows},
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
