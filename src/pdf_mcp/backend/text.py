"""Text extraction in PyMuPDF's five get_text shapes.

Line assembly is the spike's v2 algorithm: collect per-glyph boxes in
document order, split into spatially adjacent runs, group runs into
visual rows by baseline, and render each row in DOCUMENT order.

Two decisions in here are load-bearing and were measured, not reasoned:

Runs are split by adjacency BEFORE any baseline grouping, because
baseline-by-y only works for horizontal text. A 90-degree rotated run
(margin URLs, sidebar labels) has near-constant x and increasing y, so
y-clustering gives every glyph its own row and the run disintegrates.

Intra-row spacing is delegated to pdfium's get_text_range over
contiguous index runs, never reconstructed per glyph. Applying a
gap rule between adjacent glyphs shredded words ("infrastructure" into
"infra structure") and cost 0.14 bag_f1: that rule is calibrated for
multi-glyph fragments, where gaps are real word gaps, and fires on
ordinary letter spacing between glyphs of one word.
"""

from __future__ import annotations

import ctypes
import math
import statistics
from typing import Any

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw

#: Baseline tolerance for calling two glyphs the same row, x median height.
_BASELINE_TOL_FACTOR = 0.35
#: Vertical gap above this, x median line height, starts a new block.
_PARA_GAP_FACTOR = 1.35
#: A left-margin jump this large also breaks a paragraph.
_COLUMN_X_TOL = 40.0
#: A style change this large (points) starts a new span within a line.
_SPAN_SIZE_TOL = 0.2

#: PyMuPDF span flag bit 4. section_detector votes on `flags & 16`.
_FLAG_BOLD = 1 << 4
#: PyMuPDF span flag bit 1.
_FLAG_ITALIC = 1 << 1
#: pdfium reports a real weight; 600 is the usual semibold threshold.
_BOLD_WEIGHT = 600
#: PDF font descriptor flag bit 7 (value 64) means italic.
_DESCRIPTOR_ITALIC = 1 << 6

_SHAPES = ("text", "blocks", "dict", "rawdict", "words")


class _Char:
    __slots__ = ("idx", "ch", "x0", "y0", "x1", "y1", "font", "size", "flags")

    def __init__(
        self,
        idx: int,
        ch: str,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        font: str,
        size: float,
        flags: int,
    ) -> None:
        self.idx = idx
        self.ch = ch
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1
        self.font = font
        self.size = size
        self.flags = flags

    @property
    def height(self) -> float:
        return self.y1 - self.y0


def _char_style(textpage: Any, index: int) -> tuple[str, float, int]:
    """Font name, rendered size and PyMuPDF-shaped flags for one glyph.

    FPDFText_GetFontSize returns the UNSCALED size (1.0 on this corpus);
    the rendered size is that times the text matrix scale.

    pdfium exposes no PyMuPDF flags bitfield, so bold and italic are
    rebuilt from the font weight, the PDF descriptor flags and the font
    name. Only bits 1 and 4 are reproduced, which are the bits any
    consumer here reads.
    """
    buffer = ctypes.create_string_buffer(128)
    descriptor = ctypes.c_int()
    pdfium_raw.FPDFText_GetFontInfo(
        textpage.raw, index, buffer, 128, ctypes.byref(descriptor)
    )
    font = buffer.value.decode("utf-8", "replace")

    matrix = pdfium_raw.FS_MATRIX()
    pdfium_raw.FPDFText_GetMatrix(textpage.raw, index, ctypes.byref(matrix))
    scale = math.hypot(matrix.a, matrix.b) or 1.0
    size = float(pdfium_raw.FPDFText_GetFontSize(textpage.raw, index)) * scale

    weight = pdfium_raw.FPDFText_GetFontWeight(textpage.raw, index)
    lowered = font.lower()
    flags = 0
    if weight >= _BOLD_WEIGHT or any(
        mark in lowered for mark in ("bold", "black", "heavy", "semibold")
    ):
        flags |= _FLAG_BOLD
    if (
        descriptor.value & _DESCRIPTOR_ITALIC
        or "italic" in lowered
        or "oblique" in lowered
    ):
        flags |= _FLAG_ITALIC
    return font, round(size, 4), flags


