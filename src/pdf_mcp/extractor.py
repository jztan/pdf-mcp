"""
PDF extraction utilities using PyMuPDF.
"""

import logging
import os
import re
import shutil
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

# Cached tessdata path — resolved once at first OCR check to avoid repeated
# subprocess discovery (which PyMuPDF does via fragile shell=True on Windows).
_TESSDATA_PATH: str | None = None


def _has_traineddata(path: str) -> bool:
    """Check if path contains any .traineddata files."""
    try:
        return any(f.endswith(".traineddata") for f in os.listdir(path))
    except OSError:
        return False


def _resolve_tessdata() -> str | None:
    """Find tessdata directory via safe subprocess call (no shell=True).

    Checks TESSDATA_PREFIX env var first, then queries tesseract directly,
    then falls back to deriving from the tesseract binary location.
    On Windows, `tesseract --list-langs` emits to stdout (not stderr),
    so search both.

    TESSDATA_PREFIX may point to the Tesseract install root (the classic
    convention) instead of the tessdata subfolder. If the candidate has no
    *.traineddata files, append /tessdata as a fallback.
    """
    import subprocess

    try:
        env_path = os.environ.get("TESSDATA_PREFIX")
        if env_path and os.path.isdir(env_path):
            if _has_traineddata(env_path):
                return env_path
            subdir = os.path.join(env_path, "tessdata")
            if os.path.isdir(subdir) and _has_traineddata(subdir):
                return subdir
            return env_path
        result = subprocess.run(
            ["tesseract", "--list-langs"],
            capture_output=True,
            text=True,
            check=True,
        )
        match = re.search(
            r'List of available languages in "(.+)"',
            result.stderr or "",
        )
        if not match:
            match = re.search(
                r'List of available languages in "(.+)"',
                result.stdout or "",
            )
        if match:
            path = match.group(1)
            if os.path.isdir(path):
                return path
            alt = path.replace("/", "\\")
            if os.path.isdir(alt):
                return alt
        exe = shutil.which("tesseract")
        if exe:
            candidate = os.path.join(os.path.dirname(exe), "tessdata")
            if os.path.isdir(candidate):
                return candidate
    except Exception:
        pass
    return None


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
# Width-fraction check is the real two-column guard — genuine two-column blocks
# span ~0.44 of the text width and never count as "wide", so they're rejected
# regardless of this majority value. The majority threshold only governs
# single-column pages that contain some narrow blocks like captions/headings,
# which we DO want to short-circuit. 0.6 keeps two-column safety while not
# forgoing single-column-with-captions pages.
_SINGLE_COL_MAJORITY = 0.6


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


def _rawdict_line_text_and_bbox(
    line: dict[str, Any],
) -> tuple[str, tuple[float, float, float, float], float]:
    """Text, glyph-derived bbox, and baseline y for one rawdict line.

    Geometry comes from glyph bboxes (the deterministic rawdict data), not
    the line's own bbox field. Spans concatenate their glyph chars with no
    separator (rawdict glyphs already include inter-glyph spacing). The
    baseline is the first span's origin y (identical for fragments of the
    same visual row, shifted for super/subscripts); falls back to the
    bbox bottom when spans carry no origin.
    """
    chars: list[str] = []
    xs0: list[float] = []
    ys0: list[float] = []
    xs1: list[float] = []
    ys1: list[float] = []
    baseline: float | None = None
    for span in line.get("spans", []):
        if baseline is None:
            origin = span.get("origin")
            if origin is not None:
                baseline = origin[1]
        for ch in span.get("chars", []):
            bx0, by0, bx1, by1 = ch["bbox"]
            xs0.append(bx0)
            ys0.append(by0)
            xs1.append(bx1)
            ys1.append(by1)
            chars.append(ch["c"])
    text = "".join(chars)
    if xs0:
        bbox = (min(xs0), min(ys0), max(xs1), max(ys1))
    else:
        bbox = (0.0, 0.0, 0.0, 0.0)
    if baseline is None:
        baseline = bbox[3]
    return text, bbox, baseline


