"""Column-band detection over glyph boxes. Pure logic (numpy for the histogram).

Lifted from extractor.py (native column detection, PR #31) so the text
backend and the extractor share ONE column model. That sharing is the
point, not tidiness: the text backend must decide where a visual row may
be split, and the only correct answer is "at the gutters of an accepted
multi-column layout". Splitting rows at ad-hoc wide gaps instead read
datasheet tables column-major (every parameter name, then every number),
which moved each value away from its label in the text stream and broke
retrieval: excerpt containment fell from 0.707 to 0.413.

A table never survives the guards here: its bands are many, narrow and
lopsided, so `column_bands` returns [] and its rows stay whole. A real
two-column article passes, and its rows split exactly at the gutter.
"""

from __future__ import annotations

import statistics

import numpy as np

Box = tuple[float, float, float, float]

#: Gutter width floor, relative to median glyph WIDTH (stable across
#: engines; glyph-box heights are not comparable between conventions).
GUTTER_MIN_WIDTH_FACTOR = 0.6
#: A gutter must be glyph-free over at least this fraction of the text band.
GUTTER_MIN_COVERAGE = 0.80
#: Each side of a split must keep at least this fraction of the span.
COLUMN_MIN_WIDTH_FRAC = 0.12
#: Final band widths must be within this ratio; lopsided means sidebar.
COLUMN_MAX_WIDTH_RATIO = 1.35
#: Generous during recursion; the FINAL set is what the balance guard sees.
SPLIT_MAX_WIDTH_RATIO = 3.0
#: Recursion cap on bands.
MAX_COLUMNS = 16
#: Fewer glyphs than this cannot establish a layout.
MIN_GLYPHS = 40
#: Fewer text rows than this cannot establish a layout either. Word gaps
#: on two short lines can align into a glyph-free channel that passes
#: every width guard ("introduction body about" over "graph neural
#: networks" split mid-word at such a channel, and the broken tokens
#: made a section query miss). A genuine column layout runs dozens of
#: rows; the coverage guard only means something when there are rows to
#: cover.
MIN_ROWS = 8


def find_gutters(
    boxes: list[Box], page_width: float, med_h: float
) -> list[tuple[float, float]]:
    """x-intervals crossed by no glyph, wide and tall enough to be columns.

    Runs on glyphs rather than assembled lines on purpose: line assembly
    groups by baseline, and in a two-column layout both columns share
    baselines, so a "line" spans the gutter and it never looks empty.
    """
    if len(boxes) < MIN_GLYPHS:
        return []
    top = min(b[1] for b in boxes)
    bottom = max(b[3] for b in boxes)
    band = bottom - top
    if band <= 0:
        return []

    step = max(0.5, med_h / 4.0)
    n = int(page_width / step) + 1
    # Interval accumulation as a difference array + cumsum: the naive
    # per-box bin loop was the single hottest spot in the cold-page
    # profile (it runs three times per page through the recursion).
    arr = np.asarray(boxes, dtype=np.float64)
    i0 = np.clip((arr[:, 0] / step).astype(np.int64), 0, n - 1)
    i1 = np.clip((arr[:, 2] / step).astype(np.int64), 0, n - 1)
    heights = np.maximum(arr[:, 3] - arr[:, 1], 0.1)
    diff = np.zeros(n + 1, dtype=np.float64)
    np.add.at(diff, i0, heights)
    np.add.at(diff, i1 + 1, -heights)
    covered = np.cumsum(diff[:n])

    threshold = band * (1.0 - GUTTER_MIN_COVERAGE)
    open_bins = covered <= threshold
    edges = np.flatnonzero(np.diff(np.concatenate(([False], open_bins, [False]))))
    runs = [
        (float(edges[j]) * step, float(edges[j + 1]) * step)
        for j in range(0, len(edges), 2)
    ]

    text_x0 = min(b[0] for b in boxes)
    text_x1 = max(b[2] for b in boxes)
    widths = [b[2] - b[0] for b in boxes if b[2] > b[0]]
    med_w = statistics.median(widths) if widths else med_h / 2.0
    min_w = GUTTER_MIN_WIDTH_FACTOR * med_w
    out = []
    for g0, g1 in runs:
        if g1 - g0 < min_w:
            continue
        # Must have text on BOTH sides; otherwise it is a page margin.
        if g0 <= text_x0 + min_w or g1 >= text_x1 - min_w:
            continue
        out.append((g0, g1))
    return out


def best_split(
    boxes: list[Box], x0: float, x1: float, page_width: float, med_h: float
) -> tuple[float, float] | None:
    """Most balanced gutter strictly inside [x0, x1], or None."""
    span = x1 - x0
    if span <= 0:
        return None
    best: tuple[float, tuple[float, float]] | None = None
    for g0, g1 in find_gutters(boxes, page_width, med_h):
        if g0 <= x0 or g1 >= x1:
            continue
        lw, rw = g0 - x0, x1 - g1
        if lw <= 0 or rw <= 0:
            continue
        if lw / span < COLUMN_MIN_WIDTH_FRAC or rw / span < COLUMN_MIN_WIDTH_FRAC:
            continue
        ratio = max(lw, rw) / min(lw, rw)
        if ratio > SPLIT_MAX_WIDTH_RATIO:
            continue
        if best is None or ratio < best[0]:
            best = (ratio, (g0, g1))
    return best[1] if best else None


def column_bands(boxes: list[Box], page_width: float) -> list[tuple[float, float]]:
    """Accepted column x-bands in reading order, or [] when the page is
    not a multi-column layout.

    Recursive most-balanced splitting with the width guards per level and
    the balance guard on the FINAL band set, exactly as
    extractor.detect_column_boxes ships it.
    """
    if len(boxes) < MIN_GLYPHS:
        return []
    heights = [b[3] - b[1] for b in boxes]
    med_h = statistics.median(heights) if heights else 10.0
    row_bins = {round(b[3] / max(med_h, 1.0)) for b in boxes}
    if len(row_bins) < MIN_ROWS:
        return []
    text_x0 = min(b[0] for b in boxes)
    text_x1 = max(b[2] for b in boxes)

    bands = [(text_x0, text_x1)]
    for _ in range(MAX_COLUMNS):
        out: list[tuple[float, float]] = []
        changed = False
        for b0, b1 in bands:
            if len(bands) + len(out) >= MAX_COLUMNS:
                out.append((b0, b1))
                continue
            subset = [b for b in boxes if b0 - 0.5 <= (b[0] + b[2]) / 2 <= b1 + 0.5]
            gut = best_split(subset, b0, b1, page_width, med_h)
            if gut is None:
                out.append((b0, b1))
                continue
            out.extend([(b0, gut[0]), (gut[1], b1)])
            changed = True
        bands = out
        if not changed:
            break

    if len(bands) <= 1:
        return []
    widths = [b1 - b0 for b0, b1 in bands]
    if max(widths) / max(min(widths), 0.1) > COLUMN_MAX_WIDTH_RATIO:
        # Lopsided final set means a sidebar or pull-quote, not columns.
        return []
    return bands
