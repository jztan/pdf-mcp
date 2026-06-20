"""
PDF extraction utilities using PyMuPDF.
"""

import logging
import os
import re
import statistics
import sys
import typing
import warnings
from pathlib import Path
from typing import Any

# Suppress PyMuPDF/SWIG DeprecationWarnings (upstream issue, not actionable).
# Python-level filter handles import-time warnings.
warnings.filterwarnings(
    "ignore",
    category=DeprecationWarning,
    message="builtin type.*[Ss]wig.*has no __module__ attribute",
)


# C-level SWIG warnings emitted during interpreter shutdown bypass Python's
# warning filters and write directly to stderr. Wrap stderr to catch those.
class _StderrSwigFilter:
    __slots__ = ("_stream",)

    def __init__(self, stream: typing.TextIO) -> None:
        self._stream = stream

    def write(self, msg: str) -> int:
        if "DeprecationWarning" in msg and "swig" in msg.lower():
            return len(msg)
        return self._stream.write(msg)

    def __getattr__(self, name: str) -> object:
        return getattr(self._stream, name)


sys.stderr = _StderrSwigFilter(sys.stderr)  # type: ignore[assignment]

import pymupdf  # noqa: E402

from .parallel import PageError  # noqa: E402

logger = logging.getLogger(__name__)


def parse_page_range(pages: str | list[int] | None, total_pages: int) -> list[int]:
    """
    Parse page specification into list of 0-indexed page numbers.

    Args:
        pages: Page specification:
            - None: all pages
            - list[int]: explicit page numbers (1-indexed)
            - str: range like "1-5,10,15-20" (1-indexed)
        total_pages: Total number of pages in document

    Returns:
        List of 0-indexed page numbers

    Examples:
        >>> parse_page_range(None, 10)
        [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
        >>> parse_page_range([1, 5, 10], 10)
        [0, 4, 9]
        >>> parse_page_range("1-3,5,8-10", 10)
        [0, 1, 2, 4, 7, 8, 9]
    """
    if pages is None:
        return list(range(total_pages))

    if isinstance(pages, list):
        # Convert 1-indexed to 0-indexed
        return [p - 1 for p in pages if 1 <= p <= total_pages]

    # Parse string format like "1-5,10,15-20"
    result = []
    parts = re.split(r"[,\s]+", pages.strip())

    for part in parts:
        part = part.strip()
        if not part:
            continue

        if "-" in part:
            # Range: "1-5" or "10-20"
            match = re.match(r"(\d+)\s*-\s*(\d+)", part)
            if match:
                start, end = int(match.group(1)), int(match.group(2))
                # Convert to 0-indexed and clamp to valid range
                for p in range(start - 1, end):
                    if 0 <= p < total_pages:
                        result.append(p)
        else:
            # Single page: "5"
            try:
                p = int(part) - 1  # Convert to 0-indexed
                if 0 <= p < total_pages:
                    result.append(p)
            except ValueError:
                continue

    # Remove duplicates while preserving order
    seen = set()
    unique_result = []
    for p in result:
        if p not in seen:
            seen.add(p)
            unique_result.append(p)

    return unique_result


def _import_column_boxes() -> Any:
    """Import the optional column detector, or return None if unavailable.

    Single source of truth for column-aware availability: both
    ``detect_column_boxes`` (the extraction fallback path) and
    ``column_detection_available`` (server_info feature discovery) go through
    here, so the reported feature flag can never drift from what extraction
    actually does. Any failure — missing dependency or its version-guard
    ImportError — yields None.
    """
    try:
        from pymupdf4llm.helpers.multi_column import column_boxes

        return column_boxes
    except Exception:
        return None


def column_detection_available() -> bool:
    """True when the optional column detector is importable.

    Mirrors the exact guard ``detect_column_boxes`` relies on (see
    ``_import_column_boxes``), so server_info reports column-aware
    availability that matches real extraction behaviour.
    """
    return _import_column_boxes() is not None


def detect_column_boxes(page: Any) -> list[Any]:
    """Return column bounding boxes in reading order, or [] if unavailable.

    Wraps pymupdf4llm's column detector. Any failure — missing dependency,
    its version-guard ImportError, or a detection error — degrades to [] so
    callers fall back to positional-sort extraction.
    """
    column_boxes = _import_column_boxes()
    if column_boxes is None:
        return []
    try:
        # margins=0 keeps running headers/footers/page numbers in the column
        # boxes, matching the single-column path (which extracts the full page).
        # Verified to not affect reading-order benchmark score.
        return list(column_boxes(page, footer_margin=0, header_margin=0))
    except Exception:
        return []