def _rect_overlap(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> float:
    """Intersection area of two (x0, y0, x1, y1) rectangles."""
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return ix * iy


def _merge_bboxes(
    bboxes: list[tuple[float, float, float, float]],
) -> tuple[float, float, float, float]:
    """Bounding box enclosing a list of (x0, y0, x1, y1) rectangles."""
    return (
        min(b[0] for b in bboxes),
        min(b[1] for b in bboxes),
        max(b[2] for b in bboxes),
        max(b[3] for b in bboxes),
    )


# Fragments whose baselines differ by no more than this are the same visual
# row (same-row rawdict fragments share an identical baseline; super- and
# subscripts shift by more than 1pt).
_ROW_BASELINE_TOL = 0.5


_RowFrag = tuple[int, str, tuple[float, float, float, float], float]


def _row_y_range(row: list[_RowFrag]) -> tuple[float, float]:
    """Vertical extent (min y0, max y1) spanned by a row's fragments."""
    return (min(f[2][1] for f in row), max(f[2][3] for f in row))


def _render_row(row: list[_RowFrag]) -> str:
    """Join one row's fragments left-to-right, spacing word gaps.

    The gap threshold scales with the row's typical fragment height, so
    larger fonts (e.g. letter-spaced headings) get proportionally wider
    inter-word gaps without being read as adjacent letters. The median
    (not max) height is used: a single tall outlier fragment (a symbol
    glyph with a deep descender/ascender sharing the row's baseline)
    would otherwise inflate the threshold for the whole row and glue
    ordinary word gaps together. A large negative gap (heavy x-overlap)
    also gets a space: plain letter kerning overlaps only slightly, so
    a deep overlap instead signals a separate token (e.g. a subscript
    glued to the tail of the preceding one) rather than two glyphs of
    the same word.
    """
    row = sorted(row, key=lambda f: (f[2][0], f[0]))
    height = statistics.median(f[2][3] - f[2][1] for f in row) or 1.0
    threshold = max(1.0, 0.25 * height)
    merged = row[0][1]
    for prev, cur in zip(row, row[1:]):
        gap = cur[2][0] - prev[2][2]
        merged += (" " if abs(gap) > threshold else "") + cur[1]
    return merged


def _merge_row_fragments(
    lines: list[tuple[str, tuple[float, float, float, float], float]],
) -> str:
    """Merge rawdict line fragments of one run into visual rows.

    Fragments are grouped into rows by baseline: a fragment joins the
    first existing row whose anchor (first fragment) baseline is within
    _ROW_BASELINE_TOL; otherwise it starts a new row. Rows are then
    clustered by vertical (y0, y1) overlap (transitively, like merging
    overlapping intervals): ordinary document lines have disjoint
    y-ranges and each forms its own singleton cluster, so they keep
    top-to-bottom order; a super/subscript's row vertically overlaps
    its base line's row and clusters with it, in which case y alone
    cannot separate them reliably, so members of a cluster instead keep
    first-appearance (document) order. Clusters themselves are emitted
    in top-to-bottom (y0) order. Fragments within a row are ordered by
    x0; adjacent fragments join with a space only when the absolute
    x-gap exceeds max(1.0, 0.25 * median row height), reuniting words
    that rawdict split into same-row fragments when the gap is small
    (plain kerning). A large negative gap (deep x-overlap) also gets a
    space rather than being read as kerning: a real subscript glued to
    the tail of the preceding fragment overlaps far more than ordinary
    kerning does, so it needs a separate token, not a merge. Rows join
    with newline.
    """
    if not lines:
        return ""
    rows: list[list[_RowFrag]] = []
    for i, (text, bbox, baseline) in enumerate(lines):
        for row in rows:
            if abs(baseline - row[0][3]) <= _ROW_BASELINE_TOL:
                row.append((i, text, bbox, baseline))
                break
        else:
            rows.append([(i, text, bbox, baseline)])

    # Cluster rows by vertical overlap via a merge-overlapping-intervals
    # scan (sorted by y0); this is the transitive closure of "overlaps",
    # so distinct clusters never overlap and a plain y0 scan order is a
    # valid, stable total order over them.
    by_y0 = sorted(rows, key=lambda r: _row_y_range(r)[0])
    clusters: list[list[list[_RowFrag]]] = []
    cluster_y1: float | None = None
    for row in by_y0:
        y0, y1 = _row_y_range(row)
        if clusters and cluster_y1 is not None and y0 <= cluster_y1:
            clusters[-1].append(row)
            cluster_y1 = max(cluster_y1, y1)
        else:
            clusters.append([row])
            cluster_y1 = y1

    out_rows: list[str] = []
    for cluster in clusters:
        cluster.sort(key=lambda r: r[0][0])  # first-appearance order
        out_rows.extend(_render_row(row) for row in cluster)
    return "\n".join(out_rows)


def _assemble_columns_from_rawdict(page: Any, boxes: list[Any]) -> str:
    """Deterministic, dedup'd, column-major text from rawdict lines.

    Assignment happens per rawdict *line* (not per block): a block whose
    lines straddle two column boxes (pymupdf4llm's block detector can
    group short, widely spaced same-row lines into one block) would
    otherwise land entirely in a single column, scrambling reading order.
    Each line is assigned once to the column box it overlaps most
    (tiebreak: lowest box index); consecutive lines within a block that
    share the same assignment form a run merged via _merge_row_fragments,
    which reunites same-row fragments rawdict split apart (e.g. letter-
    spaced headings) while keeping an ordinary multi-line paragraph's
    original line breaks. Boxes
    are emitted in the reading order pymupdf4llm returns them; chunks
    within a box are sorted by rounded (y0, x0). Chunks overlapping no
    box are appended last in reading order. All geometry comes from
    deterministic rawdict glyph data, no clip extraction, so output is
    fully deterministic.
    """
    rd = page.get_text("rawdict")

    def r(v: float) -> float:
        return round(v, 1)

    rboxes = [(r(bx.x0), r(bx.y0), r(bx.x1), r(bx.y1)) for bx in boxes]

    def assign_box(bbox: tuple[float, float, float, float]) -> int | None:
        rb = (r(bbox[0]), r(bbox[1]), r(bbox[2]), r(bbox[3]))
        best_j: int | None = None
        best_ov = 0.0
        for j, rbox in enumerate(rboxes):
            ov = _rect_overlap(rb, rbox)
            if ov > best_ov:
                best_ov = ov
                best_j = j
        return best_j

    # (text, bbox, column_index_or_None) chunks, formed by merging
    # consecutive same-column lines within each block.
    items: list[tuple[str, tuple[float, float, float, float], int | None]] = []
    for blk in rd.get("blocks", []):
        if blk.get("type") != 0:
            continue
        run_lines: list[tuple[str, tuple[float, float, float, float], float]] = []
        run_assign: int | None = None
        run_open = False
        for line in blk.get("lines", []):
            text, bbox, baseline = _rawdict_line_text_and_bbox(line)
            if not text.strip():
                continue
            j = assign_box(bbox)
            if run_open and j != run_assign:
                items.append(
                    (
                        _merge_row_fragments(run_lines),
                        _merge_bboxes([b for _t, b, _bl in run_lines]),
                        run_assign,
                    )
                )
                run_lines = []
            run_lines.append((text, bbox, baseline))
            run_assign = j
            run_open = True
        if run_open:
            items.append(
                (
                    _merge_row_fragments(run_lines),
                    _merge_bboxes([b for _t, b, _bl in run_lines]),
                    run_assign,
                )
            )

    parts: list[str] = []
    for j in range(len(boxes)):
        col = [item for item in items if item[2] == j]
        col.sort(key=lambda tb: (r(tb[1][1]), r(tb[1][0])))
        joined = "\n\n".join(text for text, _bbox, _j in col)
        if joined.strip():
            parts.append(joined)
    orphans = [item for item in items if item[2] is None]
    orphans.sort(key=lambda tb: (r(tb[1][1]), r(tb[1][0])))
    orphan_text = "\n\n".join(text for text, _bbox, _j in orphans)
    if orphan_text.strip():
        parts.append(orphan_text)
    return "\n\n".join(parts)


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
        blocks = page.get_text("blocks", sort=True)
        if is_confidently_single_column(blocks):
            # Single-column join keeps all text blocks (byte-identical to fallback),
            # distinct from the heuristic vote which filters to non-empty blocks.
            text_blocks = [block[4] for block in blocks if block[6] == 0]
            return "\n\n".join(text_blocks)
        # NOTE: the rawdict assembly below is deterministic and dedup'd given
        # a fixed set of column boxes, but detect_column_boxes (pymupdf4llm's
        # column_boxes()) can itself return a varying box count across
        # repeated opens of the same page, so multi-column extraction is not
        # fully deterministic on re-extraction. The mtime-keyed cache masks
        # this in steady state (extraction runs once per file). A
        # deterministic column detector is a deferred follow-up.
        boxes = detect_column_boxes(page)
        if _is_multi_column_layout(boxes):
            # Multi-column: assemble each column in reading order from
            # deterministic rawdict line data (each line used once), so the
            # text is neither interleaved row-by-row nor duplicated by
            # overlapping column boxes. Any failure degrades to the
            # positional block-sort below.
            try:
                assembled = _assemble_columns_from_rawdict(page, boxes)
            except Exception:  # pragma: no cover - defensive fail-safe
                assembled = ""
            if assembled:
                return assembled
        # Single-column (or detection unavailable): positional block sort.
        # blocks format: (x0, y0, x1, y1, "text", block_no, block_type)
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


def _query_tokens(query: str) -> list[str]:
    """Tokenise a query the way the paragraph picker scores blocks:
    lowercase, whitespace-split, surrounding punctuation stripped."""
    tokens = [t.strip(".,;:!?\"'()[]{}") for t in query.lower().split()]
    return [t for t in tokens if t]


# A question that asks for a quantity. Kept deliberately narrow: it only
# ever breaks a TIE, so a false positive costs nothing unless two blocks
# score identically, and a false negative just preserves the old
# behaviour.
_QUANT_CUE_RE = re.compile(
    r"\b(?:how much|how many|how fast|how large|"
    r"what (?:was|were|is|are|total)|percentage)\b",
    re.IGNORECASE,
)

# Currency, decimals, percentages and thousands-separated numbers -- the
# shapes a financial answer actually takes. A bare integer such as a year
# is excluded on purpose: almost every block on a filing page contains
# one, so counting it would make this signal useless.
_FIGURE_RE = re.compile(r"[$€£¥]\s?\d|(?<![\w.])\d[\d,]*\.\d|\d\s?%|\d{1,3}(?:,\d{3})+")


def _wants_a_figure(query: str) -> bool:
    return bool(_QUANT_CUE_RE.search(query))


def _fold_for_match(text: str) -> str:
    """Lowercase and drop hyphens so 'pretraining' matches
    'Pre-training' (and hyphenated line breaks). Substring matching is
    already fuzzy across word boundaries; folding hyphens keeps the
    same semantics for the one separator PDFs insert inside words."""
    return text.lower().replace("-", "")


def count_query_tokens(text: str, query: str) -> int:
    """Count query tokens present in *text* (case-insensitive,
    hyphen-folded substring), with the same tokenisation and folding as
    `get_best_paragraph_for_query` so coverage comparisons between
    candidate blocks use identical scoring."""
    tokens = _query_tokens(query)
    if not tokens:
        return 0
    folded = _fold_for_match(text)
    return sum(1 for t in tokens if _fold_for_match(t) in folded)


def get_best_paragraph_for_query(
    page: Any,
    query: str,
    max_chars: int = _PARAGRAPH_MAX_CHARS,
    min_chars: int = 0,
) -> tuple[str | None, int | None]:
    """
    Find the text block on *page* best matching *query* by token overlap.

    Scores each block by the count of distinct query tokens found
    (case-insensitive, hyphen-folded substring) and returns the
    highest-scoring block. Blocks shorter than *min_chars* (after
    stripping) are skipped — this filters out section headings and
    figure captions that score well on token overlap but carry no
    useful context.

    Works well for keyword and hybrid modes where query terms appear
    literally in the text.  For pure semantic queries (conceptual
    paraphrases with few literal tokens), the winning block may be
    topically related but not the strongest semantic match on the page.

    Returns (block_text, block_index) or (None, None) if no tokens
    match or the best block exceeds max_chars.
    """
    tokens = _query_tokens(query)
    if not tokens:
        return None, None

    blocks = page.get_text("blocks", sort=True)
    text_blocks = [block[4] for block in blocks if block[6] == 0]

    prefer_figures = _wants_a_figure(query)

    best_score = 0
    best_key: tuple[int, int] = (0, 0)
    best_idx: int | None = None
    best_text: str | None = None

    folded_tokens = [_fold_for_match(t) for t in tokens]
    for idx, raw_text in enumerate(text_blocks):
        stripped = raw_text.strip()
        if len(stripped) < min_chars:
            continue
        folded = _fold_for_match(raw_text)
        score = sum(1 for t in folded_tokens if t in folded)
        # Tie-break only: token overlap still decides the winner, and the
        # figure flag separates blocks that overlap equally. Ties were
        # previously broken by document order, which on a 10-K page put
        # the narrative paragraph ahead of the one carrying the numbers.
        # A richer tie-break (total occurrences) was prototyped and
        # REJECTED 2026-07-28: it fixed 4 page-1 abstract misses on the
        # arXiv set but regressed the excerpt gate (l04), because on
        # ties the answer is in the long block on one dataset and the
        # short one on the other — no query-side signal separates them.
        carries = 1 if prefer_figures and _FIGURE_RE.search(raw_text) else 0
        key = (score, carries)
        if key > best_key:
            best_key = key
            best_score = score
            best_idx = idx
            best_text = raw_text

    if best_score == 0 or best_text is None:
        return None, None

    stripped = best_text.strip()
    if len(stripped) > max_chars:
        return None, None

    return stripped, best_idx


def block_bbox_for_index(
    page: Any, block_idx: int
) -> tuple[float, float, float, float] | None:
    """
    Return the bbox (absolute PDF points, 1 dp) of the block_idx-th text
    block on the page.

    Uses the same enumeration as get_best_paragraph_for_query and the
    direct-containment branch in server._upgrade_excerpts_to_paragraphs:
    text blocks only (block[6] == 0), sorted by position. Returns None if
    block_idx is out of range.
    """
    blocks = [b for b in page.get_text("blocks", sort=True) if b[6] == 0]
    if block_idx < 0 or block_idx >= len(blocks):
        return None
    b = blocks[block_idx]
    return (round(b[0], 1), round(b[1], 1), round(b[2], 1), round(b[3], 1))


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
    seen: set[int] = set()
    kept_index = 0

    for img_info in image_list:
        xref = img_info[0]
        # get_images lists an image once per placement; extract each
        # distinct xref only once (dedup by object reference).
        if xref in seen:
            continue
        seen.add(xref)

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
            file_name = f"{pdf_hash}_p{page_num}_i{kept_index}.png"
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
                    "Failed to save image xref %d from page %d: %s",
                    xref,
                    page_num,
                    e,
                )
                continue

            img_dict: dict[str, Any] = {
                "page": page_num + 1,  # 1-indexed for output
                "index": kept_index,
                "width": pix.width,
                "height": pix.height,
                "format": color_format,
                "path": str(file_path),
                "size_bytes": file_path.stat().st_size,
            }
            try:
                rects = page.get_image_rects(xref)
            except Exception:
                rects = []
            if rects:
                p = rects[0]
                img_dict["bbox"] = [
                    round(p.x0, 1),
                    round(p.y0, 1),
                    round(p.x1, 1),
                    round(p.y1, 1),
                ]
                if len(rects) > 1:
                    img_dict["placements"] = [
                        [
                            round(r.x0, 1),
                            round(r.y0, 1),
                            round(r.x1, 1),
                            round(r.y1, 1),
                        ]
                        for r in rects
                    ]
            images.append(img_dict)
            kept_index += 1

        except (ValueError, RuntimeError, KeyError) as e:
            # Skip problematic images but log the issue
            logger.warning(
                "Failed to extract image xref %d from page %d: %s",
                xref,
                page_num,
                e,
            )
            continue

    return images


