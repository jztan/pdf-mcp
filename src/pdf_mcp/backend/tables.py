"""Table detection over pdfplumber.

text_x_tolerance=1.5 is required, not cosmetic: at pdfplumber's default,
cell text loses inter-word spaces and comes back as
"Table1.Selectdetailsfromonlinelenderwebsites".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import pdfplumber

from .geometry import Rect

_DIGIT = re.compile(r"\d")

_TEXT_SETTINGS: dict[str, Any] = {"x_tolerance": 1.5}

#: Fallback strategy for pages the ruled-line finder cannot see.
#:
#: pdfplumber's default wants ruled lines in BOTH directions. Academic
#: tables are booktabs-style: horizontal rules only, no verticals, so the
#: default finds nothing on them (Transformer Table 3, FlashAttention
#: p28, SGDR p6). Taking columns from text alignment recovers those.
#:
#: It runs as a fallback rather than as the primary because text-derived
#: columns produce messier cells wherever ruled lines would have worked:
#: applied everywhere it cost 9 points of interpretable_with_context on
#: the excerpt-quality table class.
#:
#: Each setting was chosen on a two-sided sweep (recall on the 42 graded
#: table queries against invented tables on the 6-PDF corpus), because
#: the excerpt gate scores recall only and is blind to over-detection:
#:
#:   text_x_tolerance 1   without it the whole of Transformer Table 3
#:                        collapses to one column per row, which carries
#:                        no column identity and is rejected below
#:   min_words_vertical 8 default 3 invents tables on 27 prose pages;
#:                        8 cuts that to 6 at no recall cost. Raising it
#:                        to 26 reaches zero, but costs 2 real tables
_FALLBACK_SETTINGS: dict[str, Any] = {
    "vertical_strategy": "text",
    "horizontal_strategy": "lines",
    "text_x_tolerance": 1,
    "min_words_vertical": 8,
}

#: Minimum share of non-empty fallback cells that must contain a digit.
#:
#: The fallback's failure mode is shredding prose into pseudo-columns
#: ("outcomes de | fined in this pr | ofile are valuabl"). Measured, the
#: real tables it recovers run 0.50 to 0.83 numeric while the invented
#: ones run 0.00 to 0.42, so this separates them where a words-per-cell
#: rule did not: that one rejected the real tables too, taking recall
#: down to the level of having no fallback at all.
#:
#: It gates the FALLBACK only. The ruled-line arm is trusted on its own,
#: so prose-only tables still come through it.
_FALLBACK_MIN_NUMERIC = 0.5

#: Minimum rows for a FALLBACK detection. See _is_a_table: this must not
#: be applied to the ruled-line arm, whose one-row detections are the
#: input to extract_tables_from_page's _merge_single_row_detections.
_FALLBACK_MIN_ROWS = 2


@dataclass(frozen=True)
class RowResult:
    bbox: Rect


@dataclass
class TableResult:
    bbox: Rect
    rows: list[RowResult]
    cells: list[list[str | None]]

    def extract(self) -> list[list[str | None]]:
        return self.cells


@dataclass
class TableFinding:
    """Mirrors PyMuPDF's find_tables() return, which exposes .tables."""

    tables: list[TableResult]


class TablePage:
    """Page-shaped adapter so extract_tables_from_page can consume this.

    extract_tables_from_page reads only .find_tables() and .rect, so a
    page object is not needed, only those two.
    """

    def __init__(self, pdf_path: str, page_num: int, rect: Rect) -> None:
        self._path = pdf_path
        self._page_num = page_num
        self.rect = rect

    def find_tables(self) -> TableFinding:
        return TableFinding(tables=find_tables(self._path, self._page_num))


def open_table_page(pdf_path: str, page_num: int) -> TablePage:
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_num]
        rect = Rect(0.0, 0.0, float(page.width), float(page.height))
    return TablePage(pdf_path, page_num, rect)


