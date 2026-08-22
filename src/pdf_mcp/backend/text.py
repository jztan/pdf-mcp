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
import os
import threading
import re
import statistics
from collections import OrderedDict
from typing import Any

import numpy as np
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw

from .columns import column_bands
from .pagespace import page_transform

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

#: In-process cache of the assembled line model, keyed on file identity
#: (path, mtime_ns, size) and page. The corpus query path re-extracts the
#: same hit pages repeatedly (paragraph picker, bbox lookup, retry), which
#: PyMuPDF absorbed at ~3ms/page and this pipeline could not; per-query
#: latency ran 3x the baseline before this existed.
_LINE_CACHE_MAX = 256
_LINE_CACHE: "OrderedDict[tuple[str, int, int, int], list[Any]]" = OrderedDict()
_LINE_CACHE_LOCK = threading.Lock()


def _file_key(pdf_path: str, page_num: int) -> tuple[str, int, int, int] | None:
    try:
        st = os.stat(pdf_path)
    except OSError:
        return None
    return (os.path.abspath(pdf_path), st.st_mtime_ns, st.st_size, page_num)


def _cached_lines(pdf_path: str, page_num: int) -> "list[Any] | None":
    key = _file_key(pdf_path, page_num)
    if key is None:
        return None
    with _LINE_CACHE_LOCK:
        lines = _LINE_CACHE.get(key)
        if lines is not None:
            _LINE_CACHE.move_to_end(key)
        return lines