_RENDER_CODECS = {"png": ".png", "jpeg": ".jpg"}


def render_page_as_image(
    doc: pymupdf.Document,
    page_num: int,
    output_dir: Path,
    pdf_hash: str,
    dpi: int = 200,
    clip: "pymupdf.Rect | None" = None,
    codec: str = "png",
    quality: int = 0,
) -> dict[str, Any]:
    """
    Render a PDF page (or a clipped region of it) as an image file.

    Args:
        doc: PyMuPDF document object
        page_num: Page number (0-indexed)
        output_dir: Directory to save the image
        pdf_hash: Hash prefix for deterministic filenames
        dpi: Render resolution (default 200)
        clip: Optional region rectangle (page points). When set, only that
            region is rendered at `dpi`, and the filename carries a clip token
            so clipped and full renders never collide on disk.
        codec: "png" (lossless, default) or "jpeg" (lossy). JPEG is used only
            where a PNG has already failed the transport byte budget, so a
            page never silently loses fidelity that it did not need to lose.
        quality: JPEG quality 1-100. Ignored for PNG, which stores 0 as the
            not-applicable sentinel so its cache key stays stable.

    Returns:
        Dict with file_path_on_disk, size_bytes, width, height, codec, quality

    Raises:
        ValueError: If codec is not "png" or "jpeg".
    """
    if codec not in _RENDER_CODECS:
        raise ValueError(
            f"Unsupported render codec {codec!r};"
            f" expected one of {sorted(_RENDER_CODECS)}"
        )

    page = doc[page_num]
    if clip is not None:
        pix = page.get_pixmap(dpi=dpi, clip=clip)
        token = f"_clip{int(clip.x0)}-{int(clip.y0)}-{int(clip.x1)}-{int(clip.y1)}"
    else:
        pix = page.get_pixmap(dpi=dpi)
        token = ""

    # Quality participates in the filename so q80 and q60 renders of the same
    # page coexist; PNG keeps its historical name so existing cache rows and
    # on-disk files stay valid.
    qual_token = f"_q{quality}" if codec == "jpeg" else ""
    ext = _RENDER_CODECS[codec]
    file_name = f"{pdf_hash}_p{page_num}_render_{dpi}dpi{token}{qual_token}{ext}"
    file_path = output_dir / file_name
    tmp_path = file_path.with_name(f"{file_name}.{os.getpid()}.tmp")
    try:
        # Explicit output=: pix.save() otherwise infers format from the file
        # extension, and ".tmp" isn't a recognized image format.
        if codec == "jpeg":
            pix.save(str(tmp_path), output="jpeg", jpg_quality=quality)
        else:
            pix.save(str(tmp_path), output="png")
        os.chmod(str(tmp_path), 0o600)
        # Atomic on POSIX and Windows: a concurrent writer (e.g. an orphaned
        # pool worker rendering the same deterministic path) can no longer
        # produce a torn/locked file — last writer wins as a whole.
        os.replace(str(tmp_path), str(file_path))
    except Exception as e:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        logger.warning("Failed to save render for page %d: %s", page_num, e)
        raise

    return {
        "file_path_on_disk": str(file_path),
        "size_bytes": file_path.stat().st_size,
        "width": pix.width,
        "height": pix.height,
        "codec": codec,
        "quality": quality,
    }