# A page is only treated as multi-column when at least two detected boxes are
# "tall" — i.e. their height is at least this fraction of the tallest box on the
# page. Genuine text columns run most of the page height; a sparse grid of
# short cells (e.g. an academic paper's author/affiliation block laid out in a
# visual grid above a full-width body) is NOT a reading-order column structure,
# and extracting it column-by-column scrambles the intended row-by-row order.
# 0.25 sits comfortably above the ratio such grids produce (the Transformer
# title page's tallest author cell is ~0.22 of its full-width body box) while
# staying well below genuine half-height columns.
_COLUMN_MIN_HEIGHT_FRAC = 0.25

# Above this many detected "tall" columns, the layout is treated as degenerate
# over-segmentation (the column detector shattering a vertical/mixed page into
# dozens of slivers), NOT a real multi-column page. Clipping each sliver yields
# glyph-soup + duplication, so such pages fall back to positional-sort
# extraction. Set well above any genuine layout — academic 2-col = 2, dense
# magazine ~3-4, even a broadsheet newspaper ~9-15 — yet far below the 74 that
# motivated this. The 74-vs-real gap is wide, so 16 buys margin against
# regressing dense layouts absent from our corpus (count alone can't tell a
# legit dense layout from over-segmented garbage; the robust overlap signal is
# deferred — see the design spec).
_MAX_COLUMNS = 16


# A page routes to the vertical reorder path when vertical glyphs are at least
# this fraction of all glyphs (and there are at least _VERTICAL_MIN_CHARS of
# them). Below the fraction, or too few vertical glyphs, it is treated as
# horizontal and keeps the existing extraction path. 0.20 (not 0.50) so that
# horizontal-DOMINANT mixed pages with a substantial vertical region (e.g. a
# municipal-bulletin directory page that is 26% vertical interview + 74%
# horizontal listing) still route to the orientation-aware reorder, which
# handles both orientations — the positional path would scramble the vertical
# region. 20%+ vertical glyphs is genuinely mixed, not incidental.
_VERTICAL_MIN_FRACTION = 0.20
_VERTICAL_MIN_CHARS = 30

# Vertical (tategaki / 直排) layout is a CJK phenomenon, so a page with no CJK
# characters cannot need the reorder path. We test plain ``get_text("text")``
# (cheap) against this before paying for the per-line ``get_text("dict")`` parse
# (which builds a nested dict for every block/line/span — the dominant cost of
# the reading-order path, run on every page). Covers the CJK Unified Ideographs
# (incl. Ext-A and the SIP Ext-B block), Hiragana, Katakana, Hangul, CJK
# symbols/punctuation, and halfwidth/fullwidth forms.
_CJK_RE = re.compile("[　-ヿ㐀-䶿一-鿿가-힯豈-﫿" "＀-￯]|[\U00020000-\U0002a6df]")


def detect_writing_mode(page: Any) -> str:
    """Classify a page as 'vertical', 'mixed', or 'horizontal'.

    Builds a glyph-orientation histogram from ``get_text("dict")``: a text
    line whose direction vector is closer to vertical (|dy| > |dx|) contributes
    its glyphs to the vertical count, otherwise horizontal. 'vertical' and
    'mixed' route to the reorder path; 'horizontal' keeps the existing path.

    Uses ``"dict"`` rather than ``"rawdict"``: we only need each line's ``dir``
    vector and a character count, so the per-glyph bbox/origin data ``rawdict``
    emits is pure overhead. ``"dict"`` parses the page several times faster and
    runs on every page (including horizontal-only docs), so the difference
    dominates the reading-order path's cost.

    Before that parse we short-circuit on a cheap CJK pre-gate: a page with no
    CJK characters cannot be vertical, so we skip the ``"dict"`` parse entirely
    and return 'horizontal'. This keeps horizontal-only (e.g. Latin) docs off
    the expensive path the vertical-script feature added.
    """
    if not _CJK_RE.search(page.get_text("text")):
        return "horizontal"
    vertical = 0
    horizontal = 0
    data = page.get_text("dict")
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            dx, dy = line.get("dir", (1.0, 0.0))
            nchars = sum(len(span.get("text", "")) for span in line.get("spans", []))
            if abs(dy) > abs(dx):
                vertical += nchars
            else:
                horizontal += nchars
    total = vertical + horizontal
    if total == 0 or vertical < _VERTICAL_MIN_CHARS:
        return "horizontal"
    fraction = vertical / total
    if fraction < _VERTICAL_MIN_FRACTION:
        return "horizontal"
    if fraction >= 0.8:
        return "vertical"
    return "mixed"


def _collect_glyphs(page: Any) -> list[dict[str, Any]]:
    """Flatten ``get_text("dict")`` to glyph/line dicts with orientation.

    For vertical text PyMuPDF emits one glyph per "line"; for horizontal text a
    line is a full text run. Both become entries with the same shape; the
    reorder works at this granularity.
    """
    glyphs: list[dict[str, Any]] = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            if not text.strip():
                continue
            dx, dy = line.get("dir", (1.0, 0.0))
            x0, y0, x1, y1 = line["bbox"]
            glyphs.append(
                {
                    "text": text,
                    "x0": x0,
                    "y0": y0,
                    "x1": x1,
                    "y1": y1,
                    "vertical": abs(dy) > abs(dx),
                }
            )
    return glyphs


