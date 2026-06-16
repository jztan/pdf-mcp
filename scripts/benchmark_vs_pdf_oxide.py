#!/usr/bin/env python
"""
scripts/benchmark_vs_pdf_oxide.py

Text-extraction benchmark: pdf-mcp's stack (PyMuPDF) vs. pdf_oxide (Rust).

pdf_oxide (https://github.com/yfedoseev/pdf_oxide) is a pure-Rust PDF toolkit
with Python bindings that advertises "5x faster than industry leaders". This
script puts that claim next to pdf-mcp's actual extraction code path on the
SAME PDFs, measuring full-document text extraction end to end (open + extract
every page + join), cold on each timed iteration.

It times four engines:

  - pdf-mcp (reading-order) : our production `pdf_read_all` path —
        extractor.extract_text_from_page(page, sort_by_position=True). With the
        `multicolumn` extra installed this runs column detection per page so
        multi-column text is not interleaved; without it, it falls back to a
        positional block sort.
  - pdf-mcp (raw)           : bare page.get_text() — the PyMuPDF floor, no
        reading-order work. This is the apples-to-apples engine comparison
        against pdf_oxide's plain text property.
  - pdf_oxide               : PdfDocument(path)[i].text joined over all pages.
  - pdfminer.six            : high_level.extract_text(path) — the pure-Python
        reference extractor, included as a familiar slow baseline.

Fairness notes:
  - Char counts are reported per engine; the comparison is only meaningful when
    they are close (both extracting essentially the same string). pdf_oxide and
    PyMuPDF agree to within ~0.2% on the synthetic corpus here.
  - Each engine is warmed up once before timing so one-time costs (onnxruntime
    model load for column detection, Rust lib init) do not pollute the numbers.
  - Cross-language (Rust vs Python/PyMuPDF-C); read the ratios as directional.
  - Not measured here: pdf-mcp's SQLite page cache, which makes a re-read of the
    same path effectively free (see benchmark_data/vs_pdf_reader_mcp_results.md
    for warm numbers). pdf_oxide has no persistent cache.

Test corpus is generated deterministically (no network) from the demo sample
content, at several page counts, so runs are reproducible anywhere.

Usage:
    python scripts/benchmark_vs_pdf_oxide.py
    python scripts/benchmark_vs_pdf_oxide.py --pages 15 26 75 216 --runs 7
    python scripts/benchmark_vs_pdf_oxide.py --pdf /path/to/real.pdf
    python scripts/benchmark_vs_pdf_oxide.py --output OUT.md
    python scripts/benchmark_vs_pdf_oxide.py --tategaki   # vertical-Japanese test

Besides raw speed, `--tategaki` runs a correctness check: it builds an authentic
vertical-Japanese (縦書き) PDF and scores how well each engine reconstructs the
top-to-bottom, right-to-left reading order — the one axis where the engines
genuinely diverge (only pdfminer.six's `detect_vertical` gets it right).

Requires: pdf-mcp installed (this repo) and `pip install pdf_oxide pdfminer.six`
(the `--tategaki` test additionally needs `reportlab`).
"""

from __future__ import annotations

import argparse
import difflib
import re
import statistics
import sys
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable

import pymupdf

# Reuse the deterministic demo content so the corpus is realistic and stable.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate_demo_sample_pdf import _PAGE_RECT, _MARGIN, _page_blocks  # noqa: E402

from pdf_mcp.extractor import (  # noqa: E402
    column_detection_available,
    extract_text_from_page,
)


def generate_pdf(out_path: Path, page_count: int) -> None:
    """Deterministic text PDF of `page_count` pages, reusing demo prose."""
    doc = pymupdf.open()
    try:
        rect = pymupdf.Rect(
            _MARGIN,
            _MARGIN,
            _PAGE_RECT.width - _MARGIN,
            _PAGE_RECT.height - _MARGIN,
        )
        for i in range(page_count):
            page = doc.new_page(width=_PAGE_RECT.width, height=_PAGE_RECT.height)
            page.insert_textbox(
                rect,
                "\n\n".join(_page_blocks(i)),
                fontsize=10,
                fontname="helv",
                align=pymupdf.TEXT_ALIGN_JUSTIFY,
            )
        doc.set_metadata(
            {
                "title": f"Benchmark corpus ({page_count} pages)",
                "author": "pdf-mcp benchmark",
                "creationDate": "D:20260101000000Z",
                "modDate": "D:20260101000000Z",
            }
        )
        doc.save(str(out_path), garbage=4, deflate=True)
    finally:
        doc.close()


# --- extraction engines: each takes a path, returns the full document text ---