def render_page_as_png(
    doc: pymupdf.Document,
    page_num: int,
    output_dir: Path,
    pdf_hash: str,
    dpi: int = 200,
    clip: "pymupdf.Rect | None" = None,
) -> dict[str, Any]:
    """Render a page as PNG. Thin wrapper over render_page_as_image kept so
    the five existing call sites (including the picklable spawn-pool worker)
    are untouched."""
    return render_page_as_image(
        doc, page_num, output_dir, pdf_hash, dpi, clip, codec="png", quality=0
    )


# A page qualifies as a pure scan only when a raster covers essentially all of
# it and nothing else is drawn. Both thresholds are deliberately strict: a
# false positive here caps resolution on a page that would genuinely get
# sharper, which is a silent quality regression.
_SCAN_COVERAGE_MIN = 0.98


def native_render_dpi_cap(doc: pymupdf.Document, page_num: int) -> "int | None":
    """
    Native raster resolution of a page that is a single full-page image.

    Returns the effective DPI of the embedded raster across the page width, or
    None when the page is not a pure scan (has text, has vector drawings, has
    no image, has several images, or the image does not cover the page).
    Rendering such a page above its native DPI upsamples: it costs bytes and
    carries no additional information.

    Fail-safe: any PyMuPDF error returns None, i.e. no cap.
    """
    try:
        page = doc[page_num]
        if page.get_text().strip():
            return None
        if page.get_drawings():
            return None

        images = page.get_images(full=True)
        if len(images) != 1:
            return None

        xref = images[0][0]
        rects = page.get_image_rects(xref)
        if not rects:
            return None

        page_area = page.rect.width * page.rect.height
        if page_area <= 0:
            return None
        covered = sum(r.width * r.height for r in rects)
        if covered / page_area < _SCAN_COVERAGE_MIN:
            return None

        info = doc.extract_image(xref)
        width_pt = page.rect.width
        if width_pt <= 0:
            return None
        return int(round(info["width"] / (width_pt / 72.0)))
    except Exception:  # noqa: BLE001 - fail-safe: no cap rather than a crash
        return None


