"""Content-trust / hidden-text detection.

Pure detection utilities over PyMuPDF pages. The safety boundary is the
GEOMETRY layer: "this PDF contains text the human reader cannot see."
Lexical phrase matching (Task 2) is a best-effort English severity hint
gated behind hidden geometry, never the trigger.

Geometry source is page.get_texttrace(), the only PyMuPDF API that exposes
text render mode (type) and true constant alpha (opacity). get_text("rawdict")
cannot distinguish invisible render mode from transparent fill, so it is NOT
used here.
"""

from __future__ import annotations

from typing import Any

import pymupdf

# Detection-logic version. Bump when geometry rules / thresholds change so the
# cache layer (cache.py) re-scans. See cache._TRUST_VERSION wiring.
_TRUST_VERSION = 1

# Tuned in the benchmark loop (scripts/benchmark_content_trust.py).
_MIN_HIDDEN_CHARS = 8  # ignore stray invisible glyphs below this length
_TINY_FONT_PT = 1.0  # font size (pt) at/below which text is unreadable
_OPACITY_EPS = 0.05  # opacity at/below this counts as transparent
_WHITE_THRESHOLD = 0.95  # min per-channel value to call a color "white-ish"
_OCR_COVERAGE_RATIO = 0.8  # image coverage of an invisible span => OCR layer

HiddenSpan = dict[str, Any]


def _is_white(color: Any) -> bool:
    """True if an RGB tuple is near-white on every channel."""
    try:
        return all(float(c) >= _WHITE_THRESHOLD for c in color)
    except (TypeError, ValueError):
        return False


def _image_bboxes(page: pymupdf.Page) -> list[pymupdf.Rect]:
    try:
        return [pymupdf.Rect(im["bbox"]) for im in page.get_image_info()]
    except Exception:
        return []


def _covered_by_image(span_rect: pymupdf.Rect, images: list[pymupdf.Rect]) -> bool:
    area = span_rect.get_area()
    if area <= 0:
        return False
    for im in images:
        inter = span_rect & im
        if inter.get_area() / area >= _OCR_COVERAGE_RATIO:
            return True
    return False


def _scan_page_geometry(page: pymupdf.Page, page_index: int) -> list[HiddenSpan]:
    """Return hidden spans on one page. page_index is 0-indexed."""
    page_rect = page.rect
    images = _image_bboxes(page)
    spans: list[HiddenSpan] = []

    for s in page.get_texttrace():
        chars = s.get("chars", [])
        if len(chars) < _MIN_HIDDEN_CHARS:
            continue

        stype = s.get("type", 0)
        opacity = float(s.get("opacity", 1.0))
        size = float(s.get("size", 12.0))
        color = s.get("color", (0.0, 0.0, 0.0))
        bbox = tuple(float(c) for c in s.get("bbox", (0, 0, 0, 0)))
        span_rect = pymupdf.Rect(bbox)

        reasons: list[str] = []

        if stype == 3 and not _covered_by_image(span_rect, images):
            reasons.append("invisible_render")
        if size <= _TINY_FONT_PT:
            reasons.append("tiny_font")
        # stroke-only (type==1) is VISIBLE outlined text; only fill/fill+stroke
        # can be made transparent and thus invisible.
        if stype in (0, 2) and opacity <= _OPACITY_EPS:
            reasons.append("transparent")
        if stype in (0, 2) and opacity > _OPACITY_EPS and _is_white(color):
            reasons.append("white_on_white")
        if (span_rect & page_rect).get_area() <= 0:
            reasons.append("offpage")

        if not reasons:
            continue

        text = "".join(chr(c[0]) for c in chars)
        spans.append(
            {
                "page": page_index,
                "reasons": reasons,
                "text": text,
                "bbox": bbox,
                "font_size": size,
                "opacity": opacity,
                "char_count": len(chars),
            }
        )

    return spans
