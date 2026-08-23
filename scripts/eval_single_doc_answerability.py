"""Single-PDF performance on a 10-K: can pdf_search answer the question?

The corpus eval asks a 24-document corpus. This asks the simpler, more
common case: the caller already knows WHICH filing, and searches just that
one. Only questions whose answer lives in a single document are used, so
"the wrong document won" is off the table and what remains is purely
within-document retrieval + excerpt quality.

Reuses the committed eval's judge (majority of 3, with lossless early
stopping) so the numbers are comparable with the corpus arm.
"""

import argparse
import json
import sys

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

DATA = REPO / "benchmark_data" / "financial_reports"
TOP_K = 10
READ_PAGES = 2
READ_CHARS = 6000


def pages_to_read(matches: list[dict], limit: int = READ_PAGES) -> list[int]:
    """The pages an agent would actually open, best-ranked first.

    Uses ONLY the response's own ranking -- never the eval's expected
    page -- so this measures an agent that follows the documented flow,
    not one that already knows the answer.
    """
    seen: list[int] = []
    for m in matches:
        if m["page"] not in seen:
            seen.append(m["page"])
        if len(seen) == limit:
            break
    return seen


def build_read_payload(search_payload: str, pages: list[int], read_result: dict) -> str:
    """Search excerpts plus the full text of the pages the agent opened."""
    parts = [search_payload, ""]
    for page in read_result.get("pages") or []:
        text = " ".join((page.get("text") or "").split())[:READ_CHARS]
        parts.append(f"--- FULL TEXT OF PAGE {page.get('page')} ---")
        parts.append(text)
        tables = page.get("tables") or []
        if tables:
            rendered = json.dumps(tables, default=str)[:READ_CHARS]
            parts.append(f"--- TABLES ON PAGE {page.get('page')} ---")
            parts.append(rendered)
    if not (read_result.get("pages") or []):
        parts.append(f"(could not read pages {pages})")
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    import pdf_mcp.server as server_module

    from eval_financial_answerability import (
        DEFAULT_MODEL,
        JUDGE_CONTEXT_FLAGS,
        build_payload,
        judge_majority,
    )
    from pdf_mcp.cache import PDFCache
    from pdf_mcp.server import pdf_read_pages, pdf_search

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "mode", nargs="?", default="auto", choices=["auto", "keyword", "semantic"]
    )
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--votes", type=int, default=3)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument(
        "--decomposed",
        action="store_true",
        help=(
            "also grade the flow this server actually documents: search to"
            " locate, then pdf_read_pages to answer. Doubles judge cost."
        ),
    )
    args = ap.parse_args(argv)

    server_module.cache = PDFCache(
        cache_dir=REPO / "benchmark_data" / ".answerability_cache", ttl_hours=24 * 30
    )

    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    questions = json.loads(
        (DATA / "answerability_questions.json").read_text(encoding="utf-8")
    )
    path_by_id = {d["id"]: str(REPO / d["path"]) for d in manifest["docs"]}
    id_by_path = {v: k for k, v in path_by_id.items()}

    mode = args.mode
    single = [q for q in questions["questions"] if q["scope"] == "single-doc"]
    print(
        f"{len(single)} questions, ONE filing each, mode={mode},"
        f" model={args.model}, votes<={args.votes}\n"
    )

    rows = []
    for q in single:
        doc = q["expect_docs"][0]
        res = pdf_search(path_by_id[doc], q["question"], mode=mode, max_results=TOP_K)
        matches = [{**m, "path": path_by_id[doc]} for m in res.get("matches", [])]
        row = {
            "id": q["id"],
            "doc": doc,
            "pages": [m["page"] for m in matches],
            "payload": build_payload(matches, id_by_path),
        }
        if args.decomposed:
            opened = pages_to_read(matches)
            read = (
                pdf_read_pages(path_by_id[doc], ",".join(str(p) for p in opened))
                if opened
                else {}
            )
            row["read_pages"] = opened
            row["payload_decomposed"] = build_read_payload(row["payload"], opened, read)
        rows.append(row)

    def judge(key: str) -> list[dict]:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            return list(
                pool.map(
                    lambda pair: judge_majority(
                        pair[0], pair[1][key], args.model, votes=args.votes
                    ),
                    zip(single, rows),
                )
            )

    verdicts = judge("payload")
    verdicts_dec = judge("payload_decomposed") if args.decomposed else []

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
    spent = sum(v.get("ballots_spent", len(v.get("votes", []))) for v in verdicts)
    print()
    print(
        f"[{mode}] answerable in full     : {tally['full']}/{n} ({tally['full']/n:.0%})"
    )
    print(f"partial                       : {tally['partial']}/{n}")
    print(f"not answerable                : {tally['no']}/{n}")
    print(f"wrong attribution             : {tally['wrong']}/{n}")
    print(
        f"judge ballots spent           : {spent} ({spent/n:.2f}/question,"
        f" vs {args.votes}.00 without early stopping)"
    )

    tally_dec: dict[str, int] = {}
    if args.decomposed:
        tally_dec = {"full": 0, "partial": 0, "no": 0, "wrong": 0}
        moved_up, moved_down = [], []
        for row, v1, v2 in zip(rows, verdicts, verdicts_dec):
            s = v2.get("answerable", "error")
            tally_dec[s] = tally_dec.get(s, 0) + 1
            if v2.get("wrong_attribution"):
                tally_dec["wrong"] += 1
            rank = {"no": 0, "partial": 1, "full": 2}
            a = rank.get(v1.get("answerable"), -1)
            b = rank.get(s, -1)
            if b > a:
                moved_up.append(row["id"])
            elif b < a:
                moved_down.append(row["id"])
        spent_dec = sum(
            v.get("ballots_spent", len(v.get("votes", []))) for v in verdicts_dec
        )
        print()
        print("SEARCH ONLY vs SEARCH-THEN-READ (the flow this server documents)")
        print(f"{'':24s} {'search':>10s} {'+read':>10s}")
        for label, key in (
            ("answerable in full", "full"),
            ("partial", "partial"),
            ("not answerable", "no"),
            ("WRONG ATTRIBUTION", "wrong"),
        ):
            print(f"{label:24s} {tally[key]:>7d}/{n:<3d} {tally_dec[key]:>7d}/{n:<3d}")
        print(
            f"\nreading the top {READ_PAGES} pages improved {len(moved_up)}"
            f" questions and worsened {len(moved_down)}"
        )
        print(f"decomposed ballots spent : {spent_dec} ({spent_dec/n:.2f}/question)")

    # Persist immediately. An earlier run printed its numbers and kept none
    # of them; the results had to be rebuilt from a scrollback dump.
    out_path = DATA / f"single_doc_{mode}_results.json"
    out_path.write_text(
        json.dumps(
            {
                "mode": mode,
                "model": args.model,
                "votes_max": args.votes,
                # The judge's context is part of the experiment: two runs
                # with different flags here are not directly comparable.
                "judge_context_flags": JUDGE_CONTEXT_FLAGS,
                "top_k": TOP_K,
                "questions": n,
                "ballots_spent": spent,
                "tally": tally,
                "decomposed": bool(args.decomposed),
                "read_pages_per_question": READ_PAGES if args.decomposed else 0,
                "tally_decomposed": tally_dec,
                "per_question": [
                    {
                        **row,
                        "verdict": verdict,
                        **(
                            {"verdict_decomposed": verdicts_dec[i]}
                            if args.decomposed
                            else {}
                        ),
                    }
                    for i, (row, verdict) in enumerate(zip(rows, verdicts))
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