def check_tesseract_available() -> None:
    """
    Verify Tesseract binary is on PATH, and cache tessdata path.

    Raises:
        RuntimeError: If tesseract binary is not found or returns non-zero.
    """
    import subprocess

    global _TESSDATA_PATH  # noqa: PLW0603

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
            "apt install tesseract-ocr (Linux) / "
            "winget install Tesseract-OCR (Windows). "
            "See https://tesseract-ocr.github.io/tessdoc/Installation.html. "
            "If OCR returns empty for a page with visible text, also verify "
            "the language pack: tesseract --list-langs"
        ) from exc

    if _TESSDATA_PATH is None:
        _TESSDATA_PATH = _resolve_tessdata()


def ocr_page(
    doc: pymupdf.Document,
    page_num: int,
    lang: str = "eng",
    dpi: int = 300,
    tessdata: str | None = None,
) -> str:
    """
    OCR a PDF page using PyMuPDF's built-in Tesseract binding.

    Passes tessdata path explicitly if available, bypassing PyMuPDF's
    fragile shell=True subprocess discovery (which crashes in spawned
    worker processes on Windows).

    Args:
        doc: PyMuPDF document object
        page_num: Page number (0-indexed)
        lang: Tesseract language code (default 'eng')
        dpi: Internal render DPI for OCR (fixed at 300 for v1; not user-configurable
             to keep the surface minimal — expose as parameter in a future release
             if user feedback demands finer control)
        tessdata: Explicit tessdata directory path. When None, PyMuPDF
            auto-discovers (may use shell subprocesses).

    Returns:
        Extracted text string (empty string if OCR produces nothing)
    """
    page = doc[page_num]
    textpage = page.get_textpage_ocr(language=lang, dpi=dpi, tessdata=tessdata)
    return str(page.get_text(textpage=textpage))


