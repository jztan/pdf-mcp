#!/usr/bin/env python
"""
scripts/author_trap_passages.py

Add a body-text passage label to every trap query, blind to all arms.

The trap class was authored to test lexical trapping (the query's terms
are boilerplate in most documents and meaningful in one), but every gold
span ended up being that document's page-1 title. Span containment then
measured "did the excerpt include the title", which one raw top-of-page
chunk always satisfies and a selected paragraph rarely does. This script
drafts, for each trap query, ONE verbatim body sentence (abstract or
introduction, pages 1 to 3) where the query's concept is actually stated,
and emits it as a second label alongside the title. The title label is
kept, so the old reading stays reproducible; the harness scores a query
as found if ANY label is found.

Usage:
    python scripts/author_trap_passages.py          # -> candidates_trap_passages.json
    python scripts/author_trap_passages.py --merge  # append accepted labels
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
DATA = REPO / "benchmark_data" / "corpus_search"

from author_corpus_queries import (  # noqa: E402
    DEFAULT_MODEL,
    ask,
    content_tokens,
    fold,
    locate_raw,
    normalize,
    parse_draft,
    raw_page_text,
    page_count,
)

MAX_PAGE = 3
PAGE_CAP = 4500


def prompt_for(query: str, pages: list[tuple[int, str]]) -> str:
    body = "\n\n".join(f"=== PAGE {n} ===\n{t[:PAGE_CAP]}" for n, t in pages)
    return (
        "You are adding a graded evidence span to a retrieval benchmark. Do NOT "
        "use outside knowledge.\n\n"
        f'The search query is: "{query}"\n\n'
        "Below are the first pages of the one paper this query is about. Copy "
        "ONE verbatim sentence from the BODY text (abstract, introduction or a "
        "later paragraph) that states what the query refers to and contains at "
        "least one of the query's words. 40 to 160 characters, exactly as "
        "printed, one line, no ellipsis. Never the title line, the author line, "
        "a heading or a caption.\n"
        'Reply with ONLY one JSON object on one line: {"page": <1-based page '
        'number>, "evidence": "..."}. If no body sentence fits, reply '
        '{"skip": "reason"}.\n\n' + body
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--merge", action="store_true")
    ap.add_argument("--veto", default="")
    args = ap.parse_args(argv)

    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    path_by_id = {d["id"]: REPO / d["path"] for d in manifest["docs"]}
    qfile = DATA / "queries.json"
    queries = json.loads(qfile.read_text(encoding="utf-8"))
    out_path = DATA / "candidates_trap_passages.json"
    cache_path = DATA / "author_cache_trap_passages.jsonl"
    cache: dict[str, dict] = {}
    if cache_path.exists():
        for line in cache_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                cache[rec["key"]] = rec

    if args.merge:
        cands = json.loads(out_path.read_text(encoding="utf-8"))["accepted"]
        veto = {v.strip() for v in args.veto.split(",") if v.strip()}
        by_id = {q["id"]: q for q in queries["queries"]}
        added = 0
        for c in cands:
            if c["id"] in veto:
                continue
            q = by_id[c["id"]]
            if any(lb.get("role") == "passage" for lb in q["labels"]):
                continue
            q["labels"].append(c["label"])
            q["trap_labels"] = "v2-2026-08-30-passage"
            added += 1
        qfile.write_text(
            json.dumps(queries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"merged {added} passage labels into {qfile}", file=sys.stderr)
        return 0

    accepted: list[dict] = []
    rejected: list[dict] = []
    for q in queries["queries"]:
        if q["class"] != "trap":
            continue
        gold = [lb for lb in q["labels"] if lb.get("gain", 0) >= 2][0]
        doc = gold["doc"]
        pc = page_count(path_by_id[doc])
        pages = [
            (n, raw_page_text(path_by_id[doc], n - 1))
            for n in range(1, min(MAX_PAGE, pc) + 1)
        ]
        key = f"trap-passage|{q['id']}"
        if key in cache:
            raw = cache[key]["raw"]
        else:
            raw = ask(prompt_for(q["query"], pages), args.model)
            rec = {
                "key": key,
                "raw": raw,
                "model": args.model,
                "at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            with cache_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            cache[key] = rec
        draft = parse_draft(raw or "")
        why: list[str] = []
        if draft is None or "skip" in (draft or {}):
            why.append(
                "no draft" if draft is None else f"drafter skip: {draft['skip']}"
            )
            rejected.append({"id": q["id"], "why": why, "raw": raw})
            continue
        try:
            page = int(draft.get("page", 0))
        except (TypeError, ValueError):
            page = 0
        ev = str(draft.get("evidence", "")).strip()
        # locate on the stated page first, then on any provided page: the
        # drafter misnumbers pages more often than it misquotes text
        located = None
        for n, t in sorted(pages, key=lambda pt: (pt[0] != page, pt[0])):
            hit = locate_raw(t, ev) if ev else None
            if hit is not None:
                located, page = hit, n
                break
        text = dict(pages).get(page, "")
        if located is None:
            why.append("evidence not on pages 1-3")
        if not (40 <= len(ev) <= 200):
            why.append(f"length {len(ev)}")
        if not (content_tokens(q["query"]) & content_tokens(ev)):
            why.append("no query token in evidence")
        title = gold["evidence"]
        # the passage may quote the title phrase; it must not BE the title
        if normalize(ev) in normalize(title):
            why.append("is the title label")
        if page == 1 and fold(ev) and fold(ev) in fold(text[:250]):
            why.append("sits in the page-1 header region")
        if why:
            rejected.append({"id": q["id"], "why": why, "evidence": ev, "page": page})
            continue
        ev_raw, how = located
        accepted.append(
            {
                "id": q["id"],
                "query": q["query"],
                "label": {
                    "doc": doc,
                    "page": page,
                    "gain": 2,
                    "evidence": ev_raw,
                    "role": "passage",
                },
                "match": how,
            }
        )
        print(f"  {q['id']} p{page} ACCEPT {ev_raw[:60]!r}", file=sys.stderr)
    out_path.write_text(
        json.dumps(
            {"accepted": accepted, "rejected": rejected}, indent=2, ensure_ascii=False
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        f"accepted {len(accepted)}, rejected {len(rejected)} -> {out_path}",
        file=sys.stderr,
    )
    for r in rejected:
        print(f"  reject {r['id']}: {r['why']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
