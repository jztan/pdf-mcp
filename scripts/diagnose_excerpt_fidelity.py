#!/usr/bin/env python
"""
scripts/diagnose_excerpt_fidelity.py

Does the excerpt actually carry the answer that is on the page we found?

The answerability eval grades the whole payload with an LLM, which costs
money and carries a 13% verdict noise floor (see
scripts/measure_judge_noise_floor.py). This asks a narrower question that
needs no judge at all and is fully deterministic:

  1. locate the page that verifiably contains the reference fact
  2. ask pdf_search the question
  3. if that page came back, does its excerpt quote the fact -- and in
     particular the FIGURE the fact turns on?

Every question therefore lands in exactly one bucket:

  ok            excerpt carries the fact          -- nothing to fix
  EXCERPT MISS  right page retrieved, wrong block -- excerpt selection
  RECALL MISS   page never retrieved              -- ranking / scoring
  unlocatable   reference fact not found in text  -- ground-truth issue

The split matters because the three have different fixes, and the
wrong-attribution failures found by the judge turned out to be almost
entirely the second kind: the page ranked first and the excerpt quoted a
neighbouring paragraph. On a segment-results page the consolidated and
per-segment paragraphs share nearly every query token, so the block picker
has no signal separating them.

Free: no judge calls. Retrieval is deterministic, so this is repeatable.

Run:  uv run python scripts/diagnose_excerpt_fidelity.py
      uv run python scripts/diagnose_excerpt_fidelity.py --ids a,b,c
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

DEFAULT_DATA = REPO / "benchmark_data" / "financial_reports"
CACHE_DIR = REPO / "benchmark_data" / ".answerability_cache"
TOP_K = 10
ANCHOR_WORDS = 8
MAX_PAGES = 400

# "$12.9 billion", "17%", "224.2" -- the tokens an answer actually turns on.
FIGURE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?")

QUESTION_FILES = ("fidelity_questions.json", "answerability_questions.json")


@dataclass(frozen=True)
class Question:
    """One question, normalized across the two dataset schemas."""

    id: str
    type: str
    question: str
    doc: str
    fact: str
    span: str | None


def load_dataset(data_dir: Path) -> list[Question]:
    """Read the questions file, whichever schema the dataset uses.

    The financial set nests values in lists (expect_docs, reference_facts)
    and carries a scope field; the arXiv set uses singular keys, has no
    scope, and adds answer_span. Everything downstream sees one shape.
    """
    for name in QUESTION_FILES:
        path = data_dir / name
        if path.exists():
            raw = json.loads(path.read_text())["questions"]
            break
    else:
        raise SystemExit(
            f"no questions file in {data_dir}; expected one of"
            f" {', '.join(QUESTION_FILES)}"
        )

    out: list[Question] = []
    for q in raw:
        if q.get("scope", "single-doc") != "single-doc":
            continue
        docs = q.get("expect_docs")
        facts = q.get("reference_facts")
        out.append(
            Question(
                id=q["id"],
                type=q.get("type", "?"),
                question=q["question"],
                doc=docs[0] if docs else q["expect_doc"],
                fact=facts[0] if facts else q["reference_fact"],
                span=q.get("answer_span"),
            )
        )
    return out


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def figures(text: str) -> set[str]:
    """Numeric tokens, ignoring bare small integers that match by chance."""
    out = set()
    for m in FIGURE.findall(text):
        t = m.strip().rstrip(".")
        if t.startswith("$") or t.endswith("%") or "." in t or "," in t:
            out.add(t.lstrip("$").rstrip("%"))
    return out


def locate(fact: str, pages: dict[int, str]) -> list[int]:
    """EVERY page that states the fact, not just the first one found.

    A 10-K repeats the same figure in the MD&A, the segment note, and the
    financial statements. Stopping at the first matching window pins one
    location and reports the others as recall misses -- which is how an
    earlier version of this script produced 28% "RECALL MISS" on questions
    the judge had already called fully answerable.

    So: union across all anchor windows, plus any page carrying the exact
    figures the fact turns on together with a content word from it.
    """
    toks = norm(fact).split()
    normed = {n: norm(t) for n, t in pages.items()}
    hits: set[int] = set()
    for i in range(max(1, len(toks) - ANCHOR_WORDS + 1)):
        probe = " ".join(toks[i : i + ANCHOR_WORDS])
        hits |= {n for n, t in normed.items() if probe in t}

    want = figures(fact)
    if want:
        content = [w for w in toks if len(w) > 5 and not FIGURE.fullmatch(w)]
        for n, t in normed.items():
            if want & figures(t) and any(w in t for w in content[:6]):
                hits.add(n)
    return sorted(hits)


def classify(
    fact: str, excerpts: list[str], span: str | None = None
) -> tuple[bool, bool]:
    """(quotes_fact, carries_answer) for the excerpts of the gold page.

    `span` is the verbatim string the answer turns on. Datasets whose facts
    do not turn on a figure supply it explicitly; without it the figure
    heuristic below runs exactly as it always has, which is what keeps the
    financial results reproducible.
    """
    joined = norm(" ".join(excerpts))
    toks = norm(fact).split()
    quotes = any(
        " ".join(toks[i : i + ANCHOR_WORDS]) in joined
        for i in range(max(1, len(toks) - ANCHOR_WORDS + 1))
    )
    if span is not None:
        return quotes, norm(span) in joined
    want = figures(fact)
    carries = bool(want & figures(joined)) if want else quotes
    return quotes, carries


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", default="auto", choices=["auto", "keyword", "semantic"])
    ap.add_argument("--ids", help="comma-separated question ids (default: all)")
    ap.add_argument(
        "--corpus",
        action="store_true",
        help=(
            "search all 24 documents with pdf_corpus_search instead of the"
            " one filing that answers the question. Splits recall failure"
            " into wrong-document and right-document-wrong-page."
        ),
    )
    ap.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA,
        help="dataset directory holding manifest.json and a questions file",
    )
    args = ap.parse_args(argv)
    data = args.data_dir if args.data_dir.is_absolute() else REPO / args.data_dir

    import pdf_mcp.server as server_module

    from pdf_mcp.cache import PDFCache
    from pdf_mcp.server import pdf_corpus_search, pdf_search

    cache = PDFCache(cache_dir=CACHE_DIR, ttl_hours=24 * 30)
    server_module.cache = cache

    manifest = json.loads((data / "manifest.json").read_text())
    path_by_id = {d["id"]: str(REPO / d["path"]) for d in manifest["docs"]}
    questions = load_dataset(data)
    if args.ids:
        keep = set(args.ids.split(","))
        questions = [q for q in questions if q.id in keep]

    corpus_paths = [p for p in path_by_id.values() if Path(p).exists()]

    rows: list[dict[str, Any]] = []
    for q in questions:
        doc = q.doc
        path = path_by_id[doc]
        if args.corpus:
            info = pdf_corpus_search(
                corpus_paths, q.question, mode=args.mode, top_k=TOP_K
            )
            # keep only hits in the document that actually answers it; a hit
            # elsewhere cannot carry the gold page
            all_matches = info.get("matches", [])
            matches = [m for m in all_matches if m.get("path") == path]
            doc_returned = bool(matches)
        else:
            info = pdf_search(path, q.question, mode=args.mode, max_results=TOP_K)
            matches = info.get("matches", [])
            doc_returned = True

        # get_pages_text is an INTERNAL API: 0-indexed. pdf_search returns
        # 1-indexed pages. Convert here, or every comparison below is off by
        # one -- which silently reports a located page as a recall miss.
        raw = cache.get_pages_text(path, list(range(0, MAX_PAGES)))
        pages = {n + 1: t for n, t in raw.items()}
        gold = locate(q.fact, pages)

        got = [m["page"] for m in matches]
        hit = [p for p in gold if p in got]
        # BOTH measures are always reported. They answer different
        # questions and neither is "the" fidelity, so the script does not
        # choose between them:
        #
        #   best  -- does the excerpt for the BEST-RANKED answering page
        #            carry the answer? one page per question, so the
        #            single-document and corpus settings are comparable.
        #   any   -- does ANY returned answering page's excerpt carry it?
        #            closer to what the caller's payload actually offers,
        #            but not comparable across settings: a single-document
        #            search gives all 10 slots to the answering filing
        #            (367 gold pages over 100 questions) while a corpus
        #            search shares 10 across 24 documents (172), so it
        #            gets more attempts at the same question.
        if not gold:
            bucket = bucket_any = "unlocatable"
            quotes = carries = carries_any = False
        elif not hit:
            # In corpus mode, distinguish losing the DOCUMENT from finding
            # the document but ranking the wrong page inside it.
            bucket = "DOC MISS" if (args.corpus and not doc_returned) else "PAGE MISS"
            bucket_any = bucket
            quotes = carries = carries_any = False
        else:
            best = next(m for m in matches if m["page"] in hit)
            ex_best = [
                m.get("excerpt") or "" for m in matches if m["page"] == best["page"]
            ]
            quotes, carries = classify(q.fact, ex_best, q.span)
            bucket = "ok" if carries else "EXCERPT MISS"

            ex_any = [m.get("excerpt") or "" for m in matches if m["page"] in hit]
            _, carries_any = classify(q.fact, ex_any, q.span)
            bucket_any = "ok" if carries_any else "EXCERPT MISS"
        rows.append(
            {
                "id": q.id,
                "type": q.type,
                "doc": doc,
                "gold_pages": gold,
                "gold_rank": next(
                    (i + 1 for i, m in enumerate(matches) if m["page"] in gold), None
                ),
                "bucket": bucket,
                "bucket_any_gold_page": bucket_any,
                "quotes_fact": quotes,
                "carries_figure": carries,
                "carries_figure_any_gold_page": carries_any,
            }
        )

    order = {
        "EXCERPT MISS": 0,
        "PAGE MISS": 1,
        "DOC MISS": 2,
        "unlocatable": 3,
        "ok": 4,
    }
    rows.sort(key=lambda r: (order[r["bucket"]], r["id"]))
    print(f"{'question':32s} {'type':15s} {'gold':>10s} {'rank':>5s}  bucket")
    print("-" * 92)
    for r in rows:
        print(
            f"{r['id']:32s} {r['type']:15s} {str(r['gold_pages'][:2]):>10s}"
            f" {str(r['gold_rank'] or '-'):>5s}  {r['bucket']}"
        )

    n = len(rows)
    setting = "24-doc corpus" if args.corpus else "one filing"
    print(f"\n{n} questions, {setting}, mode={args.mode}")
    labels = ("ok", "EXCERPT MISS", "PAGE MISS", "DOC MISS", "unlocatable")
    counts = {b: sum(1 for r in rows if r["bucket"] == b) for b in order}
    counts_any = {
        b: sum(1 for r in rows if r["bucket_any_gold_page"] == b) for b in order
    }
    print(f"  {'':14s} {'best-ranked page':>18s} {'any gold page':>15s}")
    for b in labels:
        print(f"  {b:14s} {counts[b]:>13d}      {counts_any[b]:>10d}")
    found = n - counts["PAGE MISS"] - counts["DOC MISS"] - counts["unlocatable"]
    print(f"\n  recall (answering page returned) : {found}/{n} = {found/n:.0%}")
    if found:
        print(
            f"  fidelity, best-ranked page       : {counts['ok']}/{found}"
            f" = {counts['ok']/found:.0%}"
        )
        print(
            f"  fidelity, any returned gold page : {counts_any['ok']}/{found}"
            f" = {counts_any['ok']/found:.0%}"
        )
    print(
        f"\n{counts['EXCERPT MISS']} questions (best-ranked) /"
        f" {counts_any['EXCERPT MISS']} (any gold page) have the answer on a"
        "\nretrieved page but not in the excerpt. That is excerpt selection,"
        " not retrieval,\nand it is testable without a judge."
    )
    scope = "corpus" if args.corpus else "singledoc"
    out = data / f"excerpt_fidelity_{scope}_{args.mode}.json"
    out.write_text(json.dumps({"mode": args.mode, "rows": rows}, indent=2) + "\n")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
