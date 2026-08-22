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
import re
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

_SUBSET_TAG = re.compile(r"^[A-Z]{6}\+")

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
    # Strip the PDF font SUBSET tag ("EZSFUB+DejaVuSans"), as PyMuPDF
    # does. The six-letter prefix identifies one embedded subset, not a
    # typeface, and the same face can appear under several. Keeping it
    # split "10^-2" into three spans, because the minus sign lived in a
    # different subset from its digit, and chart_extractor then could not
    # read the negative exponent of a log axis.
    font = _SUBSET_TAG.sub("", buffer.value.decode("utf-8", "replace"))

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
    # Sized for LETTER spacing, not line spacing. Consecutive glyphs of a
    # run are ~1-2pt apart whichever way the run points, so this only has
    # to clear that. At 2.5x median height it also swallowed y-axis tick
    # labels stacked 25pt apart (a 17.8pt gap against an 18.5pt limit),
    # merging four of them into the single word "2468".
    #
    # Splitting a run too eagerly is cheap: _rows_by_baseline regroups
    # same-line pieces immediately afterwards. Splitting too late is not,
    # because a merged run is never reconsidered.
    limit = max(2.0, 0.9 * med_h)

    runs: list[list[_Char]] = [[chars[0]]]
    for prev, cur in zip(chars, chars[1:]):
        if cur.idx != prev.idx + 1:
            runs.append([cur])
            continue
        dx = max(cur.x0 - prev.x1, prev.x0 - cur.x1, 0.0)
        dy = max(cur.y0 - prev.y1, prev.y0 - cur.y1, 0.0)
        # Both gaps, not the smaller one. Consecutive glyphs of one visual
        # line are close along the run and overlapping across it,
        # whichever way the line points, so this still keeps a rotated run
        # intact. Taking the SMALLER gap merged anything sharing a column:
        # two y-axis tick labels stacked 40pt apart became one word "05",
        # and chart axis calibration then found no tick series at all.
        if max(dx, dy) > limit:
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

#: A horizontal gap wider than this many median glyph heights splits one
#: baseline group into separate lines.
_ROW_X_GAP_FACTOR = 1.6


def _split_rows_at_gutters(rows: list[list[_Char]], med_h: float) -> list[list[_Char]]:
    """Split a baseline group wherever a column gutter crosses it.

    Grouping purely by baseline spans the whole page width, so on a
    two-column page a left-column line and a right-column line sharing a
    baseline become ONE line. Measured on the READoc corpus that took
    two-column reading order from 0.806 (PyMuPDF) to 0.598, while recall
    stayed at 0.874: every word was present and merely in the wrong
    order, which is the failure mode containment metrics cannot see.

    Word gaps run a few points; a gutter runs tens. The threshold sits
    between them and scales with glyph height so it holds across font
    sizes.
    """
    limit = max(8.0, _ROW_X_GAP_FACTOR * med_h)
    out: list[list[_Char]] = []
    for row in rows:
        ordered = sorted(row, key=lambda c: c.x0)
        segment = [ordered[0]]
        right = ordered[0].x1
        for char in ordered[1:]:
            if char.x0 - right > limit:
                out.append(segment)
                segment = [char]
            else:
                segment.append(char)
            right = max(right, char.x1)
        out.append(segment)
    return out


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
        # Vertical overlap alone is not enough. Two lines in different
        # columns share a baseline, so an overlap-only rule glued them
        # back together immediately after the gutter split separated
        # them: 115 rows collapsed to 59 on a two-column page, and
        # two-column reading order stayed at 0.60 against PyMuPDF's 0.81
        # even though the split itself was working.
        prev_left, prev_right = min(c.x0 for c in prev), max(c.x1 for c in prev)
        left, right = min(c.x0 for c in row), max(c.x1 for c in row)
        x_gap = max(left - prev_right, prev_left - right, 0.0)
        if x_gap > max(8.0, _ROW_X_GAP_FACTOR * med_h):
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


