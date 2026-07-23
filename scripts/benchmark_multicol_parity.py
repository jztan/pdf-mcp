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
            + "\n"
        )
        vals = sorted(pages.values())
        print(
            f"baseline: {len(pages)} multi-column pages,"
            f" min={vals[0]:.3f} median={vals[len(vals)//2]:.3f}"
        )
        return 0

    if args.check:
        base = json.loads(BASELINE.read_text())["pages"]
        regressed, improved = [], []
        for k, v in pages.items():
            b = base.get(k)
            if b is None:
                continue
            if v < b - DROP_TOL:
                regressed.append((k, b, v))
            elif v > b + DROP_TOL:
                improved.append((k, b, v))
        for k, b, v in sorted(regressed):
            print(f"REGRESSED {k}: {b:.4f} -> {v:.4f}")
        for k, b, v in sorted(improved):
            print(f"improved  {k}: {b:.4f} -> {v:.4f}")
        print(
            f"{len(pages)} pages checked: {len(regressed)} regressed,"
            f" {len(improved)} improved"
        )
        return 1 if regressed else 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
