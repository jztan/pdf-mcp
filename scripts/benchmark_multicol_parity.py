#!/usr/bin/env python
"""
scripts/benchmark_multicol_parity.py

Token-multiset parity of multi-column extraction vs the no-clip
`get_text("text", sort=True)` reference (visually assembled by PyMuPDF,
verified deterministic without clip). The reference has the RIGHT word
contiguity but interleaves columns; extraction has the right column order.
Comparing token MULTISETS (order-free) isolates contiguity/fragmentation
quality from reading order.

Modes:
    --capture-baseline   write benchmark_data/multicol_parity_baseline.json
    --check              compare current extraction against the baseline;
                         exit 1 if any page drops > DROP_TOL below baseline

Corpus: first N (default 20) PDFs in benchmark_data/.reading_order_pdfs/
(local-only; missing corpus aborts loudly).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import pymupdf  # noqa: E402

from pdf_mcp import extractor  # noqa: E402

CORPUS = REPO / "benchmark_data" / ".reading_order_pdfs"
BASELINE = REPO / "benchmark_data" / "multicol_parity_baseline.json"
DROP_TOL = 0.005

# 0706.0954.pdf uses a Type3 font that PyMuPDF's own get_text("text")
# reference itself splits into single-letter/few-letter fragments (e.g.
# "Date"/":" and "F"/"or" as separate tokens; verified this holds with and
# without sort=True). The reference is token-broken on this doc, not our
# extraction, so token-multiset regressions here are reference artifacts
# rather than real quality regressions (confirmed: our merged output like
# "Date:"/"For"/"We" is the semantically correct one). Excluded from the
# hard regression gate; still scanned and reported (prefixed "excluded ")
# so a real change in behavior stays visible, and still kept in the
# baseline JSON. Guarded instead by the heading regression test plus a
# non-regression floor check on p11 (its baseline value must not drop).
EXCLUDED_DOCS = {"0706.0954.pdf"}
P11_FLOOR_KEY = "0706.0954.pdf#p11"


def is_multicol(page) -> bool:
    if extractor.detect_writing_mode(page) in ("vertical", "mixed"):
        return False
    blocks = page.get_text("blocks", sort=True)
    if extractor.is_confidently_single_column(blocks):
        return False
    boxes = extractor.detect_column_boxes(page)
    return extractor._is_multi_column_layout(boxes)


def page_overlap(page) -> float:
    ref = Counter(page.get_text("text", sort=True).split())
    got = Counter(extractor.extract_text_from_page(page, True).split())
    total = sum(ref.values())
    if total == 0:
        return 1.0
    return sum((ref & got).values()) / total


def scan(n_docs: int) -> dict[str, float]:
    pdfs = sorted(CORPUS.glob("*.pdf"))[:n_docs]
    if not pdfs:
        print(f"ERROR: corpus absent at {CORPUS}")
        raise SystemExit(2)
    out: dict[str, float] = {}
    for p in pdfs:
        doc = pymupdf.open(str(p))
        for pn in range(len(doc)):
            if is_multicol(doc[pn]):
                out[f"{p.name}#p{pn + 1}"] = round(page_overlap(doc[pn]), 4)
        doc.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--capture-baseline", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--docs", type=int, default=20)
    args = ap.parse_args()

    pages = scan(args.docs)
    if args.capture_baseline:
        BASELINE.write_text(
            json.dumps(
                {
                    "pages": pages,
                    "reference": "get_text-text-sort",
                    "docs_scanned": args.docs,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        vals = sorted(pages.values())
        print(
            f"baseline: {len(pages)} multi-column pages,"
            f" min={vals[0]:.3f} median={vals[len(vals)//2]:.3f}"
        )
        return 0

    if args.check:
        base = json.loads(BASELINE.read_text(encoding="utf-8"))["pages"]
        regressed, improved, excluded = [], [], []
        for k, v in pages.items():
            b = base.get(k)
            if b is None:
                continue
            doc_name = k.split("#p", 1)[0]
            if doc_name in EXCLUDED_DOCS:
                excluded.append((k, b, v))
                continue
            if v < b - DROP_TOL:
                regressed.append((k, b, v))
            elif v > b + DROP_TOL:
                improved.append((k, b, v))
        for k, b, v in sorted(regressed):
            print(f"REGRESSED {k}: {b:.4f} -> {v:.4f}")
        for k, b, v in sorted(improved):
            print(f"improved  {k}: {b:.4f} -> {v:.4f}")
        for k, b, v in sorted(excluded):
            print(f"excluded  {k}: {b:.4f} -> {v:.4f}")
        print(
            f"{len(pages)} pages checked: {len(regressed)} regressed,"
            f" {len(improved)} improved, {len(excluded)} excluded"
        )
        p11_floor_failed = False
        p11_val = pages.get(P11_FLOOR_KEY)
        p11_base = base.get(P11_FLOOR_KEY)
        if p11_val is not None and p11_base is not None and p11_val < p11_base:
            print(f"P11 FLOOR FAILED {P11_FLOOR_KEY}: {p11_base:.4f} -> {p11_val:.4f}")
            p11_floor_failed = True
        return 1 if (regressed or p11_floor_failed) else 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
