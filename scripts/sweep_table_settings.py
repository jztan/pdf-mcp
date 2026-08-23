"""Two-sided sweep for the pdfplumber table-detection settings.

Run: uv run python scripts/sweep_table_settings.py

Free, deterministic, no LLM judge. Exists because the excerpt-quality
gate scores table RECALL only and is structurally blind to
over-detection: it grades 42 queries on 18 PDFs and never looks at the
pages where a detector invents a table out of prose. Tuning against it
alone produced a config that scored well and shipped 27 pages of
shredded-prose junk.

  RECALL    of the 42 graded table queries, how many have their answer
            inside some detected table's region on the graded page
  JUNK      pages in pages/corpus where PyMuPDF finds no table at all
            and pdfplumber invents one

Reference points, measured 2026-08-22, through the real pipeline:

  PyMuPDF (detector shipped today)       recall 42/42
  pdfplumber backend                     recall 42/42, junk 1

Raw pdfplumber output, before extract_tables_from_page's merger:

  PyMuPDF                                recall 41/42
  ruled lines only                       recall 33/42, junk 0
  shipped config                         recall 40/42, junk 1

Both numbers matter. A change that moves one must report the other.
"""

from __future__ import annotations

import glob
import json
import re
import warnings

warnings.filterwarnings("ignore")

import pdfplumber  # noqa: E402
import pymupdf  # noqa: E402

QUERIES = json.load(open("benchmark_data/table_detection_queries.json"))
EXTRACT = {"x_tolerance": 1.5}
DIGIT = re.compile(r"\d")

#: The shipped fallback, mirrored from pdf_mcp.backend.tables.
SHIPPED_FALLBACK = {
    "vertical_strategy": "text",
    "horizontal_strategy": "lines",
    "text_x_tolerance": 1,
    "min_words_vertical": 8,
}
SHIPPED_MIN_NUMERIC = 0.5
SHIPPED_MIN_ROWS = 2


def numeric_fraction(cells: list[list[str | None]]) -> float:
    values = [str(c) for row in cells for c in row if c and str(c).strip()]
    if not values:
        return 0.0
    return sum(1 for v in values if DIGIT.search(v)) / len(values)


def is_a_table(cells: list[list[str | None]], min_rows: int) -> bool:
    if len(cells) < min_rows:
        return False
    if max((len(row) for row in cells), default=0) < 2:
        return False
    return sum(1 for row in cells for c in row if c and str(c).strip()) >= 2


def detect(page, fallback, min_numeric, min_rows):
    """Ruled lines first; the fallback only when they see nothing."""

    def run(settings, numeric_floor):
        out = []
        found = page.find_tables(settings) if settings else page.find_tables()
        for table in found:
            cells = table.extract(**EXTRACT)
            if not is_a_table(cells, min_rows):
                continue
            if numeric_floor and numeric_fraction(cells) < numeric_floor:
                continue
            out.append(table)
        return out

    got = run(None, 0.0)
    if not got and fallback is not None:
        got = run(fallback, min_numeric)
    return got


def _normalise(text: str) -> str:
    """Collapse whitespace and drop thousands separators.

    Without the whitespace collapse this metric reported a miss on
    fed-consumer-context p6, where both engines detect the right table
    and the answer sits inside it, but the page wraps the line as
    "1.99%\\nto 8.99%" so a literal substring match fails. That was a
    harness bug being read as a detection failure.
    """
    return " ".join(text.replace(",", "").split())


def answer_in_region(mu_page, bbox, answer: str) -> bool:
    text = mu_page.get_text(clip=pymupdf.Rect(*bbox))
    return _normalise(answer) in _normalise(text)


def score_pipeline() -> tuple[int, int, int]:
    """Score the SHIPPED config through the real extraction pipeline.

    detect() above stops at pdfplumber's raw output, which is blind to
    extract_tables_from_page's _merge_single_row_detections. That merger
    reassembles financial statements split into one detection per row,
    so a raw-output metric scored Starbucks p34 as a miss when the real
    pipeline resolves it.
    """
    from pdf_mcp.backend.tables import open_table_page
    from pdf_mcp.extractor import extract_tables_from_page

    by_page: dict[tuple[str, int], list[dict]] = {}
    for q in QUERIES:
        by_page.setdefault((q["path"], q["page"]), []).append(q)

    hit = 0
    for (path, page_no), qs in by_page.items():
        boxes = [
            t["bbox"] for t in extract_tables_from_page(open_table_page(path, page_no))
        ]
        doc = pymupdf.open(path)
        mu_page = doc[page_no]
        for q in qs:
            if any(answer_in_region(mu_page, b, q["answer"]) for b in boxes):
                hit += 1
        doc.close()

    junk_pages = junk_tables = 0
    for path in sorted(glob.glob("pages/corpus/*.pdf")):
        doc = pymupdf.open(path)
        for page_no in range(doc.page_count):
            try:
                if extract_tables_from_page(doc[page_no]):
                    continue
            except Exception:
                pass
            got = extract_tables_from_page(open_table_page(path, page_no))
            if got:
                junk_pages += 1
                junk_tables += len(got)
        doc.close()
    return hit, junk_pages, junk_tables