def _collect_chars(page: Any, textpage: Any) -> list[_Char]:
    """Per-glyph text, box and style in top-left space, document order."""
    height = page.get_size()[1]
    out: list[_Char] = []
    for i in range(textpage.count_chars()):
        ch = textpage.get_text_range(i, 1)
        if not ch or ch in ("\r", "\n"):
            continue
        try:
            left, bottom, right, top = textpage.get_charbox(i)
        except Exception:  # noqa: BLE001 - one bad box must not kill the page
            continue
        if right < left or top < bottom:
            continue
        font, size, flags = _char_style(textpage, i)
        out.append(
            _Char(i, ch, left, height - top, right, height - bottom, font, size, flags)
        )
    return out


def _runs_by_adjacency(chars: list[_Char]) -> list[list[_Char]]:
    """Split the document-order glyph stream into spatially adjacent runs.

    Adjacency in EITHER axis, so a rotated run survives whichever way it
    points. See the module docstring.
    """
    if not chars:
        return []
    med_h = statistics.median([c.height for c in chars if c.height > 0] or [10.0])
    limit = max(2.0, 2.5 * med_h)

    runs: list[list[_Char]] = [[chars[0]]]
    for prev, cur in zip(chars, chars[1:]):
        if cur.idx != prev.idx + 1:
            runs.append([cur])
            continue
        dx = max(cur.x0 - prev.x1, prev.x0 - cur.x1, 0.0)
        dy = max(cur.y0 - prev.y1, prev.y0 - cur.y1, 0.0)
        if min(dx, dy) > limit:
            runs.append([cur])
        else:
            runs[-1].append(cur)
    return runs


def _rows_by_baseline(chars: list[_Char]) -> list[list[_Char]]:
    """Group glyph runs into visual rows by glyph bottom.

    Anchored to each row's first member so small per-glyph drifts cannot
    chain two rows together, with a tolerance that scales with glyph
    height: a fixed absolute tolerance either splits large-font rows or
    glues tight-pitch ones.
    """
    runs = _runs_by_adjacency(chars)
    if not runs:
        return []
    med_h = statistics.median([c.height for c in chars if c.height > 0] or [10.0])
    tol = max(1.0, _BASELINE_TOL_FACTOR * med_h)

    ordered = sorted(
        runs, key=lambda run: (min(c.y1 for c in run), min(c.x0 for c in run))
    )
    rows: list[list[_Char]] = []
    anchors: list[float] = []
    for run in ordered:
        baseline = min(c.y1 for c in run)
        placed = False
        for i, anchor in enumerate(anchors):
            if abs(baseline - anchor) <= tol:
                rows[i].extend(run)
                placed = True
                break
        if not placed:
            rows.append(list(run))
            anchors.append(baseline)
    return rows


#: A row taller than this many median row heights is not a text line.
#: Rotated runs are the case that matters, and they must never merge.
_TALL_ROW_FACTOR = 3.0


def _merge_overlapping_rows(rows: list[list[_Char]]) -> list[list[_Char]]:
    """Merge rows whose vertical extents substantially overlap.

    A row far taller than the page median is excluded from merging in
    both directions. Rotated text is the reason: the vertical sidebar on
    nist-zero-trust p8 is a single 355pt-tall row against an 11.2pt
    median, so it vertically overlaps EVERY horizontal line on the page
    and swallowed them into one row, taking the page from 46 lines to 24
    and losing whole table-of-contents entries. It is a real line, so it
    is kept; it just must not absorb its neighbours.
    """
    if not rows:
        return []
    heights = [max(c.y1 for c in r) - min(c.y0 for c in r) for r in rows]
    med_h = statistics.median([h for h in heights if h > 0] or [10.0])
    tall_limit = _TALL_ROW_FACTOR * med_h

    ordered = sorted(rows, key=lambda r: min(c.y0 for c in r))
    merged: list[list[_Char]] = [ordered[0]]
    for row in ordered[1:]:
        prev = merged[-1]
        prev_top, prev_bottom = min(c.y0 for c in prev), max(c.y1 for c in prev)
        top, bottom = min(c.y0 for c in row), max(c.y1 for c in row)
        prev_h, cur_h = prev_bottom - prev_top, bottom - top
        if prev_h > tall_limit or cur_h > tall_limit:
            merged.append(row)
            continue
        overlap = min(prev_bottom, bottom) - max(prev_top, top)
        smaller = min(prev_h, cur_h)
        if smaller > 0 and overlap / smaller > 0.6:
            merged[-1] = prev + row
        else:
            merged.append(row)
    return merged