# A tier boundary is an interior low-coverage valley in the vertical-glyph
# y-projection: a bin below this fraction of the median bin coverage, flanked by
# substantially fuller bins. Dense JP layouts have no near-empty gutters, so we
# split on relative minima, not absolute whitespace.
_TIER_VALLEY_FRAC = 0.35


def _valley_tiers(
    vglyphs: list[dict[str, Any]], page_height: float, unit: float
) -> list[float]:
    """Y-positions that split vertical glyphs into tiers (段組), or [].

    Splits at *interior* low-coverage valleys in the y-projection: a run of bins
    below _TIER_VALLEY_FRAC of the median bin coverage that has substantial
    content both BEFORE and AFTER it (a gutter between two content regions, NOT a
    page margin / a band's trailing edge). The boundary is the run's midpoint.
    """
    if page_height <= 0 or unit <= 0:
        return []
    nbins = max(20, int(page_height / (unit * 0.8)))
    binw = page_height / nbins
    cov = [0] * nbins
    for g in vglyphs:
        lo = int(g["y0"] // binw)
        hi = int(g["y1"] // binw)
        for i in range(max(0, lo), min(nbins, hi + 1)):
            cov[i] += 1
    nonzero = [c for c in cov if c > 0]
    if not nonzero:
        return []
    median = statistics.median(nonzero)
    threshold = median * _TIER_VALLEY_FRAC
    bounds: list[float] = []
    i = 0
    while i < nbins:
        if cov[i] < threshold:
            j = i
            while j < nbins and cov[j] < threshold:
                j += 1
            before = max(cov[:i], default=0)
            after = max(cov[j:], default=0)
            if before > median * 0.5 and after > median * 0.5:
                bounds.append((i + j) / 2 * binw)
            i = j
        else:
            i += 1
    merged: list[float] = []
    for b in bounds:
        if not merged or b - merged[-1] > unit * 2:
            merged.append(b)
    return merged


# Vertical glyphs within ~this fraction of a glyph-height in x belong to the
# same column. 0.7 separates adjacent columns (spaced ~1.5x glyph size) while
# tolerating intra-column kerning/punctuation jitter.
_COLUMN_X_FRACTION = 0.7


def reorder_vertical_glyphs(glyphs: list[dict[str, Any]], page_height: float) -> str:
    """Reconstruct reading order for a vertical/mixed page from positioned glyphs.

    Vertical glyphs are split into tiers (valley detection), and within each tier
    ordered into columns right-to-left, top-to-bottom. Horizontal lines are
    positionally sorted into one region. Regions are emitted top-to-bottom by
    their starting y. Pure function over the glyph list (no PyMuPDF).
    """
    vertical = [g for g in glyphs if g["vertical"]]
    horizontal = [g for g in glyphs if not g["vertical"]]
    regions: list[tuple[float, str]] = []  # (region_top_y, text)

    if vertical:
        unit = statistics.median([g["y1"] - g["y0"] for g in vertical])
        degenerate = unit <= 0 or page_height <= 0

        def _column_key(g: dict[str, Any]) -> tuple[float, float]:
            x_center = (g["x0"] + g["x1"]) / 2
            if degenerate:
                # No reliable glyph-height scale: order columns RTL by raw
                # x-center, then top-to-bottom within a column.
                return (-x_center, g["y0"])
            return (-round(x_center / (unit * _COLUMN_X_FRACTION)), g["y0"])

        if degenerate:
            # A zero/negative unit or page_height makes binned valley detection
            # meaningless (and unsafe to divide by) — emit one tier holding all
            # vertical glyphs so the text is still returned.
            tiers = [list(vertical)]
        else:
            bounds = _valley_tiers(vertical, page_height, unit)
            edges = [0.0] + bounds + [page_height + 1.0]
            tiers = [
                [g for g in vertical if lo <= (g["y0"] + g["y1"]) / 2 < hi]
                for lo, hi in zip(edges, edges[1:])
            ]
        for tier in tiers:
            if not tier:
                continue
            tier.sort(key=_column_key)
            regions.append(
                (min(g["y0"] for g in tier), "".join(g["text"] for g in tier))
            )

    if horizontal:
        horizontal.sort(key=lambda g: (round(g["y0"]), g["x0"]))
        regions.append(
            (
                min(g["y0"] for g in horizontal),
                "\n".join(g["text"] for g in horizontal),
            )
        )

    regions.sort(key=lambda r: r[0])
    return "\n\n".join(text for _, text in regions if text)


def reorder_vertical(page: Any) -> str:
    """Reorder a vertical/mixed page's text from its positioned glyphs.

    Strips decorative-font mojibake, then — if the page has a page-space
    VERTICAL rule (side-by-side articles that the valley-tier reorder can't
    separate) — segments into regions and reorders each. Pages with only
    horizontal rules (or none) fall through to the whole-page reorder: its
    valley-tier detection already handles horizontal tiering, and banding on
    horizontal rules that are decorative (not article separators) scrambles
    content that flows across them.
    """
    glyphs = _collect_glyphs(page)
    for g in glyphs:
        g["text"] = _strip_mojibake(g["text"])
    glyphs = [g for g in glyphs if g["text"].strip()]
    page_h = page.rect.height
    h_rules, v_rules = _page_rules(page)
    if not v_rules:
        return reorder_vertical_glyphs(glyphs, page_h)
    regions = _segment_by_rules(glyphs, h_rules, v_rules, page.rect.width, page_h)
    parts = [reorder_vertical_glyphs(region, page_h) for region in regions]
    return "\n\n".join(p for p in parts if p)


# Glyphs whose codepoints fall in scripts that never appear in Japanese
# (Hebrew/Arabic + Indic + SE-Asian band). Broken decorative display fonts with
# no Unicode map render titles as these; strip them so they don't interrupt the
# reordered prose. A no-op on real Japanese/Latin text — does NOT touch CJK
# (0x4E00+), CJK Ext-A (0x3400+), kana, ASCII, or fullwidth forms.
_MOJIBAKE_LO = 0x0590
_MOJIBAKE_HI = 0x1CFF


def _strip_mojibake(text: str) -> str:
    """Remove glyphs in the never-in-Japanese 0x0590-0x1CFF band."""
    return "".join(c for c in text if not (_MOJIBAKE_LO <= ord(c) <= _MOJIBAKE_HI))


# A page-space drawing is a "rule" delimiting article regions when it is long
# and thin. Horizontal rule: spans >=30% of page width, <3pt tall. Vertical
# rule: spans >=25% of page height, <3pt wide.
_RULE_MIN_H_FRAC = 0.30
_RULE_MIN_V_FRAC = 0.25
_RULE_MAX_THICK = 3.0


def _page_rules(
    page: Any,
) -> tuple[list[float], list[tuple[float, float, float]]]:
    """Return (horizontal-rule y-positions, vertical rules as (x, y0, y1)).

    Reads page-space thin drawings from ``get_drawings``; any failure -> ([], []).
    Nested/transformed drawings (negative coords) are naturally excluded by the
    length thresholds, which are relative to the page rect.
    """
    pw, ph = page.rect.width, page.rect.height
    h_rules: list[float] = []
    v_rules: list[tuple[float, float, float]] = []
    try:
        for obj in page.get_drawings():
            r = obj["rect"]
            if r.width > pw * _RULE_MIN_H_FRAC and r.height < _RULE_MAX_THICK:
                h_rules.append(r.y0)
            elif r.height > ph * _RULE_MIN_V_FRAC and r.width < _RULE_MAX_THICK:
                v_rules.append((r.x0, r.y0, r.y1))
    except Exception:
        return [], []
    return sorted(h_rules), v_rules


# Bands thinner than this are merged with the previous band (over-segmentation
# guard) — a dense run of rules (e.g. a 20-row table) must not shatter the page
# into unreadable strips. Merging (vs dropping) ensures no glyph is lost.
_MIN_BAND_PT = 20.0


def _segment_by_rules(
    glyphs: list[dict[str, Any]],
    h_rules: list[float],
    v_rules: list[tuple[float, float, float]],
    page_w: float,
    page_h: float,
) -> list[list[dict[str, Any]]]:
    """Partition glyphs into article regions in vertical reading order.

    Horizontal rules split the page into bands (top-to-bottom); within a band,
    vertical rules spanning it split into regions ordered right-to-left (vertical
    RTL). Rules closer than _MIN_BAND_PT collapse so a glyph is never dropped.
    Returns regions as glyph lists, already in reading order.
    """
    edges = [0.0]
    for y in sorted(h_rules):
        if y - edges[-1] >= _MIN_BAND_PT:
            edges.append(y)
    edges.append(page_h)
    ordered: list[tuple[tuple[int, float], list[dict[str, Any]]]] = []
    for bi in range(len(edges) - 1):
        by0, by1 = edges[bi], edges[bi + 1]
        band = [g for g in glyphs if by0 <= (g["y0"] + g["y1"]) / 2 < by1]
        if not band:
            continue
        vxs = sorted(
            {round(x) for x, vy0, vy1 in v_rules if vy0 < by1 - 5 and vy1 > by0 + 5}
        )
        xs = [0.0] + [float(x) for x in vxs] + [page_w]
        for xi in range(len(xs) - 1):
            region = [g for g in band if xs[xi] <= (g["x0"] + g["x1"]) / 2 < xs[xi + 1]]
            if not region:
                continue
            cx = sum((g["x0"] + g["x1"]) / 2 for g in region) / len(region)
            ordered.append(((bi, -cx), region))
    ordered.sort(key=lambda item: item[0])
    return [region for _, region in ordered]


def vertical_detection_available() -> bool:
    """True — vertical reorder is PyMuPDF-only and always available (no extra)."""
    return True


# A page is "confidently single-column" only when the strong majority of text
# blocks run nearly the full text width. Two-column blocks span ~half the width
# and fail this test, so the heuristic errs toward False (pay the detector)
# rather than risk scrambling a real two-column page.
_SINGLE_COL_WIDTH_FRAC = 0.6
_SINGLE_COL_MAJORITY = 0.8


def is_confidently_single_column(blocks: list[Any]) -> bool:
    """True only when block geometry is unmistakably single-column.

    Conservative by design: ambiguous or multi-column-looking pages return
    False so the full ``detect_column_boxes`` path runs unchanged.
    """
    text_blocks = [
        b
        for b in blocks
        if len(b) >= 7 and b[6] == 0 and isinstance(b[4], str) and b[4].strip()
    ]
    if len(text_blocks) < 2:
        return False
    left = min(b[0] for b in text_blocks)
    right = max(b[2] for b in text_blocks)
    text_width = right - left
    if text_width <= 0:
        return False
    wide = sum(
        1 for b in text_blocks if (b[2] - b[0]) >= _SINGLE_COL_WIDTH_FRAC * text_width
    )
    return wide >= _SINGLE_COL_MAJORITY * len(text_blocks)


def _is_multi_column_layout(boxes: list[Any]) -> bool:
    """True only when >=2 detected boxes are tall enough to be real columns.

    Guards against ``detect_column_boxes`` over-segmenting a single-column page
    whose top is a visual grid (author/affiliation blocks, badge rows) into many
    short side-by-side boxes — reading those column-by-column reorders content
    that is meant to be read row-by-row. See ``_COLUMN_MIN_HEIGHT_FRAC``. True
    only when 2..``_MAX_COLUMNS`` boxes are tall enough to be real columns; above
    the ceiling the layout is degenerate over-segmentation — see ``_MAX_COLUMNS``.
    """
    if len(boxes) <= 1:
        return False
    max_height = max(box.height for box in boxes)
    if max_height <= 0:
        return False
    tall = sum(1 for box in boxes if box.height >= _COLUMN_MIN_HEIGHT_FRAC * max_height)
    # Lower bound: need >=2 real columns. Upper bound (_MAX_COLUMNS): more than
    # any genuine layout has => degenerate over-segmentation, use positional sort.
    return 2 <= tall <= _MAX_COLUMNS


def extract_text_from_page(page: Any, sort_by_position: bool = True) -> str:
    """
    Extract text from a PDF page.

    Args:
        page: PyMuPDF page object
        sort_by_position: If True, sort text blocks by Y-coordinate for reading order

    Returns:
        Extracted text content
    """
    if sort_by_position:
        if detect_writing_mode(page) in ("vertical", "mixed"):
            return reorder_vertical(page)
        boxes = detect_column_boxes(page)
        if _is_multi_column_layout(boxes):
            # Multi-column: extract each column in reading order so the
            # text is not interleaved row-by-row across columns.
            parts = (
                page.get_text("text", clip=box, sort=True).strip() for box in boxes
            )
            return "\n\n".join(part for part in parts if part)
        # Single-column (or detection unavailable): positional block sort.
        blocks = page.get_text("blocks", sort=True)
        # blocks format: (x0, y0, x1, y1, "text", block_no, block_type)
        # block_type: 0 = text, 1 = image
        text_blocks = [block[4] for block in blocks if block[6] == 0]
        return "\n\n".join(text_blocks)
    else:
        return str(page.get_text())


_PARAGRAPH_MAX_CHARS = 2000


def get_paragraph_for_offset(
    page: Any, char_offset: int, max_chars: int = _PARAGRAPH_MAX_CHARS
) -> tuple[str | None, int | None]:
    """
    Find the text block containing char_offset in the page's joined text.

    The joined text uses the same layout as extract_text_from_page
    (blocks joined by "\\n\\n", text blocks only, sorted by position).

    Returns (block_text, block_index) or (None, None) if the offset
    is out of range or the matching block exceeds max_chars.
    """
    blocks = page.get_text("blocks", sort=True)
    text_blocks = [block[4] for block in blocks if block[6] == 0]

    cursor = 0
    for idx, block_text in enumerate(text_blocks):
        block_len = len(block_text)
        if cursor + block_len > char_offset:
            stripped = block_text.strip()
            if len(stripped) > max_chars:
                return None, None
            return stripped, idx
        cursor += block_len + 2  # +2 for "\n\n" separator

    return None, None


_PARAGRAPH_MIN_CHARS = 80


def get_best_paragraph_for_query(
    page: Any,
    query: str,
    max_chars: int = _PARAGRAPH_MAX_CHARS,
    min_chars: int = 0,
) -> tuple[str | None, int | None]:
    """
    Find the text block on *page* best matching *query* by token overlap.

    Scores each block by the count of distinct query tokens found
    (case-insensitive substring) and returns the highest-scoring block.
    Blocks shorter than *min_chars* (after stripping) are skipped —
    this filters out section headings and figure captions that score
    well on token overlap but carry no useful context.

    Works well for keyword and hybrid modes where query terms appear
    literally in the text.  For pure semantic queries (conceptual
    paraphrases with few literal tokens), the winning block may be
    topically related but not the strongest semantic match on the page.

    Returns (block_text, block_index) or (None, None) if no tokens
    match or the best block exceeds max_chars.
    """
    tokens = [t.strip(".,;:!?\"'()[]{}") for t in query.lower().split()]
    tokens = [t for t in tokens if t]
    if not tokens:
        return None, None

    blocks = page.get_text("blocks", sort=True)
    text_blocks = [block[4] for block in blocks if block[6] == 0]

    best_score = 0
    best_idx: int | None = None
    best_text: str | None = None

    for idx, raw_text in enumerate(text_blocks):
        stripped = raw_text.strip()
        if len(stripped) < min_chars:
            continue
        lower = raw_text.lower()
        score = sum(1 for t in tokens if t in lower)
        if score > best_score:
            best_score = score
            best_idx = idx
            best_text = raw_text

    if best_score == 0 or best_text is None:
        return None, None

    stripped = best_text.strip()
    if len(stripped) > max_chars:
        return None, None

    return stripped, best_idx


def extract_text_with_coordinates(page: Any) -> list[dict[str, Any]]:
    """
    Extract text with Y-coordinate information for content ordering.

    Args:
        page: PyMuPDF page object

    Returns:
        List of content blocks with type, text, and position
    """
    blocks = page.get_text("dict")["blocks"]

    content = []
    for block in blocks:
        if block["type"] == 0:  # Text block
            # Extract text from spans
            text_parts = []
            for line in block["lines"]:
                line_text = ""
                for span in line["spans"]:
                    line_text += span["text"]
                text_parts.append(line_text)

            text = "\n".join(text_parts)
            if text.strip():
                content.append(
                    {
                        "type": "text",
                        "text": text,
                        "y": block["bbox"][1],  # Top Y coordinate
                        "bbox": block["bbox"],
                    }
                )
        elif block["type"] == 1:  # Image block
            content.append(
                {
                    "type": "image_placeholder",
                    "y": block["bbox"][1],
                    "bbox": block["bbox"],
                }
            )

    # Sort by Y coordinate for natural reading order
    content.sort(key=lambda x: x["y"])

    return content


def extract_images_from_page(
    doc: pymupdf.Document,
    page_num: int,
    output_dir: Path | None = None,
    pdf_hash: str = "",
) -> list[dict[str, Any]]:
    """
    Extract images from a PDF page as PNG files saved to disk.

    Args:
        doc: PyMuPDF document object
        page_num: Page number (0-indexed)
        output_dir: Directory to save PNG files
        pdf_hash: Hash prefix for deterministic filenames

    Returns:
        List of image dicts with width, height, format, path, size_bytes
    """
    page = doc[page_num]
    images = []

    image_list = page.get_images(full=True)

    for img_index, img_info in enumerate(image_list):
        xref = img_info[0]

        try:
            # Extract image as Pixmap
            pix = pymupdf.Pixmap(doc, xref)

            # Handle CMYK images
            if pix.n - pix.alpha > 3:
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)

            # Determine color format
            if pix.n == 1:
                color_format = "grayscale"
            elif pix.n == 3:
                color_format = "rgb"
            elif pix.n == 4:
                color_format = "rgba"
            else:
                color_format = "unknown"

            # Save to disk
            assert output_dir is not None
            file_name = f"{pdf_hash}_p{page_num}_i{img_index}.png"
            file_path = output_dir / file_name
            try:
                pix.save(str(file_path))
                os.chmod(str(file_path), 0o600)
            except Exception as e:
                try:
                    file_path.unlink(missing_ok=True)
                except Exception:
                    pass
                logger.warning(
                    "Failed to save image %d from page %d: %s",
                    img_index,
                    page_num,
                    e,
                )
                continue

            images.append(
                {
                    "page": page_num + 1,  # 1-indexed for output
                    "index": img_index,
                    "width": pix.width,
                    "height": pix.height,
                    "format": color_format,
                    "path": str(file_path),
                    "size_bytes": file_path.stat().st_size,
                }
            )

        except (ValueError, RuntimeError, KeyError) as e:
            # Skip problematic images but log the issue
            logger.warning(
                "Failed to extract image %d from page %d: %s", img_index, page_num, e
            )
            continue

    return images


def render_page_as_png(
    doc: pymupdf.Document,
    page_num: int,
    output_dir: Path,
    pdf_hash: str,
    dpi: int = 200,
    clip: "pymupdf.Rect | None" = None,
) -> dict[str, Any]:
    """
    Render a PDF page (or a clipped region of it) as a PNG file.

    Args:
        doc: PyMuPDF document object
        page_num: Page number (0-indexed)
        output_dir: Directory to save the PNG
        pdf_hash: Hash prefix for deterministic filenames
        dpi: Render resolution (default 200)
        clip: Optional region rectangle (page points). When set, only that
            region is rendered at `dpi`, and the filename carries a clip token
            so clipped and full renders never collide on disk.

    Returns:
        Dict with file_path_on_disk, size_bytes, width, height
    """
    page = doc[page_num]
    if clip is not None:
        pix = page.get_pixmap(dpi=dpi, clip=clip)
        token = f"_clip{int(clip.x0)}-{int(clip.y0)}-{int(clip.x1)}-{int(clip.y1)}"
    else:
        pix = page.get_pixmap(dpi=dpi)
        token = ""

    file_name = f"{pdf_hash}_p{page_num}_render_{dpi}dpi{token}.png"
    file_path = output_dir / file_name
    try:
        pix.save(str(file_path))
        os.chmod(str(file_path), 0o600)
    except Exception as e:
        try:
            file_path.unlink(missing_ok=True)
        except Exception:
            pass
        logger.warning("Failed to save render for page %d: %s", page_num, e)
        raise

    return {
        "file_path_on_disk": str(file_path),
        "size_bytes": file_path.stat().st_size,
        "width": pix.width,
        "height": pix.height,
    }


def check_tesseract_available() -> None:
    """
    Verify Tesseract binary is on PATH.

    Raises:
        RuntimeError: If tesseract binary is not found or returns non-zero.
    """
    import subprocess

    try:
        subprocess.run(
            ["tesseract", "--version"],
            capture_output=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise RuntimeError(
            "Tesseract not found. Install with: "
            "brew install tesseract (macOS) / "
            "apt install tesseract-ocr (Linux). "
            "See https://tesseract-ocr.github.io/tessdoc/Installation.html. "
            "If OCR returns empty for a page with visible text, also verify "
            "the language pack: tesseract --list-langs"
        ) from exc


def ocr_page(
    doc: pymupdf.Document,
    page_num: int,
    lang: str = "eng",
    dpi: int = 300,
) -> str:
    """
    OCR a PDF page using PyMuPDF's built-in Tesseract binding.

    Args:
        doc: PyMuPDF document object
        page_num: Page number (0-indexed)
        lang: Tesseract language code (default 'eng')
        dpi: Internal render DPI for OCR (fixed at 300 for v1; not user-configurable
             to keep the surface minimal — expose as parameter in a future release
             if user feedback demands finer control)

    Returns:
        Extracted text string (empty string if OCR produces nothing)
    """
    page = doc[page_num]
    textpage = page.get_textpage_ocr(language=lang, dpi=dpi)
    return str(page.get_text(textpage=textpage))


def _ocr_page_worker(
    args: tuple[str, int, str, int],
) -> tuple[int, "str | PageError"]:
    """Picklable OCR worker for ProcessPoolExecutor.

    Opens its OWN Document (PyMuPDF documents are not shareable across
    processes) and isolates per-page failure as a PageError so one bad page
    never crashes the batch. Lives in extractor.py (not server.py) so spawn
    re-imports only PyMuPDF, never FastMCP.
    """
    path, page_num, lang, dpi = args
    try:
        doc = pymupdf.open(path)
        try:
            return page_num, ocr_page(doc, page_num, lang=lang, dpi=dpi)
        finally:
            doc.close()
    except Exception as exc:  # noqa: BLE001 - deliberate per-page isolation
        return page_num, PageError(repr(exc))


def _render_page_worker(
    args: tuple[str, int, str, str, int],
) -> tuple[int, "dict[str, Any] | PageError"]:
    """Picklable render worker for ProcessPoolExecutor.

    Opens its own Document and writes the PNG to disk (filenames are
    deterministic from pdf_hash+page+dpi, so concurrent workers never collide).
    Returns the render_info dict; the parent records SQLite metadata.
    """
    path, page_num, out_dir, pdf_hash, dpi = args
    try:
        doc = pymupdf.open(path)
        try:
            info = render_page_as_png(doc, page_num, Path(out_dir), pdf_hash, dpi)
            return page_num, info
        finally:
            doc.close()
    except Exception as exc:  # noqa: BLE001 - deliberate per-page isolation
        return page_num, PageError(repr(exc))


# A detected "table" whose bounding box spans almost the entire page body in
# BOTH dimensions is almost always a false positive: the table finder latched
# onto the page's main text block. This is common on dense CJK / academic prose
# pages, where it emits many phantom columns of broken (sometimes reversed)
# text. Real tables fill at most one dimension of the page body — never both
# (corpus calibration: real tables top out at min(width_frac, height_frac)
# ~0.65, while the observed false positive spans 0.82 wide x 0.88 tall). Drop a
# table only when it exceeds this fraction in width AND height.
_FULL_PAGE_TABLE_FRAC = 0.8


def _table_spans_full_page(bbox: Any, page_rect: Any) -> bool:
    """Return True when ``bbox`` covers >= 80% of the page in both dimensions.

    Defensive against non-numeric / degenerate inputs (returns False) so the
    caller never drops a table on a measurement error.
    """
    try:
        width_frac = (float(bbox[2]) - float(bbox[0])) / float(page_rect.width)
        height_frac = (float(bbox[3]) - float(bbox[1])) / float(page_rect.height)
    except (TypeError, ValueError, ZeroDivisionError, IndexError):
        return False
    return width_frac >= _FULL_PAGE_TABLE_FRAC and height_frac >= _FULL_PAGE_TABLE_FRAC


def extract_tables_from_page(page: Any) -> list[dict[str, Any]]:
    """
    Extract tables from a PDF page using PyMuPDF's table finder.

    Requires visible line borders to detect table structure.
    Pages without detectable tables return an empty list.

    Args:
        page: PyMuPDF page object

    Returns:
        List of table dicts, each with:
        - index: 0-based table index on this page
        - bbox: [x0, y0, x1, y1] bounding box
        - row_count: total rows including header (equals 1 + len(rows))
        - col_count: number of columns
        - header: list of header cell strings (first row)
        - rows: list of data rows (excludes header); each row is a list of cell strings
    """
    tables: list[dict[str, Any]] = []
    try:
        found = page.find_tables()
        for table in found.tables:
            if _table_spans_full_page(table.bbox, page.rect):
                logger.debug(
                    "Skipping full-page false-positive table: bbox=%s",
                    list(table.bbox),
                )
                continue
            extracted = table.extract()
            if not extracted:
                continue
            header = [str(cell) if cell is not None else "" for cell in extracted[0]]
            rows = [
                [str(cell) if cell is not None else "" for cell in row]
                for row in extracted[1:]
            ]
            tables.append(
                {
                    "index": len(tables),
                    "bbox": list(table.bbox),
                    "row_count": len(extracted),
                    "col_count": len(extracted[0]),
                    "header": header,
                    "rows": rows,
                }
            )
    except Exception as e:
        logger.warning("Failed to extract tables from page: %s", e)
    return tables


def extract_metadata(doc: pymupdf.Document) -> dict[str, Any]:
    """
    Extract metadata from PDF document.

    Args:
        doc: PyMuPDF document object

    Returns:
        Metadata dict with author, title, subject, etc.
    """
    meta = doc.metadata or {}

    return {
        "title": meta.get("title", ""),
        "author": meta.get("author", ""),
        "subject": meta.get("subject", ""),
        "keywords": meta.get("keywords", ""),
        "creator": meta.get("creator", ""),
        "producer": meta.get("producer", ""),
        "creation_date": meta.get("creationDate", ""),
        "modification_date": meta.get("modDate", ""),
        "format": meta.get("format", ""),
        "encryption": meta.get("encryption", ""),
    }


def extract_toc(doc: pymupdf.Document) -> list[dict[str, Any]]:
    """
    Extract table of contents from PDF document.

    Args:
        doc: PyMuPDF document object

    Returns:
        List of TOC entries with level, title, page
    """
    toc = doc.get_toc()

    return [
        {
            "level": entry[0],
            "title": entry[1],
            "page": entry[2],
        }
        for entry in toc
    ]


def estimate_tokens(text: str) -> int:
    """
    Estimate token count for text (rough approximation).

    Uses ~4 characters per token as rough estimate.

    Args:
        text: Input text

    Returns:
        Estimated token count
    """
    return len(text) // 4


def chunk_text(
    text: str, max_tokens: int = 4000, overlap_tokens: int = 200
) -> list[dict[str, Any]]:
    """
    Split text into chunks with overlap.

    Args:
        text: Input text
        max_tokens: Maximum tokens per chunk
        overlap_tokens: Overlap tokens between chunks

    Returns:
        List of chunk dicts with text, start_char, end_char, estimated_tokens
    """
    max_chars = max_tokens * 4
    overlap_chars = overlap_tokens * 4

    chunks = []
    start = 0
    chunk_index = 0

    while start < len(text):
        end = min(start + max_chars, len(text))

        # Try to break at sentence boundary
        if end < len(text):
            # Look for sentence end (.!?) followed by space or newline
            search_start = max(start + max_chars - 500, start)
            last_sentence = -1

            for i in range(end - 1, search_start, -1):
                if text[i] in ".!?" and (i + 1 >= len(text) or text[i + 1] in " \n\t"):
                    last_sentence = i + 1
                    break

            if last_sentence > start:
                end = last_sentence

        chunk_text = text[start:end]

        chunks.append(
            {
                "chunk_index": chunk_index,
                "text": chunk_text,
                "start_char": start,
                "end_char": end,
                "estimated_tokens": estimate_tokens(chunk_text),
            }
        )

        chunk_index += 1
        start = end - overlap_chars if end < len(text) else end

    return chunks