#: A gap wider than this many font sizes starts a new span, so separately
#: placed items on one baseline do not become one span.
_SPAN_GAP_FACTOR = 1.0


def _split_into_spans(row: list[_Char]) -> list[list[_Char]]:
    """Split a row where font, size or flags change, as PyMuPDF does.

    Also on a wide horizontal gap, because a whole axis of tick labels
    shares one baseline and would otherwise be a single span ("101010").

    Deliberately NOT on an index discontinuity. pdfium's character
    indices skip the glyphs _collect_chars filters out, so "1" and "0" of
    a "10" tick can be indices 0 and 3. Splitting there broke the base
    into two spans, and chart_extractor._power_pairs needs a span whose
    text is exactly "10" to recognise a log axis: without it, 10^0..10^2
    read as literal 100..102, calibrated as a clean linear axis, and
    emitted a chart wrong by orders of magnitude.
    """
    ordered = sorted(row, key=lambda c: c.idx)
    spans: list[list[_Char]] = [[ordered[0]]]
    for prev, cur in zip(ordered, ordered[1:]):
        gap = max(cur.x0 - prev.x1, prev.x0 - cur.x1, 0.0)
        changed = (
            cur.font != prev.font
            or cur.flags != prev.flags
            or abs(cur.size - prev.size) > _SPAN_SIZE_TOL
            or gap > _SPAN_GAP_FACTOR * max(prev.size, 1.0)
        )
        if changed:
            spans.append([cur])
        else:
            spans[-1].append(cur)
    return spans


#: A run this much smaller than its neighbour is a superscript, not a
#: line of its own.
_SUPERSCRIPT_SIZE_RATIO = 0.9


def _attach_superscripts(rows: list[list[_Char]]) -> list[list[_Char]]:
    """Reattach superscripts to the row they belong to.

    A log axis is labelled 10^0, 10^1, 10^2. The exponent sits on its own
    raised baseline, 4.2pt above the base against a 2.6pt grouping
    tolerance, so baseline grouping alone files it as a separate line and
    the tick reads as "10" and "0" rather than "100". Three log-axis
    charts then failed to calibrate at all.

    Loosening the baseline tolerance to cover it would merge genuinely
    separate lines. The superscript signature is specific instead: a
    SMALLER run, vertically overlapping its base, horizontally adjacent,
    and to its right.

    Candidates are identified before any attachment. A superscript sits
    ABOVE its base, so it sorts first, and a pass that treated each row
    in turn as a potential base consumed the exponent before its own
    base could claim it.
    """
    if len(rows) < 2:
        return rows

    def size_of(row: list[_Char]) -> float:
        sizes = [c.size for c in row if c.size > 0]
        return statistics.median(sizes) if sizes else 0.0

    sizes = [size_of(r) for r in rows]
    body = statistics.median([s for s in sizes if s > 0] or [0.0])
    if body <= 0:
        return rows

    small = [
        i
        for i, size in enumerate(sizes)
        if 0 < size <= _SUPERSCRIPT_SIZE_RATIO * body and len(rows[i]) <= 3
    ]
    if not small:
        return rows

    attached: set[int] = set()
    merged = {i: list(row) for i, row in enumerate(rows)}
    for i in small:
        cand = rows[i]
        c_top, c_bottom = min(c.y0 for c in cand), max(c.y1 for c in cand)
        c_left = min(c.x0 for c in cand)
        best: tuple[float, int] | None = None
        for j, base in enumerate(rows):
            if j == i or j in small or sizes[j] <= sizes[i]:
                continue
            b_top, b_bottom = min(c.y0 for c in base), max(c.y1 for c in base)
            if min(b_bottom, c_bottom) - max(b_top, c_top) <= 0:
                continue
            gap = c_left - max(c.x1 for c in base)
            if not 0 <= gap <= 0.5 * sizes[j]:
                continue
            if best is None or gap < best[0]:
                best = (gap, j)
        if best is not None:
            merged[best[1]].extend(cand)
            attached.add(i)

    return [row for i, row in merged.items() if i not in attached]


