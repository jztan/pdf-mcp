"""Arm P re-run with excerpt_style="snippet" instead of the default paragraph.

Standalone so it does not collide with edits to scripts/. Scores with the
same containment functions the harness uses, at the same 2,000-token budget.
"""

import collections
import json
import sys
import time
from pathlib import Path

REPO = Path("/Users/jztan/src/pdf-mcp-bedrock-kb")
sys.path.insert(0, str(REPO / "scripts"))

from benchmark_bedrock_kb import (  # noqa: E402
    cap_to_budget,
    grade_containment,
    matches_to_units,
)

BUDGET = 2000
TOP_K = 25


def main() -> int:
    from pdf_mcp.server import pdf_corpus_search

    manifest = json.loads(
        (REPO / "benchmark_data/corpus_search/manifest.json").read_text()
    )
    queries = json.loads(
        (REPO / "benchmark_data/corpus_search/queries.json").read_text()
    )["queries"]
    id_by_path = {str((REPO / d["path"]).resolve()): d["id"] for d in manifest["docs"]}
    paths = list(id_by_path)

    rows = {}
    t0 = time.time()
    for i, q in enumerate(queries, 1):
        res = pdf_corpus_search(
            paths, q["query"], mode="auto", top_k=TOP_K, excerpt_style="snippet"
        )
        if "error" in res:
            print(f"ERROR {q['id']}: {res['error']}")
            return 2
        units = matches_to_units(res["matches"], id_by_path)
        kept, k = cap_to_budget(units, BUDGET)
        rows[q["id"]] = {
            "class": q["class"],
            "realized_k": k,
            "status": grade_containment(q, kept)["status"],
            "span_recall": grade_containment(q, kept)["span_recall"],
        }
        if i % 20 == 0:
            print(f"  {i}/{len(queries)} ({time.time() - t0:.0f}s)", flush=True)

    out = REPO / "benchmark_data/bedrock_kb/snippet_arm_results.json"
    out.write_text(json.dumps(rows, indent=2))

    by_class = collections.defaultdict(list)
    for r in rows.values():
        by_class[r["class"]].append(r)
    print(f"\n{'class':<12}{'n':>4}{'snippet':>10}{'mean k':>9}")
    for cls in sorted(by_class):
        sel = by_class[cls]
        sr = sum(r["span_recall"] for r in sel) / len(sel)
        mk = sum(r["realized_k"] for r in sel) / len(sel)
        print(f"{cls:<12}{len(sel):>4}{sr:>10.3f}{mk:>9.1f}")
    print(f"\nelapsed {time.time() - t0:.0f}s -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