def extract_ours_reading_order(path: str) -> str:
    doc = pymupdf.open(path)
    try:
        return "\n\n".join(
            extract_text_from_page(doc[i], sort_by_position=True)
            for i in range(len(doc))
        )
    finally:
        doc.close()


def extract_ours_raw(path: str) -> str:
    doc = pymupdf.open(path)
    try:
        return "\n\n".join(doc[i].get_text() for i in range(len(doc)))
    finally:
        doc.close()


def extract_pdf_oxide(path: str) -> str:
    from pdf_oxide import PdfDocument

    with PdfDocument(path) as doc:
        return "\n\n".join(doc[i].text for i in range(len(doc)))


def extract_pdfminer(path: str) -> str:
    from pdfminer.high_level import extract_text

    return extract_text(path)


def time_engine(
    fn: Callable[[str], str], path: str, runs: int
) -> tuple[float, float, int]:
    """Warmup once, then time `runs` cold extractions. Returns (min, median, chars)."""
    chars = len(fn(path))  # warmup (also captures output size)
    samples = []
    for _ in range(runs):
        start = time.perf_counter()
        fn(path)
        samples.append(time.perf_counter() - start)
    return min(samples), statistics.median(samples), chars


ENGINES: list[tuple[str, Callable[[str], str]]] = [
    ("pdf-mcp (reading-order)", extract_ours_reading_order),
    ("pdf-mcp (raw)", extract_ours_raw),
    ("pdf_oxide", extract_pdf_oxide),
    ("pdfminer.six", extract_pdfminer),
]


# --- tategaki (vertical Japanese 縦書き) reading-order correctness ---

# Three short columns laid out right-to-left; correct reading order is this list
# (rightmost column first). They are drawn into the PDF in a SCRAMBLED content-
# stream order, so an extractor must use geometry (vertical writing mode + RTL
# column order) — not luck of stream order — to recover the reading order.
_TATEGAKI_COLUMNS = [
    "これは縦書きの日本語です。",  # rightmost column — read first
    "右から左へ読みます。",
    "正しい順序を確認する。",  # leftmost column — read last
]
_TATEGAKI_GT = "".join(_TATEGAKI_COLUMNS)


