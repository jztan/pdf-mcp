"""Single-PDF performance on a 10-K: can pdf_search answer the question?

The corpus eval asks a 24-document corpus. This asks the simpler, more
common case: the caller already knows WHICH filing, and searches just that
one. Only questions whose answer lives in a single document are used, so
"the wrong document won" is off the table and what remains is purely
within-document retrieval + excerpt quality.

Reuses the committed eval's judge (majority of 3) so the numbers are
comparable with the corpus arm.
"""

import json
import sys

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

DATA = REPO / "benchmark_data" / "financial_reports"
TOP_K = 10


def main() -> int:
    import pdf_mcp.server as server_module

    from eval_financial_answerability import (
        DEFAULT_MODEL,
        build_payload,
        judge_majority,
    )
    from pdf_mcp.cache import PDFCache
    from pdf_mcp.server import pdf_search

    server_module.cache = PDFCache(
        cache_dir=REPO / "benchmark_data" / ".answerability_cache", ttl_hours=24 * 30
    )

    manifest = json.loads((DATA / "manifest.json").read_text())
    questions = json.loads((DATA / "answerability_questions.json").read_text())
    path_by_id = {d["id"]: str(REPO / d["path"]) for d in manifest["docs"]}
    id_by_path = {v: k for k, v in path_by_id.items()}

    mode = sys.argv[1] if len(sys.argv) > 1 else "auto"
    single = [q for q in questions["questions"] if q["scope"] == "single-doc"]
    print(f"{len(single)} questions, ONE filing each, mode={mode}\n")

    rows = []
    for q in single:
        doc = q["expect_docs"][0]
        res = pdf_search(path_by_id[doc], q["question"], mode=mode, max_results=TOP_K)
        matches = [{**m, "path": path_by_id[doc]} for m in res.get("matches", [])]
        rows.append(
            {
                "id": q["id"],
                "doc": doc,
                "pages": [m["page"] for m in matches],
                "payload": build_payload(matches, id_by_path),
            }
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        verdicts = list(
            pool.map(
                lambda pair: judge_majority(pair[0], pair[1]["payload"], DEFAULT_MODEL),
                zip(single, rows),
            )
        )

    errors = sum(1 for v in verdicts for b in v.get("votes", []) if b == "error")
    if errors:
        print(
            f"WARNING: {errors} judge ballot(s) returned an error;"
            " those questions were decided on fewer votes\n"
        )
    print(f"{'id':28s} {'doc':14s} answerable  wrong  ballots")
    tally = {"full": 0, "partial": 0, "no": 0, "wrong": 0}
    for row, v in zip(rows, verdicts):
        state = v.get("answerable", "error")
        tally[state] = tally.get(state, 0) + 1
        if v.get("wrong_attribution"):
            tally["wrong"] += 1
        print(
            f"{row['id']:28s} {row['doc']:14s} {state:10s}"
            f"  {'YES' if v.get('wrong_attribution') else '-':5s}"
            f"  {','.join(v.get('votes', []))}"
        )
    n = len(rows)
    print()
    print(
        f"[{mode}] answerable in full     : {tally['full']}/{n} ({tally['full']/n:.0%})"
    )
    print(f"partial                       : {tally['partial']}/{n}")
    print(f"not answerable                : {tally['no']}/{n}")
    print(f"wrong attribution             : {tally['wrong']}/{n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
