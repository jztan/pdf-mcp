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


def _is_a_table(cells: list[list[str | None]]) -> bool:
    """Reject detections that carry no table structure.

    pdfplumber reports aligned prose blocks as tables. Attaching one as
    table_context points the agent at a paragraph and calls it a table,
    which is worse than attaching nothing.

    Two rows minimum because extract_tables_from_page treats row 0 as the
    header and rows[1:] as the data: a one-row detection has no data at
    all. That alone removed two invented tables ("1 Introduct | ion").
    """
    if len(cells) < 2:
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


def _collect(
    page: Any, settings: dict[str, Any] | None, min_numeric: float = 0.0
) -> list[TableResult]:
    out: list[TableResult] = []
    found = page.find_tables(settings) if settings else page.find_tables()
    for table in found:
        cells = table.extract(**_TEXT_SETTINGS)
        if not _is_a_table(cells):
            continue
        if min_numeric and _numeric_fraction(cells) < min_numeric:
            continue
        x0, top, x1, bottom = table.bbox
        rows = [
            RowResult(bbox=Rect(r.bbox[0], r.bbox[1], r.bbox[2], r.bbox[3]))
            for r in table.rows
        ]
        out.append(TableResult(bbox=Rect(x0, top, x1, bottom), rows=rows, cells=cells))
    return out


def find_tables(pdf_path: str, page_num: int) -> list[TableResult]:
    """Tables on one page, ruled-line finder first.

    The fallback runs only when the ruled-line finder sees nothing at
    all, so pages it handles keep their cleaner cells.
    """
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_num]
        out = _collect(page, None)
        if not out:
            out = _collect(page, _FALLBACK_SETTINGS, _FALLBACK_MIN_NUMERIC)
    return out