def build_tategaki_pdf(out_path: Path) -> None:
    """Authentic 縦書き PDF via reportlab + Adobe-Japan1 vertical CMap."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    name = "HeiseiMin-W3"
    pdfmetrics.registerFont(UnicodeCIDFont(name, isVertical=True))
    width, height = A4
    c = canvas.Canvas(str(out_path), pagesize=A4)
    c.setFont(name, 18)
    xs = [width - 60 - 40 * i for i in range(len(_TATEGAKI_COLUMNS))]
    for idx in (1, 2, 0):  # scrambled draw order vs. right-to-left positions
        c.drawString(xs[idx], height - 60, _TATEGAKI_COLUMNS[idx])
    c.showPage()
    c.save()


def extract_pdfminer_vertical(path: str) -> str:
    from pdfminer.high_level import extract_text
    from pdfminer.layout import LAParams

    return extract_text(path, laparams=LAParams(detect_vertical=True))


def _norm_cjk(s: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", s))


def _reading_order_accuracy(text: str) -> float:
    """Similarity of extracted char order to the ground-truth reading order."""
    return difflib.SequenceMatcher(
        None, _norm_cjk(_TATEGAKI_GT), _norm_cjk(text)
    ).ratio()


TATEGAKI_ENGINES: list[tuple[str, Callable[[str], str]]] = [
    ("pdf-mcp (reading-order)", extract_ours_reading_order),
    ("pdf-mcp (raw)", extract_ours_raw),
    ("pdf_oxide", extract_pdf_oxide),
    ("pdfminer.six", extract_pdfminer),
    ("pdfminer.six detect_vertical", extract_pdfminer_vertical),
]


def run_tategaki() -> str:
    """Vertical-Japanese reading-order correctness comparison; returns markdown."""
    try:
        import reportlab  # noqa: F401
    except ImportError:
        sys.exit("tategaki test needs reportlab — run: pip install reportlab")

    tmp = Path(tempfile.mkdtemp(prefix="tategaki_"))
    pdf = tmp / "tategaki.pdf"
    try:
        build_tategaki_pdf(pdf)
    except Exception as exc:  # missing Adobe-Japan1 CMap resources, etc.
        sys.exit(f"could not build vertical Japanese PDF: {exc}")

    lines = [
        "## Tategaki 縦書き — vertical-Japanese reading-order correctness",
        "",
        "Authentic vertical PDF (reportlab + Adobe-Japan1 `UniJIS-UCS2-V` CMap):",
        "three right-to-left columns emitted to the content stream in *scrambled*",
        "order, so recovering the reading order needs real vertical layout",
        "analysis, not stream order. Score = char-order similarity to ground truth.",
        "",
        f"Ground truth: `{_TATEGAKI_GT}`",
        "",
        "| engine | reading-order accuracy | output |",
        "|--------|-----------------------:|--------|",
    ]
    for name, fn in TATEGAKI_ENGINES:
        out = fn(str(pdf))
        acc = _reading_order_accuracy(out)
        mark = " ✅" if acc > 0.999 else ""
        lines.append(f"| {name} | {acc * 100:.0f}%{mark} | `{_norm_cjk(out)}` |")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pages",
        type=int,
        nargs="+",
        default=[15, 26, 75, 216],
        help="page counts for the generated corpus",
    )
    parser.add_argument("--runs", type=int, default=5, help="timed runs per engine")
    parser.add_argument(
        "--pdf",
        type=Path,
        action="append",
        default=None,
        help="use a real PDF instead of (or in addition to) the generated corpus",
    )
    parser.add_argument("--output", type=Path, help="write markdown report to FILE")
    parser.add_argument(
        "--tategaki",
        action="store_true",
        help="run the vertical-Japanese reading-order correctness test instead",
    )
    args = parser.parse_args()

    try:
        import pdf_oxide  # noqa: F401
        import pdfminer  # noqa: F401
    except ImportError as exc:
        sys.exit(
            f"missing dependency ({exc.name}) — run: pip install pdf_oxide pdfminer.six"
        )

    if args.tategaki:
        report = run_tategaki()
        print(report)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(report + "\n")
            print(f"\nWrote {args.output}")
        return

    tmp = Path(tempfile.mkdtemp(prefix="vs_oxide_"))
    targets: list[tuple[str, Path]] = []
    for n in args.pages:
        p = tmp / f"corpus_{n}p.pdf"
        generate_pdf(p, n)
        targets.append((f"synthetic {n}p", p))
    for real in args.pdf or []:
        if not real.exists():
            sys.exit(f"PDF not found: {real}")
        targets.append((real.name, real))

    # rows[(label)] = {engine: (min, median, chars, pages)}
    table: list[dict[str, Any]] = []
    for label, path in targets:
        pages = len(pymupdf.open(str(path)))
        row: dict[str, Any] = {"label": label, "pages": pages, "engines": {}}
        for name, fn in ENGINES:
            mn, md, chars = time_engine(fn, str(path), args.runs)
            row["engines"][name] = (mn * 1e3, md * 1e3, chars)
        table.append(row)

    # ---- render ----
    lines: list[str] = []
    lines.append("# pdf-mcp vs pdf_oxide — text-extraction benchmark")
    lines.append("")
    lines.append(
        f"- Runs: best-of-{args.runs} (warmup + min), cold full-document extraction."
    )
    lines.append(
        f"- column detection (multicolumn extra) available: "
        f"**{column_detection_available()}**"
    )
    lines.append(
        f"- PyMuPDF {pymupdf.__version__.split(' ')[0]} | "
        f"pdf_oxide {pdf_oxide.__version__}"
    )
    lines.append("- Reproduce: `python scripts/benchmark_vs_pdf_oxide.py`")
    lines.append("")
    lines.append("## Full-document text extraction (min ms, best-of-N)")
    lines.append("")
    header = "| PDF (pages) |"
    sep = "|-------------|"
    for name, _ in ENGINES:
        header += f" {name} | chars |"
        sep += "------:|------:|"
    lines.append(header)
    lines.append(sep)
    for row in table:
        cells = f"| {row['label']} ({row['pages']}) |"
        for name, _ in ENGINES:
            mn, _md, chars = row["engines"][name]
            cells += f" {mn:.1f} ms | {chars} |"
        lines.append(cells)
    lines.append("")

    # speedup of pdf_oxide vs each of our paths (min ms)
    lines.append("## pdf_oxide speedup vs pdf-mcp (min ms ratio)")
    lines.append("")
    lines.append("| PDF (pages) | vs reading-order | vs raw |")
    lines.append("|-------------|-----------------:|-------:|")
    for row in table:
        ox = row["engines"]["pdf_oxide"][0]
        ro = row["engines"]["pdf-mcp (reading-order)"][0]
        raw = row["engines"]["pdf-mcp (raw)"][0]
        lines.append(
            f"| {row['label']} ({row['pages']}) | "
            f"{ro / ox:.2f}x | {raw / ox:.2f}x |"
        )
    lines.append("")

    report = "\n".join(lines)
    print(report)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report + "\n")
        print(f"\nWrote {args.output}")


if __name__ == "__main__":
    main()
