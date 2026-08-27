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

from ..extractor import _NUMBER_TOKEN
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
    split_cells: int = 0

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


#: Non-alphabetic characters allowed inside a value fragment before the
#: "contains no letter" test (rule 5). Units of <=4 chars are dropped too.
_VALUE_PUNCT = "+-–±×%/~∼()"


def _fragment_value_shaped(fragment: str) -> bool:
    """Rule 5: a fragment reads as a value, not prose.

    Remove number tokens, value punctuation, and any remaining token of 4
    or fewer characters (units: V, mA, VDD). What is left must hold no
    alphabetic character.
    """
    stripped = _NUMBER_TOKEN.sub(" ", fragment)
    stripped = "".join(ch for ch in stripped if ch not in _VALUE_PUNCT)
    remaining = [tok for tok in stripped.split() if len(tok) > 4]
    return not any(ch.isalpha() for tok in remaining for ch in tok)


def _column_index_for_centre(
    centre: float, ranges: list[tuple[float, float] | None]
) -> int | None:
    """The single header column whose [x0, x1] contains centre, else None.

    None when the centre falls in no column or in two (overlapping boxes):
    both abort the split for the cell (rule 2).
    """
    hits = [j for j, r in enumerate(ranges) if r is not None and r[0] <= centre <= r[1]]
    return hits[0] if len(hits) == 1 else None


def _reassign_packed_cell(
    words: list[tuple[float, str]],
    ranges: list[tuple[float, float] | None],
    labels: list[str],
    row_cells: list[str | None],
    src_col: int,
) -> dict[int, str] | None:
    """Map a packed cell's words to header columns, or None if any rule fails.

    ``words`` are (x_centre, text) in reading order. Rule 1 (packed: 2+
    numbers) is the caller's gate. Returns {column: fragment} for every
    target column, including ``src_col`` mapped to its own words (absent
    from the map when no word stays there, so the caller empties it).
    """
    assign: dict[int, list[str]] = {}
    for centre, text in words:
        col = _column_index_for_centre(centre, ranges)
        if col is None:  # rule 2
            return None
        assign.setdefault(col, []).append(text)

    targets = sorted(assign)
    if len(targets) < 2:  # rule 3
        return None

    for col in targets:  # rule 4
        if not labels[col].strip():
            return None
        if col != src_col:
            existing = row_cells[col]
            if existing and str(existing).strip():
                return None

    fragments = {col: " ".join(assign[col]) for col in targets}
    for frag in fragments.values():  # rule 5
        if not _fragment_value_shaped(frag):
            return None
    return fragments


def _split_packed_cells(
    page: Any, table: Any, cells: list[list[str | None]]
) -> tuple[list[list[str | None]], int]:
    """Rewrite packed body cells against header geometry. Fails closed.

    Any exception (a None header box, a missing row) leaves the cells
    exactly as extracted with a count of 0: a failure to split is never a
    failure to extract.
    """
    try:
        header_boxes = table.rows[0].cells
        if any(b is None for b in header_boxes):  # cannot evaluate rule 2
            return cells, 0
        ranges: list[tuple[float, float] | None] = [
            (float(b[0]), float(b[2])) for b in header_boxes
        ]
        header_row = cells[0] if cells else []
        labels = [
            str(header_row[j]) if j < len(header_row) and header_row[j] else ""
            for j in range(len(ranges))
        ]
        words = page.extract_words(x_tolerance=1.5)

        split_count = 0
        for i in range(1, len(cells)):
            row_boxes = table.rows[i].cells
            for j, text in enumerate(cells[i]):
                if not text or len(_NUMBER_TOKEN.findall(str(text))) < 2:  # rule 1
                    continue
                if j >= len(row_boxes) or row_boxes[j] is None:
                    continue
                bx0, btop, bx1, bbot = (float(v) for v in row_boxes[j])
                in_cell: list[tuple[float, str]] = []
                for w in words:
                    cx = (float(w["x0"]) + float(w["x1"])) / 2.0
                    cy = (float(w["top"]) + float(w["bottom"])) / 2.0
                    if bx0 - 1.0 <= cx <= bx1 + 1.0 and btop - 1.0 <= cy <= bbot + 1.0:
                        in_cell.append((cx, str(w["text"])))
                if not in_cell:
                    continue
                mapping = _reassign_packed_cell(in_cell, ranges, labels, cells[i], j)
                if mapping is None:
                    continue
                for col, frag in mapping.items():
                    cells[i][col] = frag
                if j not in mapping:
                    cells[i][j] = ""
                split_count += 1
        return cells, split_count
    except Exception:  # noqa: BLE001 - fail closed, never break extraction
        return cells, 0


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
        cells, split_count = _split_packed_cells(page, table, cells)
        rows = [RowResult(bbox=_rect(r.bbox)) for r in table.rows]
        out.append(
            TableResult(
                bbox=_rect(table.bbox),
                rows=rows,
                cells=cells,
                split_cells=split_count,
            )
        )
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