def _ocr_page_worker(
    args: tuple[str, int, str, int, str | None],
) -> tuple[int, "str | PageError"]:
    """Picklable OCR worker for ProcessPoolExecutor.

    Opens its OWN Document (PyMuPDF documents are not shareable across
    processes) and isolates per-page failure as a PageError so one bad page
    never crashes the batch. Lives in extractor.py (not server.py) so spawn
    re-imports only PyMuPDF, never FastMCP.

    Args tuple: (path, page_num, lang, dpi, tessdata)

    The tuple unpack is inside the try so a malformed tuple (e.g. from a
    stale caller) produces a PageError instead of crashing the whole batch.
    """
    page_num = args[1]  # safe fallback when unpack fails
    try:
        path, page_num, lang, dpi, tessdata = args
        doc = pymupdf.open(path)
        try:
            return page_num, ocr_page(
                doc, page_num, lang=lang, dpi=dpi, tessdata=tessdata
            )
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
    page_num = args[1]  # safe fallback when unpack fails
    try:
        path, page_num, out_dir, pdf_hash, dpi = args
        doc = pymupdf.open(path)
        try:
            info = render_page_as_png(doc, page_num, Path(out_dir), pdf_hash, dpi)
            return page_num, info
        finally:
            doc.close()
    except Exception as exc:  # noqa: BLE001 - deliberate per-page isolation
        return page_num, PageError(repr(exc))