def score(fallback, min_numeric, min_rows) -> tuple[int, int, int]:
    by_page: dict[tuple[str, int], list[dict]] = {}
    for q in QUERIES:
        by_page.setdefault((q["path"], q["page"]), []).append(q)

    hit = 0
    for (path, page_no), qs in by_page.items():
        with pdfplumber.open(path) as pdf:
            got = detect(pdf.pages[page_no], fallback, min_numeric, min_rows)
            boxes = [t.bbox for t in got]
        doc = pymupdf.open(path)
        mu_page = doc[page_no]
        for q in qs:
            if any(answer_in_region(mu_page, b, q["answer"]) for b in boxes):
                hit += 1
        doc.close()

    junk_pages = junk_tables = 0
    for path in sorted(glob.glob("pages/corpus/*.pdf")):
        doc = pymupdf.open(path)
        with pdfplumber.open(path) as pdf:
            for page_no in range(doc.page_count):
                try:
                    if doc[page_no].find_tables().tables:
                        continue
                except Exception:
                    pass
                got = detect(pdf.pages[page_no], fallback, min_numeric, min_rows)
                if got:
                    junk_pages += 1
                    junk_tables += len(got)
        doc.close()
    return hit, junk_pages, junk_tables


def pymupdf_reference() -> int:
    by_page: dict[tuple[str, int], list[dict]] = {}
    for q in QUERIES:
        by_page.setdefault((q["path"], q["page"]), []).append(q)
    hit = 0
    for (path, page_no), qs in by_page.items():
        doc = pymupdf.open(path)
        mu_page = doc[page_no]
        try:
            boxes = [t.bbox for t in mu_page.find_tables().tables]
        except Exception:
            boxes = []
        for q in qs:
            if any(answer_in_region(mu_page, b, q["answer"]) for b in boxes):
                hit += 1
        doc.close()
    return hit


CONFIGS: dict[str, tuple] = {
    "ruled lines only": (None, 0.0, SHIPPED_MIN_ROWS),
    "SHIPPED": (SHIPPED_FALLBACK, SHIPPED_MIN_NUMERIC, SHIPPED_MIN_ROWS),
    "shipped, no numeric guard": (SHIPPED_FALLBACK, 0.0, SHIPPED_MIN_ROWS),
    "shipped, allow 1-row": (SHIPPED_FALLBACK, SHIPPED_MIN_NUMERIC, 1),
    "fallback default mwv=3": (
        {**SHIPPED_FALLBACK, "min_words_vertical": 3},
        SHIPPED_MIN_NUMERIC,
        SHIPPED_MIN_ROWS,
    ),
    "mwv=26, no numeric guard": (
        {**SHIPPED_FALLBACK, "min_words_vertical": 26},
        0.0,
        SHIPPED_MIN_ROWS,
    ),
}


def pymupdf_reference_pipeline() -> int:
    from pdf_mcp.extractor import extract_tables_from_page

    by_page: dict[tuple[str, int], list[dict]] = {}
    for q in QUERIES:
        by_page.setdefault((q["path"], q["page"]), []).append(q)
    hit = 0
    for (path, page_no), qs in by_page.items():
        doc = pymupdf.open(path)
        mu_page = doc[page_no]
        try:
            boxes = [t["bbox"] for t in extract_tables_from_page(mu_page)]
        except Exception:
            boxes = []
        for q in qs:
            if any(answer_in_region(mu_page, b, q["answer"]) for b in boxes):
                hit += 1
        doc.close()
    return hit


def main() -> int:
    total = len(QUERIES)
    print("--- through the real pipeline (extract_tables_from_page) ---")
    print(f"{'backend':28s} {'recall':>9s} {'junk pages':>11s} {'junk tables':>12s}")
    mu_hit = pymupdf_reference_pipeline()
    print(f"{'PyMuPDF (shipped today)':28s} {mu_hit:>6d}/{total:<2d}")
    hit, jp, jt = score_pipeline()
    print(f"{'pdfplumber backend':28s} {hit:>6d}/{total:<2d} {jp:>11d} {jt:>12d}")

    print("\n--- raw pdfplumber output, no merger (setting sweep) ---")
    print(f"PyMuPDF raw reference: {pymupdf_reference()}/{total}\n")
    print(f"{'config':28s} {'recall':>9s} {'junk pages':>11s} {'junk tables':>12s}")
    for name, (fallback, min_numeric, min_rows) in CONFIGS.items():
        hit, jp, jt = score(fallback, min_numeric, min_rows)
        print(f"{name:28s} {hit:>6d}/{total:<2d} {jp:>11d} {jt:>12d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
