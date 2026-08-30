#!/usr/bin/env python
"""
scripts/merge_query_candidates.py

Merge accepted candidates from author_corpus_queries.py into
benchmark_data/corpus_search/queries.json as a dated set, after a second,
independent validation pass.

Checks per label (every one is reported, only the first two reject):
  1. evidence present in the RAW pdfium text of its page (whitespace and
     case folded, the harness's own `contain` rule)      -> reject if not
  2. described: the harness's validate_described_queries rule (enough
     content tokens; at least one token absent from the page; single doc)
                                                        -> reject if not
  3. evidence present in pdf-mcp's EXTRACTED text of that page (cached
     page_text, or a live extraction when the page is not cached)
                                                        -> REPORTED as an
     "extraction finding", never a label defect: the label is right and
     pdf-mcp is not. This is the check that would have caught the
     spanning-line bug months earlier (Trap 9).

Usage:
    python scripts/merge_query_candidates.py --classes described,needle,spread
    python scripts/merge_query_candidates.py --classes described \
        --veto described-31,described-40
    python scripts/merge_query_candidates.py --classes described --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "src"))
DATA = REPO / "benchmark_data" / "corpus_search"

from author_corpus_queries import raw_page_text  # noqa: E402
from benchmark_corpus_modes import validate_described_queries  # noqa: E402

_WS = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ufffe\ufffd]")


def normalize(text: str) -> str:
    return _WS.sub(" ", text).strip().lower()


def contain(context: str, evidence: str) -> bool:
    return evidence in context or normalize(evidence) in normalize(context)


def extracted_page_text(path: Path, page_num_0: int) -> str:
    from pdf_mcp.backend.page import open_document
    from pdf_mcp.extractor import extract_text_from_page
    from pdf_mcp.server import cache

    cached = cache.get_page_text(str(path.resolve()), page_num_0)
    if cached is not None:
        return cached
    doc = open_document(str(path))
    return extract_text_from_page(doc[page_num_0])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--classes", default="described,needle,spread")
    ap.add_argument("--veto", default="", help="comma-separated candidate ids to drop")
    ap.add_argument("--set-name", default=f"v2-{dt.date.today().isoformat()}")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    path_by_id = {d["id"]: REPO / d["path"] for d in manifest["docs"]}
    queries = json.loads((DATA / "queries.json").read_text(encoding="utf-8"))
    existing_ids = {q["id"] for q in queries["queries"]}
    veto = {v.strip() for v in args.veto.split(",") if v.strip()}

    raw_cache: dict[tuple[str, int], str] = {}

    def raw_lookup(doc: str, page: int) -> str:
        key = (doc, page)
        if key not in raw_cache:
            raw_cache[key] = raw_page_text(path_by_id[doc], page - 1)
        return raw_cache[key]

    merged: list[dict] = []
    rejected: list[tuple[str, str]] = []
    findings: list[str] = []
    for klass in [c.strip() for c in args.classes.split(",") if c.strip()]:
        cand_path = DATA / f"candidates_{klass}.json"
        if not cand_path.exists():
            print(f"no candidates file for {klass}: {cand_path}", file=sys.stderr)
            continue
        cands = json.loads(cand_path.read_text(encoding="utf-8"))["accepted"]
        for c in cands:
            if c["id"] in veto:
                rejected.append((c["id"], "vetoed"))
                continue
            if c["id"] in existing_ids:
                rejected.append((c["id"], "id already in queries.json"))
                continue
            bad = None
            for lb in c["labels"]:
                if _CONTROL.search(lb["evidence"]):
                    # a font with no usable ToUnicode map: pdfium emits a
                    # control placeholder for the ligature, every arm's
                    # text differs, no arm can match. Not a valid label.
                    bad = f"{lb['doc']} p{lb['page']}: control character in evidence"
                    break
                if not contain(raw_lookup(lb["doc"], lb["page"]), lb["evidence"]):
                    bad = f"{lb['doc']} p{lb['page']}: evidence not in raw text"
                    break
            if bad:
                rejected.append((c["id"], bad))
                continue
            if klass == "described":
                errs = validate_described_queries({"queries": [c]}, raw_lookup)
                if errs:
                    rejected.append((c["id"], "; ".join(errs)))
                    continue
            for lb in c["labels"]:
                try:
                    ours = extracted_page_text(path_by_id[lb["doc"]], lb["page"] - 1)
                except Exception as exc:  # noqa: BLE001 - report, do not reject
                    findings.append(
                        f"{c['id']} {lb['doc']} p{lb['page']}: extraction error {exc}"
                    )
                    continue
                if not contain(ours, lb["evidence"]):
                    findings.append(
                        f"{c['id']} {lb['doc']} p{lb['page']}: span in raw pdfium text "
                        f"but NOT in pdf-mcp extraction: {lb['evidence'][:70]!r}"
                    )
            entry = {
                "id": c["id"],
                "class": c["class"],
                "query": c["query"],
                "labels": [
                    {k: v for k, v in lb.items() if k != "match"} for lb in c["labels"]
                ],
                "set": args.set_name,
                "provenance": c.get("provenance", {}),
            }
            merged.append(entry)

    print(f"merge: {len(merged)} accepted, {len(rejected)} rejected", file=sys.stderr)
    for cid, why in rejected:
        print(f"  reject {cid}: {why}", file=sys.stderr)
    print(f"extraction findings: {len(findings)}", file=sys.stderr)
    for f in findings:
        print(f"  FINDING {f}", file=sys.stderr)
    by_class: dict[str, int] = {}
    deep = 0
    for m in merged:
        by_class[m["class"]] = by_class.get(m["class"], 0) + 1
        deep += sum(1 for lb in m["labels"] if lb["page"] > 1)
    n_labels = sum(len(m["labels"]) for m in merged)
    print(
        f"  per class {by_class}; labels below page 1: {deep}/{n_labels}",
        file=sys.stderr,
    )
    if args.dry_run:
        return 0
    queries["queries"].extend(merged)
    queries["sets"] = {
        **queries.get("sets", {}),
        args.set_name: {
            "added": dt.date.today().isoformat(),
            "count": len(merged),
            "authoring": "scripts/author_corpus_queries.py, blind to all arms; "
            "merged by scripts/merge_query_candidates.py after raw-text validation",
        },
    }
    (DATA / "queries.json").write_text(
        json.dumps(queries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"queries.json now holds {len(queries['queries'])} queries", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