def _rows_of(page: Any, textpage: Any) -> list[list[_Char]]:
    chars = _collect_chars(page, textpage)
    med_h = statistics.median([c.height for c in chars if c.height > 0] or [10.0])
    rows = _split_rows_at_gutters(_rows_by_baseline(chars), med_h)
    # Split again after merging. _merge_overlapping_rows compares each
    # row only against the previously kept one, so on a two-column page,
    # where rows alternate between columns, a merge can chain across the
    # gutter even though the first split separated them correctly.
    rows = _split_rows_at_gutters(_merge_overlapping_rows(rows), med_h)
    rows = _attach_superscripts(rows)
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


#: A gap wider than this fraction of the font size ends a word, even
#: with no space character between the glyphs.
#:
#: Font SIZE, not glyph ink height. Word spacing scales with the former;
#: the latter varies per glyph, and a minus sign's ink box is under a
#: point tall. Using ink height dragged a negative tick label's limit
#: down to 1.3pt, split "-1" into "-" and "1", and the sign was then lost
#: from the axis: a WRONG-EMIT at 155% error on line_neg_linear, caught
#: by the chart ground-truth gate.
_WORD_GAP_FACTOR = 0.32


def _words_of(blocks: list[list[Any]]) -> list[tuple[Any, ...]]:
    """8-tuples (x0, y0, x1, y1, word, block, line, word_no).

    Words break on whitespace AND on a horizontal gap, because pdfium
    emits no space character between separately placed runs. Splitting
    on whitespace alone merged four x-axis tick labels into the single
    word "2468", and chart axis calibration then found no tick series.

    Word boxes come from the word's own glyphs rather than by
    proportional advance, so each box is exact rather than interpolated.
    """
    out: list[tuple[Any, ...]] = []
    for block_no, block in enumerate(blocks):
        for line_no, (_bbox, _text, row) in enumerate(block):
            glyphs = sorted(row, key=lambda c: c.idx)
            sizes = [c.size for c in glyphs if c.size > 0]
            gap_limit = _WORD_GAP_FACTOR * (statistics.median(sizes) if sizes else 10.0)
            word_no = 0
            current: list[_Char] = []

            def flush() -> None:
                nonlocal current, word_no
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

            for char in glyphs:
                if char.ch.isspace():
                    flush()
                    continue
                if current:
                    prev = current[-1]
                    gap = max(char.x0 - prev.x1, prev.x0 - char.x1, 0.0)
                    if gap > gap_limit:
                        flush()
                current.append(char)
            flush()
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


class TextPage:
    """Page-shaped adapter so extractor's text path can consume this.

    extract_text_from_page and detect_column_boxes read only
    ``get_text(...)`` and ``rect``, so a full page object is not needed,
    only those two. Same approach as backend.tables.TablePage.
    """

    def __init__(self, pdf_path: str, page_num: int, rect: Any) -> None:
        self._path = pdf_path
        self.number = page_num
        self.rect = rect

    def get_text(
        self,
        kind: str = "text",
        *,
        sort: bool = False,
        clip: Any = None,
        **_ignored: Any,
    ) -> Any:
        box = None
        if clip is not None:
            box = (float(clip[0]), float(clip[1]), float(clip[2]), float(clip[3]))
        return get_text(self._path, self.number, kind, sort=sort, clip=box)


def open_text_page(pdf_path: str, page_num: int) -> TextPage:
    from .geometry import Rect

    doc = pdfium.PdfDocument(pdf_path)
    try:
        width, height = doc[page_num].get_size()
    finally:
        doc.close()
    return TextPage(pdf_path, page_num, Rect(0.0, 0.0, width, height))