def _is_a_table(cells: list[list[str | None]], min_rows: int = 1) -> bool:
    """Reject detections that carry no table structure.

    pdfplumber reports aligned prose blocks as tables. Attaching one as
    table_context points the agent at a paragraph and calls it a table,
    which is worse than attaching nothing.

    ``min_rows`` is 2 for the FALLBACK arm only, where a one-row
    detection is invented structure ("1 Introduct | ion"). It must stay 1
    for the ruled-line arm: financial statements legitimately come back
    as a run of one-row detections sharing a column block, and
    extract_tables_from_page's _merge_single_row_detections exists to
    stitch those back together. Filtering them here starves that merger,
    which cost the Starbucks p34 query its answer, and Starbucks p34 is
    the very case that merger was written for.
    """
    if len(cells) < max(min_rows, 1):
        return False
    if max((len(row) for row in cells), default=0) < 2:
        return False
    filled = sum(1 for row in cells for cell in row if cell and str(cell).strip())
    return filled >= 2


def _numeric_fraction(cells: list[list[str | None]]) -> float:
    values = [str(c) for row in cells for c in row if c and str(c).strip()]
    if not values:
        return 0.0
    return sum(1 for v in values if _DIGIT.search(v)) / len(values)


def _ncols(table: TableResult) -> int:
    return max((len(row) for row in table.cells), default=0)


def _covers(outer: Rect, inner: Rect) -> bool:
    """Do the two regions describe the same part of the page?"""
    ix = min(outer.x1, inner.x1) - max(outer.x0, inner.x0)
    iy = min(outer.y1, inner.y1) - max(outer.y0, inner.y0)
    if ix <= 0 or iy <= 0:
        return False
    smaller = min(outer.get_area(), inner.get_area())
    return smaller > 0 and (ix * iy) / smaller > 0.5


def _refine_columns(
    primary: list[TableResult], fallback: list[TableResult]
) -> list[TableResult]:
    """Prefer a fallback detection that splits the same region further.

    The ruled-line arm reads SGDR p6 as 4 columns and drops the values
    the query wants; text alignment reads the same region as 8 and keeps
    them. Same region, strictly finer column resolution, so the finer
    read wins. A fallback table overlapping nothing is not added here:
    that is the unguarded behaviour that invents tables out of prose.
    """
    out = list(primary)
    for cand in fallback:
        for i, existing in enumerate(out):
            if _covers(existing.bbox, cand.bbox) and _ncols(cand) > _ncols(existing):
                out[i] = cand
                break
    return out


def _collect(
    page: Any,
    settings: dict[str, Any] | None,
    min_numeric: float = 0.0,
    min_rows: int = 1,
) -> list[TableResult]:
    # pdfplumber reports raw PDF coordinates, so a page whose mediabox
    # origin is not (0, 0) yields boxes shifted by that origin. PyMuPDF
    # normalises the page to (0, 0) and the rest of pdf_mcp assumes that,
    # so translate here. Berkshire's 2024 annual report has an origin of
    # (18, 18): every box came back 18pt out in both axes, which is
    # enough to make a table_context clip miss its own table.
    origin_x, origin_y = float(page.bbox[0]), float(page.bbox[1])

    def _rect(box: Any) -> Rect:
        return Rect(
            box[0] - origin_x,
            box[1] - origin_y,
            box[2] - origin_x,
            box[3] - origin_y,
        )

    out: list[TableResult] = []
    found = page.find_tables(settings) if settings else page.find_tables()
    for table in found:
        cells = table.extract(**_TEXT_SETTINGS)
        if not _is_a_table(cells, min_rows):
            continue
        if min_numeric and _numeric_fraction(cells) < min_numeric:
            continue
        rows = [RowResult(bbox=_rect(r.bbox)) for r in table.rows]
        out.append(TableResult(bbox=_rect(table.bbox), rows=rows, cells=cells))
    return out


def find_tables(pdf_path: str, page_num: int) -> list[TableResult]:
    """Tables on one page, ruled-line finder first.

    The fallback either replaces the result outright, when ruled lines
    see nothing, or refines the column split of a region they read too
    coarsely. Pages the ruled-line arm reads well keep its cleaner cells.
    """
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_num]
        primary = _collect(page, None)
        fallback = _collect(
            page, _FALLBACK_SETTINGS, _FALLBACK_MIN_NUMERIC, _FALLBACK_MIN_ROWS
        )
        if not primary:
            return fallback
        return _refine_columns(primary, fallback)