def _store_lines(pdf_path: str, page_num: int, lines: "list[Any]") -> None:
    key = _file_key(pdf_path, page_num)
    if key is None:
        return
    with _LINE_CACHE_LOCK:
        _LINE_CACHE[key] = lines
        _LINE_CACHE.move_to_end(key)
        while len(_LINE_CACHE) > _LINE_CACHE_MAX:
            _LINE_CACHE.popitem(last=False)


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
    """Per-glyph text, box and style in top-left space, document order.

    Exact-overlap duplicates are dropped. Some documents draw a glyph
    twice at the same position for a faux-bold effect (the iwaki
    newsletter's interview block does this page-wide), MuPDF's stext
    dedups them, and everything downstream is calibrated to deduped
    text: fed both copies, every character doubled ("めぐみ" became
    "めめぐぐみみ") and CJK phrase queries stopped matching.

    Two hot-path economies, measured on the corpus query benchmark
    (per-query latency was 3x PyMuPDF's before them):

    Style is computed once per TEXT OBJECT, not per glyph. Every glyph
    of one Tj run shares its font, matrix scale and weight, and a page
    has tens of objects against thousands of glyphs; the per-glyph
    FontInfo call (buffer + UTF-8 decode each time) dominated the
    profile. FPDFText_GetTextObject is one cheap call and its pointer
    keys the cache.

    Page text is fetched in ONE call and indexed, guarded by the
    count == length check (a non-BMP character can make pdfium's
    UTF-16 indices diverge from Python string positions; such pages
    fall back to per-char fetches).
    """
    x_off, y_top = page_transform(page)
    out: list[_Char] = []
    seen: set[tuple[str, float, float]] = set()

    n = textpage.count_chars()
    try:
        full = textpage.get_text_range()
    except Exception:  # noqa: BLE001
        full = None
    bulk = full if (full is not None and len(full) == n) else None

    style_cache: dict[int, tuple[str, float, int]] = {}
    raw_tp = textpage.raw
    get_obj = pdfium_raw.FPDFText_GetTextObject
    get_box = pdfium_raw.FPDFText_GetCharBox
    c_left, c_right = ctypes.c_double(), ctypes.c_double()
    c_bottom, c_top = ctypes.c_double(), ctypes.c_double()
    ref = ctypes.byref

    def style_of(index: int) -> tuple[str, float, int]:
        obj = get_obj(raw_tp, index)
        key = ctypes.cast(obj, ctypes.c_void_p).value or 0
        cached = style_cache.get(key)
        if cached is None:
            cached = _char_style(textpage, index)
            style_cache[key] = cached
        return cached

    for i in range(n):
        ch = bulk[i] if bulk is not None else textpage.get_text_range(i, 1)
        if not ch or ch in ("\r", "\n"):
            continue
        # pdfium emits U+FFFE where a line-end soft hyphen was resolved;
        # PyMuPDF keeps the hyphen ("En-\nglish"). Downstream matching
        # folds hyphens to rejoin such words, so the hyphen must survive:
        # left as U+FFFE, "English" was unfindable in the extracted text.
        if ch == "\ufffe":
            ch = "-"
        # Direct FPDFText_GetCharBox with reused ctypes doubles: the
        # pypdfium2 wrapper allocates four fresh doubles per call, which
        # is measurable at thousands of glyphs per page.
        if not get_box(raw_tp, i, ref(c_left), ref(c_right), ref(c_bottom), ref(c_top)):
            continue
        left, right = c_left.value, c_right.value
        bottom, top = c_bottom.value, c_top.value
        if right < left or top < bottom:
            continue
        key = (ch, round(left, 1), round(top, 1))
        if key in seen:
            continue
        seen.add(key)
        font, size, flags = style_of(i)
        out.append(
            _Char(
                i,
                ch,
                left - x_off,
                y_top - top,
                right - x_off,
                y_top - bottom,
                font,
                size,
                flags,
            )
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

    # A run that is clearly VERTICAL is a text column (or column segment)
    # and is a row of its own. Baseline grouping otherwise merges the
    # side-by-side columns of a vertical-Japanese page into one wide
    # pseudo-row whenever their bottoms align, and the merged row's
    # direction vector then reads as horizontal: detect_writing_mode
    # classified vertical pages as horizontal, the extractor's vertical
    # reorder path never engaged, and every digit-adjacent CJK query on
    # the yamato corpus returned zero hits.
    def _is_vertical_run(run: list[_Char]) -> bool:
        x0 = min(c.x0 for c in run)
        x1 = max(c.x1 for c in run)
        y0 = min(c.y0 for c in run)
        y1 = max(c.y1 for c in run)
        height = y1 - y0
        # len >= 2, not 3: the two-character vertical header cells of a
        # Japanese table ("種別", "内容" written top to bottom) are 25pt
        # tall, and left in baseline grouping each one vertically
        # overlaps several horizontal rows and glues them together. Two
        # stacked glyphs are already unmistakably vertical: a horizontal
        # two-character pair is wider than it is tall.
        return len(run) >= 2 and height > 2.0 * (x1 - x0) and height > 2.0 * med_h

    vertical_rows = [run for run in runs if _is_vertical_run(run)]
    runs = [run for run in runs if not _is_vertical_run(run)]
    if not runs:
        return vertical_rows

    ordered = sorted(
        runs, key=lambda run: (min(c.y1 for c in run), min(c.x0 for c in run))
    )
    rows: list[list[_Char]] = list(vertical_rows)
    # First-match-in-insertion-order semantics are load-bearing (a later
    # anchor never shadows an earlier one), so the vectorised form takes
    # the FIRST index of the boolean hit vector, not a nearest match.
    # The buffer is preallocated so each append stays O(1); vertical
    # rows occupy sentinel slots no baseline can match.
    anchor_buf = np.full(len(vertical_rows) + len(ordered), -1e9, dtype=np.float64)
    count = len(vertical_rows)
    for run in ordered:
        baseline = min(c.y1 for c in run)
        hits = np.flatnonzero(np.abs(anchor_buf[:count] - baseline) <= tol)
        if hits.size:
            rows[int(hits[0])].extend(run)
        else:
            rows.append(list(run))
            anchor_buf[count] = baseline
            count += 1
    return rows


#: A row taller than this many median row heights is not a text line.
#: Rotated runs are the case that matters, and they must never merge.
_TALL_ROW_FACTOR = 3.0


#: A horizontal gap wider than this many median glyph heights is treated
#: as a genuine separation when merging rows.
_ROW_X_GAP_FACTOR = 1.6


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
        top, bottom = min(c.y0 for c in row), max(c.y1 for c in row)
        cur_h = bottom - top
        left, right = min(c.x0 for c in row), max(c.x1 for c in row)
        chosen: int | None = None
        # Scan ALL still-overlapping kept rows, not only the last one. A
        # subscript label ("JC(Top)") overlaps its value row, but another
        # fragment of the same table row can be kept between them, and a
        # last-row-only comparison then measured the x-gap against the
        # wrong row and never merged the label back.
        if cur_h <= tall_limit:
            for i in range(len(merged) - 1, -1, -1):
                prev = merged[i]
                prev_top = min(c.y0 for c in prev)
                prev_bottom = max(c.y1 for c in prev)
                if prev_bottom < top:
                    break
                prev_h = prev_bottom - prev_top
                if prev_h > tall_limit:
                    continue
                prev_left = min(c.x0 for c in prev)
                prev_right = max(c.x1 for c in prev)
                x_gap = max(left - prev_right, prev_left - right, 0.0)
                if x_gap > max(8.0, _ROW_X_GAP_FACTOR * med_h):
                    continue
                # Refuse any merge whose UNION would exceed the tall
                # limit. Merging grows the row's extent, so each merge
                # made the next overlap test easier and rows snowballed:
                # one newsletter table congealed into a 100-glyph blob
                # spanning 74pt before the per-row height checks could
                # object.
                union_h = max(prev_bottom, bottom) - min(prev_top, top)
                if union_h > tall_limit:
                    continue
                overlap = min(prev_bottom, bottom) - max(prev_top, top)
                smaller = min(prev_h, cur_h)
                if smaller <= 0:
                    continue

                # A sub- or superscript label sits mostly outside its base
                # row's band ("JC(Top)" overlaps its value row by only
                # 0.47 of its height), so the same-size threshold of 0.6
                # never reunites it. Font size is the discriminator:
                # distinct text rows have matching sizes and near-zero
                # overlap, so the lower bar for smaller-font rows cannot
                # glue them.
                def _size(chars: list[_Char]) -> float:
                    sizes = [c.size for c in chars if c.size > 0]
                    return statistics.median(sizes) if sizes else 0.0

                size_a, size_b = _size(prev), _size(row)
                # 0.7, not 0.85: a genuine sub/superscript label is far
                # smaller than its base ("JC(Top)" is 5.2pt against 8pt,
                # ratio 0.65). At 0.85 the mixed-size cells of a Japanese
                # newsletter table (8pt against 9.9pt, ratio 0.81)
                # qualified, and rows 35pt apart chained into one blob.
                shifted_label = (
                    min(size_a, size_b) > 0
                    and min(size_a, size_b) / max(size_a, size_b) <= 0.7
                )
                threshold = 0.3 if shifted_label else 0.6
                if overlap / smaller > threshold:
                    chosen = i
                    break
        if chosen is None:
            merged.append(row)
        else:
            merged[chosen] = merged[chosen] + row
    return merged


def _split_rows_at_bands(
    rows: list[list[_Char]], bands: list[tuple[float, float]]
) -> list[list[_Char]]:
    """Split each visual row at the boundaries of accepted column bands.

    ONLY there. A first version split rows at any wide internal gap, and
    on datasheet tables that dismembered every row; block grouping then
    stacked the fragments per column, the whole page read column-major
    (all parameter names, then all the numbers), and each value moved far
    from its label in the text stream. Search snippets stopped containing
    the answers next to the labels they matched: excerpt containment fell
    from 0.707 to 0.413.

    Bands come from backend.columns, the same model the extractor's
    column detection uses. A table's bands are many, narrow and lopsided,
    so it gets NO bands and its rows stay whole (row-major, value beside
    label). A two-column article gets exactly its gutter, where a row
    genuinely contains one line per column and must split, or two-column
    reading order collapses (0.598 vs 0.806, while recall stayed 0.874).
    """
    if not bands or len(bands) < 2:
        return rows
    edges = [(bands[i][1] + bands[i + 1][0]) / 2 for i in range(len(bands) - 1)]

    def band_of(char: _Char) -> int:
        centre = (char.x0 + char.x1) / 2
        for i, edge in enumerate(edges):
            if centre < edge:
                return i
        return len(edges)

    out: list[list[_Char]] = []
    for row in rows:
        pieces: dict[int, list[_Char]] = {}
        for char in row:
            pieces.setdefault(band_of(char), []).append(char)
        for _, piece in sorted(pieces.items()):
            out.append(piece)
    return out


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


def _ordered_chars(row: list[_Char]) -> list[_Char]:
    """The row's glyphs in reading order: x-sorted segments.

    One ordering for every output shape. The first version applied the
    segment sort only in the plain-text renderer, so the dict tree kept
    raw index order, and the vertical reorder path (which reads the
    dict shape) still saw "(年度6令和（" for a heading the text shape
    had already fixed.
    """
    ordered = sorted(row, key=lambda c: c.idx)
    if len(ordered) < 2:
        return ordered
    x0 = min(c.x0 for c in row)
    x1 = max(c.x1 for c in row)
    y0 = min(c.y0 for c in row)
    y1 = max(c.y1 for c in row)
    if (x1 - x0) < (y1 - y0):
        return ordered

    segments: list[list[_Char]] = [[ordered[0]]]
    for prev, cur in zip(ordered, ordered[1:]):
        if cur.idx != prev.idx + 1 or cur.x0 < prev.x0 - 1.0:
            segments.append([cur])
        else:
            segments[-1].append(cur)
    if len(segments) > 1:
        segments.sort(key=lambda seg: min(c.x0 for c in seg))
    return [c for seg in segments for c in seg]


def _render_row(row: list[_Char], textpage: Any) -> str:
    """Emit a horizontal row's segments left to right.

    Ordering is per SEGMENT, never per glyph: a segment is a maximal run
    of index-contiguous glyphs whose x positions move forward, and its
    text is fetched whole from pdfium (the spike measured per-glyph
    reassembly shredding words). Segments are what x-ordering may
    legally rearrange. Document order between them is not reliable: the
    yamato newsletter emits a table heading as "年度", then "6", then
    "令和" — index-contiguous, spatially reversed — and document-order
    rendering produced "(年度6令和（" for "（令和6年度）", which made
    every digit-adjacent CJK phrase query miss. A predominantly vertical
    row keeps document order, which for a rotated sidebar or a vertical
    column IS the reading order.
    """
    x0 = min(c.x0 for c in row)
    x1 = max(c.x1 for c in row)
    y0 = min(c.y0 for c in row)
    y1 = max(c.y1 for c in row)
    horizontal = (x1 - x0) >= (y1 - y0)

    ordered = sorted(row, key=lambda c: c.idx)
    segments: list[list[_Char]] = [[ordered[0]]]
    for prev, cur in zip(ordered, ordered[1:]):
        backward = cur.x0 < prev.x0 - 1.0
        if cur.idx != prev.idx + 1 or (horizontal and backward):
            segments.append([cur])
        else:
            segments[-1].append(cur)
    if horizontal and len(segments) > 1:
        segments.sort(key=lambda seg: min(c.x0 for c in seg))

    pieces: list[tuple[float, float, str]] = []  # (x0, x1, text)
    for seg in segments:
        begin = seg[0].idx
        count = seg[-1].idx - begin + 1
        try:
            # Same U+FFFE mapping as _collect_chars: this path fetches
            # the text straight from pdfium, so the per-char fix alone
            # left soft hyphens as U+FFFE in the "text" shape.
            text = textpage.get_text_range(begin, count).replace("\ufffe", "-")
        except Exception:  # noqa: BLE001
            continue
        if text.strip():
            pieces.append(
                (min(c.x0 for c in seg), max(c.x1 for c in seg), text.strip())
            )

    if not pieces:
        return ""
    # Space between segments only across a real visual gap. Segments are
    # an emission-order artefact, not a word boundary: the digits of
    # "10" can arrive as two segments 0.3pt apart, and joining them with
    # a space split the token, so the FTS phrase "10 人" (tokens 10, 人)
    # could never match a page indexed as 1, 0, 人.
    sizes = [c.size for c in row if c.size > 0]
    gap_limit = _WORD_GAP_FACTOR * (statistics.median(sizes) if sizes else 10.0)
    out = [pieces[0][2]]
    for prev_piece, cur_piece in zip(pieces, pieces[1:]):
        gap = cur_piece[0] - prev_piece[1]
        if abs(gap) > gap_limit:
            out.append(" ")
        out.append(cur_piece[2])
    return "".join(out).strip()


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
    ordered = row  # caller supplies reading order (_ordered_chars)
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


def _split_row_into_lines(row: list[_Char]) -> list[list[_Char]]:
    """Split a horizontal row into visual lines at cell-sized gaps.

    MuPDF's granularity: each chart axis label and each table cell is its
    own LINE ("1月", "2月", ... / "Reset Voltage", "0.4", ...), and both
    the vertical reorder path and the CJK excerpt filter consume that
    shape. Kept as one line, the stream's own space glyphs land inside
    the wrong label after x-ordering ("1月2 月3 月4 月"), and the literal
    "3月" the contiguity post-filter needs never appears in page text.

    The threshold is one font size, the same the span splitter uses:
    word gaps run ~0.3 of the size, cell and label gaps run 1.5 to 3.
    Whitespace glyphs at the boundaries are dropped, not carried into a
    line, since the gap itself now expresses the separation.
    """
    ordered = _ordered_chars(row)
    x0 = min(c.x0 for c in row)
    x1 = max(c.x1 for c in row)
    y0 = min(c.y0 for c in row)
    y1 = max(c.y1 for c in row)
    if (x1 - x0) < (y1 - y0) or len(ordered) < 2:
        return [row]

    sizes = [c.size for c in ordered if c.size > 0]
    limit = max(2.0, statistics.median(sizes) if sizes else 10.0)

    def _is_cjk(ch: str) -> bool:
        cp = ord(ch[0]) if ch else 0
        return (
            0x3000 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF or 0xFF00 <= cp <= 0xFFEF
        )

    lines: list[list[_Char]] = [[]]
    prev: _Char | None = None
    for pos, ch in enumerate(ordered):
        if not ch.ch.strip():
            # A whitespace glyph never starts or spans a cell boundary,
            # and next to a CJK glyph it is a typesetting artifact, not a
            # word boundary: the stream's space inside a "2月" axis label
            # kept the literal 2月 out of the page text, and the CJK
            # excerpt contiguity filter dropped the page.
            nxt = next((c for c in ordered[pos + 1 :] if c.ch.strip()), None)
            near_cjk = (prev is not None and _is_cjk(prev.ch)) or (
                nxt is not None and _is_cjk(nxt.ch)
            )
            if (
                lines[-1]
                and prev is not None
                and ch.x0 - prev.x1 <= limit
                and not near_cjk
            ):
                lines[-1].append(ch)
                prev = ch
            continue
        if lines[-1] and prev is not None and ch.x0 - prev.x1 > limit:
            lines.append([ch])
        else:
            lines[-1].append(ch)
        prev = ch
    out = []
    for line in lines:
        while line and not line[-1].ch.strip():
            line = line[:-1]
        if line:
            out.append(line)
    return out or [row]


def _rows_of(page: Any, textpage: Any) -> list[list[_Char]]:
    chars = _collect_chars(page, textpage)
    boxes = [(c.x0, c.y0, c.x1, c.y1) for c in chars if c.ch.strip()]
    bands = column_bands(boxes, page.get_size()[0])
    rows = _split_rows_at_bands(_rows_by_baseline(chars), bands)
    # Split again after merging: _merge_overlapping_rows compares each row
    # only against the previously kept one, so on a multi-column page,
    # where rows alternate between columns, a merge can chain across the
    # gutter even though the first split separated them correctly.
    rows = _split_rows_at_bands(_merge_overlapping_rows(rows), bands)
    rows = [line for row in rows for line in _split_row_into_lines(row)]
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


#: Minimum share of the narrower line that must overlap horizontally for
#: two lines to belong to the same block.
_BLOCK_X_OVERLAP = 0.35
#: A line whose baseline pitch from the previous block line exceeds this
#: multiple of the page's median pitch starts a new block.
_BLOCK_PITCH_FACTOR = 1.25
#: A jump this large in document order (glyph indices) starts a new block
#: even when the geometry is contiguous.
_BLOCK_DOC_ORDER_JUMP = 100


def _x_overlaps(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    overlap = min(a[2], b[2]) - max(a[0], b[0])
    narrower = min(a[2] - a[0], b[2] - b[0])
    return narrower > 0 and overlap / narrower >= _BLOCK_X_OVERLAP


def _median_pitch(lines: list[Any]) -> float:
    """Median baseline-to-baseline distance between stacked lines."""
    pitches: list[float] = []
    ordered = sorted(lines, key=lambda ln: ln[0][1])
    for i, line in enumerate(ordered):
        bbox = line[0]
        best: float | None = None
        for other in ordered[i + 1 :]:
            delta = other[0][1] - bbox[1]
            # y-sorted: once delta exceeds the best found, no later line
            # can beat it. Without this the scan was O(n^2) per call and
            # showed up in the corpus query profile.
            if best is not None and delta >= best:
                break
            if delta <= 0.5 or not _x_overlaps(bbox, other[0]):
                continue
            best = delta
        if best is not None:
            pitches.append(best)
    return statistics.median(pitches) if pitches else 12.0


def _group_into_blocks(lines: list[Any]) -> list[list[Any]]:
    """Group lines into blocks in DOCUMENT order, breaking on geometry.

    Document order is primary, geometry only breaks: this mirrors
    MuPDF's stext device, whose blocks the paragraph picker was tuned
    against, and it is the only model that handles tables and columns
    with one rule. A typesetter emits a table row by row and a
    two-column page column by column, so document-order runs ARE table
    rows in one case and column paragraphs in the other. No spatial
    threshold can do both: on 1807.11632 p4 the caption sits 11.5pt
    above the table header while the header sits 16.2pt above its data
    rows, so any y-scan limit either glues the caption on or cuts the
    data off. In document order the caption run ends before the table
    run starts (the "Set" corner cell, emitted between them, sits 16.4pt
    below the caption and breaks the pitch), and the header, sub-header
    and data rows chain contiguously into one block.

    Breaks:
      - a jump in glyph indices (figure tick labels sit 145 positions
        from the caption below them; body prose continues 3 after it)
      - a baseline pitch beyond the page median (paragraph and section
        boundaries; also the huge upward jump when a column ends and the
        next begins, which is what keeps two-column text from gluing)

    There is deliberately NO x-overlap requirement: the "Set" corner
    cell shares no x-range with the header row beside it, yet belongs to
    the table block, and cross-column joins are already broken by the
    pitch rule because a new column restarts near the top of the page.
    """
    if not lines:
        return []
    heights = [ln[0][3] - ln[0][1] for ln in lines if ln[0][3] > ln[0][1]]
    med_line_h = statistics.median(heights) if heights else 10.0
    # Capped by line height: on a page with only a couple of lines the
    # "median pitch" IS the gap between them (350pt on a two-block test
    # fixture), and an uncapped limit then merges blocks that are half a
    # page apart. No paragraph's leading approaches three line heights.
    pitch_limit = min(_BLOCK_PITCH_FACTOR * _median_pitch(lines), 3.0 * med_line_h)

    def min_idx(line: Any) -> int:
        idxs = [c.idx for c in line[2]]
        return min(idxs) if idxs else 0

    def max_idx(line: Any) -> int:
        idxs = [c.idx for c in line[2]]
        return max(idxs) if idxs else 0

    ordered = sorted(lines, key=min_idx)
    blocks: list[list[Any]] = [[ordered[0]]]
    for prev, cur in zip(ordered, ordered[1:]):
        idx_gap = min_idx(cur) - max_idx(prev)
        pitch = cur[0][1] - prev[0][1]
        same_row = abs(pitch) <= 0.5
        if idx_gap > _BLOCK_DOC_ORDER_JUMP or (
            not same_row and abs(pitch) > pitch_limit
        ):
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
            # PyMuPDF emits vertical text as ONE GLYPH PER LINE, and the
            # vertical reorder path is calibrated to that: it takes the
            # median line height as its glyph-size unit and bins columns
            # right-to-left by x in multiples of it. Fed whole-column
            # lines, the unit became the column height (~300pt), every
            # column landed in one bin, and vertical pages scrambled.
            width = bbox[2] - bbox[0]
            height = bbox[3] - bbox[1]
            cjk = sum(1 for c in row if c.ch and 0x3000 <= ord(c.ch[0]) <= 0x9FFF)
            # Per-glyph lines are for CJK vertical COLUMNS only. A rotated
            # Latin run (a chart's right-axis title, a margin URL) is one
            # LINE that happens to run downward, and PyMuPDF reports it as
            # one line with its full text: split per glyph, the axis-title
            # reader saw a single "(" where it needed "RMSE (nm)".
            if len(row) >= 2 and height > 2.0 * max(width, 1.0) and cjk * 2 >= len(row):
                for ch in sorted(row, key=lambda c: c.idx):
                    glyph_bbox = (ch.x0, ch.y0, ch.x1, ch.y1)
                    glyph_span: dict[str, Any] = {
                        "size": ch.size,
                        "flags": ch.flags,
                        "font": ch.font,
                        "color": 0,
                        "bbox": glyph_bbox,
                    }
                    if raw:
                        glyph_span["chars"] = [{"c": ch.ch, "bbox": glyph_bbox}]
                    else:
                        glyph_span["text"] = ch.ch
                    out_lines.append(
                        {
                            "spans": [glyph_span],
                            "wmode": 1,
                            "dir": (0.0, 1.0),
                            "bbox": glyph_bbox,
                        }
                    )
                continue
            out_spans = []
            for span_chars in _split_into_spans(_ordered_chars(row)):
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
                        for c in span_chars
                    ]
                else:
                    span["text"] = "".join(c.ch for c in span_chars)
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
            glyphs = _ordered_chars(row)
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

    cached = _cached_lines(pdf_path, page_num)
    if cached is not None:
        lines = cached
        return _shape_from_lines(lines, kind, sort=sort, clip=clip)

    doc = pdfium.PdfDocument(pdf_path)
    try:
        page = doc[page_num]
        textpage = page.get_textpage()
        lines = _lines(page, textpage)
        _store_lines(pdf_path, page_num, lines)
        return _shape_from_lines(lines, kind, sort=sort, clip=clip)
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


#: Grouped blocks are cached alongside the lines for unclipped calls
#: (the paragraph picker and bbox lookup always call unclipped, several
#: times per hit page). A clip changes the line set and therefore the
#: grouping, so clipped calls always group fresh.
_BLOCK_CACHE: "dict[int, list[list[Any]]]" = {}


def _shape_from_lines(
    lines: list[Any],
    kind: str,
    *,
    sort: bool = False,
    clip: tuple[float, float, float, float] | None = None,
) -> Any:
    if clip is not None:
        clipped_lines = [ln for ln in lines if _clipped(ln[0], clip)]
        blocks = _group_into_blocks(clipped_lines)
    else:
        cache_key = id(lines)
        cached_blocks = _BLOCK_CACHE.get(cache_key)
        if cached_blocks is None:
            cached_blocks = _group_into_blocks(lines)
            with _LINE_CACHE_LOCK:
                if len(_BLOCK_CACHE) > _LINE_CACHE_MAX:
                    _BLOCK_CACHE.clear()
                _BLOCK_CACHE[cache_key] = cached_blocks
        blocks = cached_blocks

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
