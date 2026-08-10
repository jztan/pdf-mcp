#!/usr/bin/env python
"""
scripts/benchmark_excerpt_quality.py

Directional signal: excerpt_style="paragraph" vs "snippet" quality.

Compares two cells (snippet, paragraph) over a frozen query corpus
across multiple PDFs. Measures excerpt containment rate — whether
the returned excerpt contains a known answer substring.

n~30 queries across 5 PDFs; treat results as a go/no-go signal,
not a publishable benchmark.  Containment is a weak proxy: it
catches wrong-block failures but can't distinguish "right block,
noisy context" from "right block, clean context."

Usage:
    python scripts/benchmark_excerpt_quality.py              # gated run
    python scripts/benchmark_excerpt_quality.py --calibrate  # report only
    python scripts/benchmark_excerpt_quality.py --pdfs transformer,gpt3
    python scripts/benchmark_excerpt_quality.py --output-json results.json

Exit codes: 0 = PASS / calibrate, 1 = FAIL, 2 = setup error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pdf_mcp.server import _resolve_path, pdf_search  # noqa: E402

VALID_CATEGORIES = {"prose", "structured", "table"}
REQUIRED_QUERY_FIELDS = ("id", "category", "query", "page", "answer")
GATED_CELLS = ("snippet", "paragraph")


def bbox_contains_answer(page, bbox, answer: str) -> bool:
    """True if re-extracting the bbox region contains the gold answer.

    Punctuation-normalized: lowercased, hyphens collapsed to spaces,
    whitespace collapsed, so degenerate-encoding PDFs that drop
    "-'\" at clip edges still match.
    """
    clip_text = page.get_text(clip=pymupdf.Rect(bbox))

    def norm(s: str) -> str:
        return " ".join(s.lower().replace("-", " ").split())

    return norm(answer) in norm(clip_text)


def load_queries(path: str) -> dict:
    """Load and validate the frozen query corpus.

    Returns: {pdf_key: {"path"|"url": str, "title": str,
              "queries": [query_dict, ...]}}.
    Raises ValueError on schema violations.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    if "pdfs" not in data:
        raise ValueError("Query file missing top-level 'pdfs' key")

    for pdf_key, pdf_data in data["pdfs"].items():
        if "path" not in pdf_data and "url" not in pdf_data:
            raise ValueError(f"PDF '{pdf_key}' must have 'path' or 'url'")
        if "queries" not in pdf_data:
            raise ValueError(f"PDF '{pdf_key}' must have 'queries'")
        for q in pdf_data["queries"]:
            for field in REQUIRED_QUERY_FIELDS:
                if field not in q:
                    raise ValueError(f"Query {q.get('id', '?')} missing field: {field}")
            if q["category"] not in VALID_CATEGORIES:
                raise ValueError(
                    f"Query {q['id']} has invalid category: {q['category']}"
                )
            kf = q.get("known_fail")
            if kf is not None:
                if not isinstance(kf, dict):
                    raise ValueError(f"Query {q['id']} known_fail must be an object")
                if kf.get("cell") not in GATED_CELLS:
                    raise ValueError(
                        f"Query {q['id']} known_fail.cell must be one of"
                        f" {GATED_CELLS}, got {kf.get('cell')!r}"
                    )
                if not str(kf.get("reason", "")).strip():
                    raise ValueError(
                        f"Query {q['id']} known_fail.reason must be a"
                        " non-empty string explaining the frozen failure"
                    )

    return data["pdfs"]


def _resolve_pdf_path(pdf_data: dict) -> str:
    """Return the path or URL for a PDF entry."""
    if "url" in pdf_data:
        return pdf_data["url"]
    path = pdf_data["path"]
    if not path.startswith("/"):
        path = str(Path(__file__).parent.parent / path)
    return path