def _index_runs(row: list[_Char]) -> list[tuple[int, int]]:
    """Contiguous (start, count) index runs within a row."""
    ordered = sorted(row, key=lambda c: c.idx)
    runs: list[tuple[int, int]] = []
    start = prev = ordered[0].idx
    for c in ordered[1:]:
        if c.idx == prev + 1:
            prev = c.idx
            continue
        runs.append((start, prev - start + 1))
        start = prev = c.idx
    runs.append((start, prev - start + 1))
    return runs


def _render_row(row: list[_Char], textpage: Any) -> str:
    """Emit a row in DOCUMENT order, spacing delegated to pdfium.

    Document order rather than x-sort keeps a bullet ahead of its text
    and keeps a style-split URL contiguous.
    """
    parts = []
    for begin, count in _index_runs(row):
        try:
            parts.append(textpage.get_text_range(begin, count))
        except Exception:  # noqa: BLE001
            continue
    return " ".join(p.strip() for p in parts if p.strip()).strip()


def _row_bbox(row: list[_Char]) -> tuple[float, float, float, float]:
    return (
        min(c.x0 for c in row),
        min(c.y0 for c in row),
        max(c.x1 for c in row),
        max(c.y1 for c in row),
    )


def _row_direction(row: list[_Char]) -> tuple[float, float]:
    """PyMuPDF's line['dir'] unit vector.

    detect_writing_mode classifies a whole page from this alone, so a
    default of horizontal is the safe answer for a row too short to
    measure: it keeps Latin pages out of the vertical reorder path.
    """
    if len(row) < 2:
        return (1.0, 0.0)
    ordered = sorted(row, key=lambda c: c.idx)
    first, last = ordered[0], ordered[-1]
    dx = ((last.x0 + last.x1) - (first.x0 + first.x1)) / 2
    dy = ((last.y0 + last.y1) - (first.y0 + first.y1)) / 2
    norm = math.hypot(dx, dy)
    if norm < 1e-6:
        return (1.0, 0.0)
    return (round(dx / norm, 6), round(dy / norm, 6))


def _split_into_spans(row: list[_Char]) -> list[list[_Char]]:
    """Split a row where font, size or flags change, as PyMuPDF does."""
    ordered = sorted(row, key=lambda c: c.idx)
    spans: list[list[_Char]] = [[ordered[0]]]
    for prev, cur in zip(ordered, ordered[1:]):
        changed = (
            cur.font != prev.font
            or cur.flags != prev.flags
            or abs(cur.size - prev.size) > _SPAN_SIZE_TOL
            or cur.idx != prev.idx + 1
        )
        if changed:
            spans.append([cur])
        else:
            spans[-1].append(cur)
    return spans


def _rows_of(page: Any, textpage: Any) -> list[list[_Char]]:
    chars = _collect_chars(page, textpage)
    rows = _merge_overlapping_rows(_rows_by_baseline(chars))
    return sorted(rows, key=lambda r: (_row_bbox(r)[1], _row_bbox(r)[0]))


def _lines(
    page: Any, textpage: Any
) -> list[tuple[tuple[float, ...], str, list[_Char]]]:
    out: list[tuple[tuple[float, ...], str, list[_Char]]] = []
    for row in _rows_of(page, textpage):
        text = _render_row(row, textpage)
        if not text:
            continue
        out.append((_row_bbox(row), text, row))
    return out


def _group_into_blocks(lines: list[Any]) -> list[list[Any]]:
    if not lines:
        return []
    heights = [ln[0][3] - ln[0][1] for ln in lines if ln[0][3] > ln[0][1]]
    med_h = statistics.median(heights) if heights else 10.0
    blocks: list[list[Any]] = [[lines[0]]]
    for prev, cur in zip(lines, lines[1:]):
        gap = cur[0][1] - prev[0][3]
        same_column = abs(cur[0][0] - prev[0][0]) < _COLUMN_X_TOL
        if gap > med_h * _PARA_GAP_FACTOR or not same_column:
            blocks.append([cur])
        else:
            blocks[-1].append(cur)
    return blocks