def _warm_extract_worker(
    path: str,
) -> tuple[int, dict[str, Any], list[Any], dict[int, str], list[dict[str, int]]]:
    """Picklable whole-doc extraction worker for concurrent corpus warm.

    Extracts everything one doc needs (metadata, TOC, per-page text,
    coverage) with NO cache access, so the parent can write atomically
    after the fact. Raises on failure; the parent maps the exception to
    a ``skipped`` entry. Lives in extractor.py so spawn re-imports only
    PyMuPDF, never FastMCP (same rule as the per-page workers above).
    Coverage counts use raw ``get_text()`` chars, matching pdf_info.
    """
    doc = pymupdf.open(path)
    try:
        page_count = len(doc)
        metadata = extract_metadata(doc)
        toc = extract_toc(doc)
        texts: dict[int, str] = {}
        coverage: list[dict[str, int]] = []
        for pn in range(page_count):
            page = doc[pn]
            texts[pn] = extract_text_from_page(page, sort_by_position=True)
            coverage.append(
                {
                    "page": pn + 1,
                    "text_chars": len(page.get_text()),
                    "raster_images": len({img[0] for img in page.get_images()}),
                }
            )
    finally:
        doc.close()
    return page_count, metadata, toc, texts, coverage


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


#: Bump when table extraction logic or response shape changes. Cached
#: `page_tables` rows carrying an older version are ignored and re-extracted.
#: 2: tables are extracted in an isolated spawn process. Version 1 rows were
#: produced in a process that had imported ``pymupdf4llm``, which activates
#: ``pymupdf.layout`` and corrupts ``find_tables`` cell text -- decimal points
#: detach from their numbers ("4.5" -> "45\n."), so every cached numeric cell
#: from that era is untrustworthy and must be discarded.
TABLE_EXTRACTION_VERSION = 2