def _assert_sha256(local_path: str, pdf_key: str, expected: str) -> None:
    """Fail loudly if a corpus file's bytes are not the graded ones.

    Baselines are only comparable against fixed inputs. A vendor that
    re-issues a datasheet, or a re-download that silently returns
    something else, would otherwise show up as a quality change.
    """
    h = hashlib.sha256()
    with open(local_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    actual = h.hexdigest()
    if actual != expected:
        raise ValueError(
            f"Corpus PDF '{pdf_key}' failed its sha256 check.\n"
            f"  expected {expected}\n  actual   {actual}\n"
            f"  path     {local_path}\n"
            "The graded bytes changed, so its recorded pages and answers"
            " may no longer hold. Re-verify the corpus and update the"
            " digest deliberately; do not just paste the new value."
        )


def _assert_answer_on_page(doc, pdf_key: str, q: dict) -> None:
    """Fail loudly if the graded page cannot contain the answer.

    Without this a wrong or drifted ground-truth page scores 0 and reads
    as a quality regression -- the same trap as a silently missing PDF,
    one level up. It is live risk for URL-fetched vendor datasheets,
    which are re-issued with content on different pages and carry no
    pinned revision.
    """
    page_no = q["page"]
    if not 1 <= page_no <= doc.page_count:
        raise ValueError(
            f"Query {q['id']} ({pdf_key}) grades page {page_no}, but the"
            f" document has {doc.page_count} pages. The document was"
            " probably revised; re-verify the corpus rather than reading"
            " the score."
        )

    def norm(s: str) -> str:
        return " ".join(s.lower().replace("-", " ").split())

    if norm(q["answer"]) not in norm(doc[page_no - 1].get_text()):
        raise ValueError(
            f"Query {q['id']} ({pdf_key}) grades page {page_no}, but the"
            f" answer {q['answer']!r} does not appear anywhere on it. That"
            " is a corpus error, not a retrieval failure; scoring it 0"
            " would misreport it as a quality regression."
        )


def run_all_cells(all_pdfs: dict) -> tuple[dict, list[dict]]:
    """Run snippet vs paragraph over every (pdf, query) pair.

    Also records `bbox_containment`: for the paragraph-style hit on the
    graded page, whether re-extracting the hit's `bbox` region (the
    literal geometry a caller would clip/render) still contains the
    gold answer. This is the geometry-faithfulness counterpart to
    `excerpt_containment` (the paragraph cell's text-only containment
    rate) — it should never be lower, since the bbox is what produced
    the excerpt in the first place.

    Returns:
        cells: {cell_name: {category: containment_rate, "all": rate}}.
               Includes a synthetic "bbox" cell alongside "snippet"
               and "paragraph".
        rows:  per-query detail for the report table.
    """
    CELLS = ("snippet", "paragraph")

    accum: dict[str, dict[str, list[int]]] = {
        c: defaultdict(list) for c in CELLS + ("bbox",)
    }
    rows: list[dict] = []

    for pdf_key, pdf_data in all_pdfs.items():
        pdf_path = _resolve_pdf_path(pdf_data)
        print(f"  {pdf_data.get('title', pdf_key)} ...", flush=True)

        local_path, resolve_err = _resolve_path(pdf_path)
        if local_path is None:
            raise FileNotFoundError(
                f"Corpus PDF '{pdf_key}' could not be resolved: {pdf_path}"
                f" ({resolve_err}). Aborting: an unresolvable PDF scores 0"
                " on every one of its queries, which is indistinguishable"
                " from a total quality collapse."
            )
        expected_sha = pdf_data.get("sha256")
        if expected_sha:
            _assert_sha256(local_path, pdf_key, expected_sha)
        doc = pymupdf.open(local_path)

        for q in pdf_data["queries"]:
            _assert_answer_on_page(doc, pdf_key, q)
            row: dict = {
                "id": q["id"],
                "pdf": pdf_key,
                "query": q["query"],
                "page": q["page"],
                "category": q["category"],
                "known_fail": q.get("known_fail"),
            }

            for style in CELLS:
                r = pdf_search(
                    pdf_path,
                    q["query"],
                    excerpt_style=style,
                    max_results=5,
                )
                matches = r.get("matches", [])
                target = next((m for m in matches if m["page"] == q["page"]), None)

                if target is None:
                    contains = 0
                    excerpt_len = 0
                else:
                    excerpt = target["excerpt"]
                    contains = 1 if q["answer"].lower() in excerpt.lower() else 0
                    excerpt_len = len(excerpt)

                accum[style][q["category"]].append(contains)
                row[f"{style}_contains"] = contains
                row[f"{style}_len"] = excerpt_len

                if style == "paragraph":
                    bbox = target.get("bbox") if target else None
                    bbox_present = 1 if (target is not None and bbox is not None) else 0
                    if bbox is not None and doc is not None:
                        page = doc[q["page"] - 1]
                        bbox_contains = (
                            1 if bbox_contains_answer(page, bbox, q["answer"]) else 0
                        )
                    else:
                        bbox_contains = 0
                    accum["bbox"][q["category"]].append(bbox_contains)
                    row["bbox_contains"] = bbox_contains
                    row["bbox_present"] = bbox_present

            rows.append(row)

        if doc is not None:
            doc.close()

    cells: dict[str, dict[str, float]] = {}
    for cell in CELLS + ("bbox",):
        cell_out: dict[str, float] = {}
        all_vals: list[int] = []
        for cat in sorted(VALID_CATEGORIES):
            vals = accum[cell][cat]
            cell_out[cat] = sum(vals) / len(vals) if vals else 0.0
            all_vals.extend(vals)
        cell_out["all"] = sum(all_vals) / len(all_vals) if all_vals else 0.0
        cells[cell] = cell_out

    return cells, rows


def _frozen_for(row: dict, cell: str) -> bool:
    """True if this row's failure in `cell` is a recorded known failure."""
    kf = row.get("known_fail")
    return bool(kf) and kf.get("cell") == cell


def evaluate_gate(cells: dict, rows: list[dict]) -> dict:
    """Evaluate the three-clause gate.

    Clause 1: paragraph overall containment >= snippet.
    Clause 2: zero regressions (no query where snippet contains
              answer but paragraph doesn't).
    Clause 3: bbox fidelity, scoped to hits that actually carry a bbox.
              A hit with no bbox (e.g. the answer lives in a block that
              exceeds the excerpt char cap, so the picker legitimately
              falls back to the raw snippet with no block selected)
              makes no geometry claim at all, so it must not count
              against geometry fidelity. Scoring it as a "bbox failure"
              would conflate "no geometry claim" with "geometry lost
              information" — the global bbox_containment aggregate has
              exactly that flaw, which is why it stays a reported
              transparency metric only, not the gate.
    """
    # Clause 1 is scoped to live (unfrozen) rows, matching clause 2. A
    # frozen row scores 0 on paragraph by definition and cannot regress
    # further, so averaging it in cannot detect anything -- it only holds
    # the gate red at a fixed offset. The report still prints the honest
    # all-rows `excerpt_containment`, and that is the published baseline.
    live = [r for r in rows if not _frozen_for(r, "paragraph")]
    n_live = len(live)
    live_paragraph = (
        sum(r["paragraph_contains"] for r in live) / n_live if n_live else 0.0
    )
    live_snippet = sum(r["snippet_contains"] for r in live) / n_live if n_live else 0.0
    clause_1_pass = live_paragraph >= live_snippet

    regressions = [
        r
        for r in rows
        if r["snippet_contains"] == 1
        and r["paragraph_contains"] == 0
        and not _frozen_for(r, "paragraph")
    ]
    clause_2_pass = len(regressions) == 0

    # The ratchet only tightens: a frozen row that now passes must be
    # un-frozen, or the next real regression there would be invisible.
    stale = [
        r for r in rows if _frozen_for(r, "paragraph") and r["paragraph_contains"] == 1
    ]
    clause_4_pass = len(stale) == 0

    bbox_rows = [r for r in rows if r.get("bbox_present") == 1]
    scoped_bbox = sum(r["bbox_contains"] for r in bbox_rows)
    scoped_excerpt = sum(r["paragraph_contains"] for r in bbox_rows)
    # Require at least one bbox-present hit: an empty subset would make the
    # inequality vacuously true (0 >= 0), silently passing if a regression
    # ever zeroed bbox emission across the whole corpus.
    clause_3_pass = len(bbox_rows) > 0 and scoped_bbox >= scoped_excerpt

    return {
        "pass": clause_1_pass and clause_2_pass and clause_3_pass and clause_4_pass,
        "clause_1_containment": {
            "pass": clause_1_pass,
            "snippet": live_snippet,
            "paragraph": live_paragraph,
            "n_live": n_live,
            "all_rows_paragraph": cells["paragraph"]["all"],
            "all_rows_snippet": cells["snippet"]["all"],
        },
        "clause_2_regressions": {
            "pass": clause_2_pass,
            "count": len(regressions),
            "ids": [r["id"] for r in regressions],
        },
        "clause_3_bbox_fidelity": {
            "pass": clause_3_pass,
            "scoped_bbox": scoped_bbox,
            "scoped_excerpt": scoped_excerpt,
            "n_bbox_present": len(bbox_rows),
        },
        "clause_4_stale_known_fail": {
            "pass": clause_4_pass,
            "count": len(stale),
            "ids": [r["id"] for r in stale],
            "action": "un-freeze these rows: delete their known_fail block",
        },
    }


def print_report(cells: dict, rows: list[dict], all_pdfs: dict) -> None:
    n = len(rows)
    pdf_count = len(all_pdfs)

    print()
    print("=" * 78)
    print("Excerpt quality: paragraph vs snippet containment rate")
    print("=" * 78)

    # Per-cell summary
    cats = ("prose", "structured", "table", "all")
    print(f"\n{'cell':<14}" + "".join(f"{c:>14}" for c in cats))
    for cell, scores in cells.items():
        row_str = f"{cell:<14}" + "".join(f"{scores.get(c, 0):>13.0%} " for c in cats)
        print(row_str)

    # Per-query detail
    print(
        f"\n{'ID':<6} {'PDF':<12} {'Query':<42} {'Pg':>3}"
        f"  {'Cat':<10} {'Snip':>4} {'Para':>4}"
        f"  {'S.len':>5} {'P.len':>5}"
    )
    print("-" * 104)

    for r in rows:
        s_mark = "Y" if r["snippet_contains"] else "N"
        p_mark = "Y" if r["paragraph_contains"] else "N"
        print(
            f"{r['id']:<6} {r['pdf']:<12} {r['query']:<42} {r['page']:>3}"
            f"  {r['category']:<10} {s_mark:>4} {p_mark:>4}"
            f"  {r['snippet_len']:>5} {r['paragraph_len']:>5}"
        )

    # Length distribution
    print()
    for style in ("snippet", "paragraph"):
        lengths = sorted(r[f"{style}_len"] for r in rows if r[f"{style}_len"] > 0)
        if not lengths:
            continue
        avg = sum(lengths) / len(lengths)
        buckets = {"<100": 0, "100-299": 0, "300-499": 0, "500-999": 0, "1000+": 0}
        for length in lengths:
            if length < 100:
                buckets["<100"] += 1
            elif length < 300:
                buckets["100-299"] += 1
            elif length < 500:
                buckets["300-499"] += 1
            elif length < 1000:
                buckets["500-999"] += 1
            else:
                buckets["1000+"] += 1
        dist = "  ".join(f"{k}:{v}" for k, v in buckets.items() if v > 0)
        print(f"  {style:<10} avg={avg:.0f}  {dist}")

    # Per-PDF breakdown
    print()
    pdf_keys = list(dict.fromkeys(r["pdf"] for r in rows))
    for pk in pdf_keys:
        s_items = [r for r in rows if r["pdf"] == pk]
        s_rate = sum(r["snippet_contains"] for r in s_items) / len(s_items)
        p_rate = sum(r["paragraph_contains"] for r in s_items) / len(s_items)
        title = all_pdfs[pk].get("title", pk)
        print(f"  {title}: snippet {s_rate:.0%}  paragraph {p_rate:.0%}")

    # Head-to-head
    wins = sum(1 for r in rows if r["paragraph_contains"] and not r["snippet_contains"])
    losses = sum(
        1 for r in rows if r["snippet_contains"] and not r["paragraph_contains"]
    )
    ties = sum(1 for r in rows if r["snippet_contains"] == r["paragraph_contains"])
    print(f"\n  Queries: {n} across {pdf_count} PDF(s).")
    print(
        f"  Head-to-head: paragraph wins {wins}," f" snippet wins {losses}, ties {ties}"
    )

    excerpt_containment = cells["paragraph"]["all"]
    bbox_containment = cells["bbox"]["all"]
    print(
        f"\n  excerpt_containment={excerpt_containment:.3f}"
        f"  bbox_containment={bbox_containment:.3f}"
    )


def print_gate_verdict(verdict: dict) -> None:
    print()
    print("=" * 60)
    print(f"GATE VERDICT: {'PASS' if verdict['pass'] else 'FAIL'}")
    print("=" * 60)
    for clause_key in (
        "clause_1_containment",
        "clause_2_regressions",
        "clause_3_bbox_fidelity",
        "clause_4_stale_known_fail",
    ):
        c = verdict[clause_key]
        marker = "✓" if c["pass"] else "✗"
        detail = {k: v for k, v in c.items() if k != "pass"}
        print(f"  {marker} {clause_key}: {detail}")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Excerpt quality: paragraph vs snippet containment"
    )
    p.add_argument(
        "--calibrate",
        action="store_true",
        help="Print numbers, no PASS/FAIL gating.",
    )
    p.add_argument(
        "--pdfs",
        default="",
        help="Comma-separated PDF keys (default: all).",
    )
    p.add_argument(
        "--categories",
        default="",
        help="Comma-separated categories (default: all).",
    )
    p.add_argument(
        "--output-json",
        default="",
        help="Write structured results to this path.",
    )
    p.add_argument(
        "--queries",
        default="benchmark_data/excerpt_quality_queries.json",
        help="Path to the query corpus file.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    """Returns exit code: 0 PASS / calibrate, 1 FAIL, 2 setup error."""
    args = _build_parser().parse_args(argv)

    try:
        all_pdfs = load_queries(args.queries)
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if args.pdfs:
        keep = set(args.pdfs.split(","))
        all_pdfs = {k: v for k, v in all_pdfs.items() if k in keep}
    if args.categories:
        cats = set(args.categories.split(","))
        for v in all_pdfs.values():
            v["queries"] = [q for q in v["queries"] if q["category"] in cats]

    total_q = sum(len(v["queries"]) for v in all_pdfs.values())
    if total_q == 0:
        print(
            "ERROR: no queries loaded — query file is empty or filters "
            "excluded everything.",
            file=sys.stderr,
        )
        return 2

    print(f"Running excerpt quality benchmark ({total_q} queries)...\n")
    try:
        cells, rows = run_all_cells(all_pdfs)
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print_report(cells, rows, all_pdfs)

    if args.output_json:
        with open(args.output_json, "w") as f:
            json.dump(
                {"cells": cells, "rows": rows},
                f,
                indent=2,
                default=str,
            )

    if args.calibrate:
        print("\n[--calibrate] Skipping gate. No exit-code gating.")
        return 0

    verdict = evaluate_gate(cells, rows)
    print_gate_verdict(verdict)
    return 0 if verdict["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
