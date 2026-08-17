#!/usr/bin/env python3
"""Measure the cost of the ocr_lang cache-key thrash (issue #27).

SPIKE OUTPUT. The question this answers is "what does a user actually pay
when a caller varies the ocr_lang string on the same pages?", so the
metric is wall-clock and cache misses, not retrieval quality.

Three sequences run against a cold, isolated cache:

  baseline     same ocr_lang every call        -> 1 miss, K-1 hits
  case-thrash  'rus+eng' / 'RUS+ENG' alternating
  order-thrash 'rus+eng' / 'eng+rus' alternating

`strip().lower()` on the key would fix case-thrash alone. Only the wider
(file_path, page_num, ocr_lang) primary key fixes order-thrash, because
the two orderings are genuinely different requests to Tesseract.

Needs a tessdata dir holding eng + rus. Point TESSDATA_PREFIX at one:

    mkdir -p /tmp/td && cp /opt/homebrew/share/tessdata/eng.traineddata /tmp/td/
    curl -sLo /tmp/td/rus.traineddata \
      https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/rus.traineddata
    TESSDATA_PREFIX=/tmp/td python scripts/measure_ocr_lang_thrash.py

Exit codes: 0 measured, 2 setup error (missing packs, no Tesseract).
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import tempfile
import time

# A bilingual page: Cyrillic and Latin paragraphs alternating, so both
# language models compete over the whole page rather than over one line.
# Two Latin languages would often produce identical output on a clean
# render, which makes the comparison vacuous.
RU_PARA = (
    "Оптическое распознавание символов на отсканированной странице никогда "
    "не является единственным решением. Механизм разбивает изображение на "
    "строки, строки на слова, а слова на кандидаты символов."
)
EN_PARA = (
    "When two language models are loaded at once, both of them see the same "
    "candidates. A page carrying more than one language forces the recogniser "
    "to arbitrate between competing dictionaries."
)
UNICODE_FONT = "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"


def build_bilingual_scan(path: str, n_pages: int) -> None:
    """Write an image-only PDF with no recoverable text layer."""
    import pymupdf

    if not pathlib.Path(UNICODE_FONT).exists():
        sys.exit(f"[setup] Unicode font not found: {UNICODE_FONT}")

    text_doc = pymupdf.open()
    for _ in range(n_pages):
        page = text_doc.new_page(width=595, height=842)
        page.insert_font(fontname="uni", fontfile=UNICODE_FONT)
        y = 70
        for _ in range(4):
            for para in (RU_PARA, EN_PARA):
                page.insert_textbox(
                    pymupdf.Rect(50, y, 545, y + 80),
                    para,
                    fontname="uni",
                    fontsize=10.5,
                )
                y += 85

    scan = pymupdf.open()
    for page in text_doc:
        pix = page.get_pixmap(dpi=200)
        img_page = scan.new_page(width=page.rect.width, height=page.rect.height)
        img_page.insert_image(page.rect, pixmap=pix)
    scan.save(path)
    text_doc.close()
    scan.close()

    # The measurement is meaningless if PyMuPDF can still read a text layer,
    # because then nothing would take the OCR path at all.
    check = pymupdf.open(path)
    leaked = "".join(p.get_text().strip() for p in check)
    check.close()
    if leaked:
        sys.exit("[setup] scan still carries a text layer; OCR path not forced")


def run_sequence(pdf_path: str, page_spec: str, langs: list[str], calls: int) -> dict:
    """Run `calls` OCR reads, cycling through `langs`, on a cold cache."""
    from pdf_mcp import server as server_module
    from pdf_mcp.cache import PDFCache

    with tempfile.TemporaryDirectory() as cache_dir:
        server_module.cache = PDFCache(cache_dir=pathlib.Path(cache_dir))
        per_call = []
        for i in range(calls):
            lang = langs[i % len(langs)]
            t0 = time.perf_counter()
            result = server_module.pdf_read_pages(
                pdf_path, page_spec, ocr=True, ocr_lang=lang
            )
            elapsed = time.perf_counter() - t0
            if isinstance(result, dict) and result.get("error"):
                sys.exit(f"[setup] tool error: {result['error']}")
            # pdf_read_pages reports hit/miss counts at the top level; there
            # is no per-page from_cache field (it reads back as None).
            misses = int(result.get("cache_misses", 0))
            pages = result.get("pages", [])
            if not pages or any(p.get("source") != "ocr" for p in pages):
                sys.exit("[setup] a page did not take the OCR path")
            per_call.append(
                {
                    "call": i + 1,
                    "ocr_lang": lang,
                    "seconds": round(elapsed, 3),
                    "page_misses": misses,
                    "page_hits": int(result.get("cache_hits", 0)),
                }
            )
        return {
            "calls": per_call,
            "total_seconds": round(sum(c["seconds"] for c in per_call), 3),
            "page_misses": sum(c["page_misses"] for c in per_call),
            "page_hits": sum(c["page_hits"] for c in per_call),
        }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=3, help="pages per document")
    ap.add_argument("--calls", type=int, default=6, help="tool calls per sequence")
    ap.add_argument(
        "--out",
        default="benchmark_data/ocr_lang_thrash_results.json",
        help="where to persist results (never only stdout)",
    )
    args = ap.parse_args()

    if not os.environ.get("TESSDATA_PREFIX"):
        sys.exit("[setup] set TESSDATA_PREFIX to a dir holding eng + rus")
    td = pathlib.Path(os.environ["TESSDATA_PREFIX"])
    missing = [p for p in ("eng", "rus") if not (td / f"{p}.traineddata").exists()]
    if missing:
        sys.exit(f"[setup] missing traineddata in {td}: {', '.join(missing)}")

    from pdf_mcp.extractor import check_tesseract_available

    try:
        check_tesseract_available()
    except Exception as exc:  # noqa: BLE001 - setup probe, report and stop
        sys.exit(f"[setup] Tesseract unavailable: {exc}")

    with tempfile.TemporaryDirectory() as workdir:
        pdf_path = str(pathlib.Path(workdir) / "bilingual_scan.pdf")
        build_bilingual_scan(pdf_path, args.pages)
        page_spec = f"1-{args.pages}"

        sequences = {
            "baseline": ["rus+eng"],
            "case-thrash": ["rus+eng", "RUS+ENG"],
            "order-thrash": ["rus+eng", "eng+rus"],
        }
        results = {}
        for name, langs in sequences.items():
            print(f"running {name} ...", flush=True)
            results[name] = run_sequence(pdf_path, page_spec, langs, args.calls)
            r = results[name]
            print(f"  {r['total_seconds']:>7.2f}s  " f"{r['page_misses']} page-misses")

    payload = {
        "config": {
            "pages": args.pages,
            "calls_per_sequence": args.calls,
            "tessdata_prefix": str(td),
        },
        "results": results,
    }
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2))

    base = results["baseline"]["total_seconds"]
    print("\n" + "=" * 58)
    for name, r in results.items():
        overhead = r["total_seconds"] - base
        print(
            f"{name:14s} {r['total_seconds']:>7.2f}s  "
            f"{r['page_misses']:>3d} page-misses  "
            f"{r['page_hits']:>3d} page-hits  "
            f"{overhead:+7.2f}s vs baseline"
        )
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