def _block_bbox(block: list[Any]) -> tuple[float, float, float, float]:
    return (
        min(ln[0][0] for ln in block),
        min(ln[0][1] for ln in block),
        max(ln[0][2] for ln in block),
        max(ln[0][3] for ln in block),
    )


def _clipped(bbox: tuple[float, ...], clip: tuple[float, float, float, float]) -> bool:
    """Keep anything whose centre falls inside the clip."""
    cx = (bbox[0] + bbox[2]) / 2
    cy = (bbox[1] + bbox[3]) / 2
    return clip[0] <= cx <= clip[2] and clip[1] <= cy <= clip[3]


def _build_tree(blocks: list[list[Any]], raw: bool) -> dict[str, Any]:
    out_blocks = []
    for number, block in enumerate(blocks):
        out_lines = []
        for bbox, _text, row in block:
            out_spans = []
            for span_chars in _split_into_spans(row):
                head = span_chars[0]
                span: dict[str, Any] = {
                    "size": head.size,
                    "flags": head.flags,
                    "font": head.font,
                    "color": 0,
                    "bbox": _row_bbox(span_chars),
                }
                if raw:
                    span["chars"] = [
                        {"c": c.ch, "bbox": (c.x0, c.y0, c.x1, c.y1)}
                        for c in sorted(span_chars, key=lambda c: c.idx)
                    ]
                else:
                    span["text"] = "".join(
                        c.ch for c in sorted(span_chars, key=lambda c: c.idx)
                    )
                out_spans.append(span)
            out_lines.append(
                {
                    "spans": out_spans,
                    "wmode": 0,
                    "dir": _row_direction(row),
                    "bbox": bbox,
                }
            )
        out_blocks.append(
            {
                "number": number,
                "type": 0,
                "bbox": _block_bbox(block),
                "lines": out_lines,
            }
        )
    return {"blocks": out_blocks}


def _words_of(blocks: list[list[Any]]) -> list[tuple[Any, ...]]:
    """8-tuples (x0, y0, x1, y1, word, block, line, word_no).

    Word boxes are apportioned across the word's own glyphs rather than
    by proportional advance, so a word box is exact rather than
    interpolated.
    """
    out: list[tuple[Any, ...]] = []
    for block_no, block in enumerate(blocks):
        for line_no, (_bbox, _text, row) in enumerate(block):
            word_no = 0
            current: list[_Char] = []
            for c in sorted(row, key=lambda c: c.idx):
                if c.ch.isspace():
                    if current:
                        out.append(
                            (
                                *_row_bbox(current),
                                "".join(ch.ch for ch in current),
                                block_no,
                                line_no,
                                word_no,
                            )
                        )
                        word_no += 1
                        current = []
                    continue
                current.append(c)
            if current:
                out.append(
                    (
                        *_row_bbox(current),
                        "".join(ch.ch for ch in current),
                        block_no,
                        line_no,
                        word_no,
                    )
                )
    return out


def get_text(
    pdf_path: str,
    page_num: int,
    kind: str = "text",
    *,
    sort: bool = False,
    clip: tuple[float, float, float, float] | None = None,
) -> Any:
    """PyMuPDF's get_text, in the five shapes pdf_mcp consumes.

    `sort` orders blocks in reading order, matching
    get_text("blocks", sort=True). `clip` keeps only content whose centre
    falls inside the rectangle.
    """
    if kind not in _SHAPES:
        raise ValueError(f"unknown get_text shape {kind!r}; expected one of {_SHAPES}")

    doc = pdfium.PdfDocument(pdf_path)
    try:
        page = doc[page_num]
        textpage = page.get_textpage()
        lines = _lines(page, textpage)
        if clip is not None:
            lines = [ln for ln in lines if _clipped(ln[0], clip)]
        blocks = _group_into_blocks(lines)

        if kind == "text":
            return "\n\n".join("\n".join(ln[1] for ln in block) for block in blocks)
        if kind == "blocks":
            out = [
                (*_block_bbox(block), "\n".join(ln[1] for ln in block), number, 0)
                for number, block in enumerate(blocks)
            ]
            if sort:
                out = sorted(out, key=lambda b: (round(b[1], 1), round(b[0], 1)))
                out = [(*b[:5], i, b[6]) for i, b in enumerate(out)]
            return out
        if kind == "words":
            return _words_of(blocks)
        return _build_tree(blocks, raw=(kind == "rawdict"))
    finally:
        doc.close()