def _extract_tables_worker(
    args: tuple[str, list[int]],
) -> dict[int, "list[dict[str, Any]] | PageError"]:
    """Picklable worker extracting tables for several pages of one document.

    MUST run in a ``spawn`` process, never ``fork``. Importing
    ``pymupdf4llm`` (which the server does at startup, and which any
    column-aware text extraction triggers) executes
    ``use_layout(True)`` -> ``import pymupdf.layout``, and that import alone
    swaps PyMuPDF's text engine process-wide and irreversibly. A forked
    child inherits that state and would return the same corrupt cells; only
    a spawned child re-imports a clean PyMuPDF.

    Batched over pages so one process spawn (~200-500ms) serves a whole
    ``pdf_read_pages`` call rather than being paid per page.

    Per-page failure is isolated as a PageError so one bad page cannot cost
    the batch. A failure is never silently downgraded to "no tables": the
    parent must not confuse "extraction failed" with "this page has none".
    """
    path, page_nums = args
    out: dict[int, list[dict[str, Any]] | PageError] = {}
    doc = pymupdf.open(path)
    try:
        for page_num in page_nums:
            try:
                out[page_num] = extract_tables_from_page(doc[page_num])
            except Exception as exc:  # noqa: BLE001 - per-page isolation
                out[page_num] = PageError(repr(exc))
    finally:
        doc.close()
    return out


def extract_tables_from_page(page: Any) -> list[dict[str, Any]]:
    """
    Extract tables from a PDF page using PyMuPDF's table finder.

    .. warning::
       Calling this in a process that has imported ``pymupdf4llm`` returns
       corrupted cell text. Use ``_extract_tables_worker`` via an isolated
       spawn process instead. See TABLE_EXTRACTION_VERSION.

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
                    "bbox": [round(v, 1) for v in table.bbox],
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
