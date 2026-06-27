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
_TRUST_VERSION = 2

# Tuned in the benchmark loop (scripts/benchmark_content_trust.py).
# CJK text is split into short per-font spans by PyMuPDF (e.g. 4-char runs);
# floor=3 catches meaningful CJK injections while ignoring 1-2 char strays.
_MIN_HIDDEN_CHARS = 3  # ignore stray invisible glyphs below this length
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
    except (RuntimeError, AttributeError, KeyError, TypeError, ValueError):
        # Best-effort guard: RuntimeError covers PyMuPDF/mupdf-level errors;
        # the rest cover a malformed image-info dict. A flaky page must not
        # break detection, so fall back to "no images".
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


_SPAN_CAP = 200
_SPAN_TEXT_CAP = 200

# Best-effort English instruction patterns. NOT a detector — only counted
# over already-hidden span text (severity hint). Conservative on purpose.
_INJECTION_PHRASES: tuple[str, ...] = (
    "ignore previous instructions",
    "ignore all previous instructions",
    "disregard the above",
    "disregard previous",
    "system prompt",
    "you are now",
    "new instructions",
    "do not tell the user",
)

_SIGNAL_KEYS = (
    "invisible_render",
    "tiny_font",
    "transparent",
    "white_on_white",
    "offpage",
)


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _count_injection_in_hidden(spans: list[HiddenSpan]) -> int:
    blob = _normalize(" ".join(s["text"] for s in spans))
    return sum(1 for phrase in _INJECTION_PHRASES if phrase in blob)


def scan_document(doc: pymupdf.Document) -> dict[str, Any]:
    """Full document scan. Best-effort: a page that throws is counted in
    pages_errored and contributes nothing."""
    all_spans: list[HiddenSpan] = []
    pages_flagged: set[int] = set()
    signals = {k: 0 for k in _SIGNAL_KEYS}
    pages_errored = 0

    for i in range(doc.page_count):
        try:
            spans = _scan_page_geometry(doc[i], i)
            if spans:
                pages_flagged.add(i + 1)  # 1-indexed
                for s in spans:
                    for r in s["reasons"]:
                        signals[r] = signals.get(r, 0) + 1
                all_spans.extend(spans)
        except Exception:
            pages_errored += 1
            continue

    return {
        "suspicious": bool(all_spans),
        "hidden_text_runs": len(all_spans),
        "hidden_chars": sum(s["char_count"] for s in all_spans),
        "injection_in_hidden": _count_injection_in_hidden(all_spans),
        "pages_flagged": sorted(pages_flagged),
        "signals": signals,
        "pages_errored": pages_errored,
        "spans": all_spans,
        "trust_version": _TRUST_VERSION,
    }


def summarize(scan: dict[str, Any], detail: bool) -> dict[str, Any]:
    """Shape the public content_trust block from a raw scan()."""
    block: dict[str, Any] = {
        "suspicious": scan["suspicious"],
        "hidden_text_runs": scan["hidden_text_runs"],
        "hidden_chars": scan["hidden_chars"],
        "injection_in_hidden": scan["injection_in_hidden"],
        "pages_flagged": scan["pages_flagged"],
        "signals": scan["signals"],
        "pages_errored": scan["pages_errored"],
        "detail_included": detail,
    }
    if detail:
        raw = scan["spans"]
        block["spans_truncated"] = len(raw) > _SPAN_CAP
        block["spans"] = [
            {
                "page": s["page"] + 1,  # 1-indexed for the public payload
                "reason": s["reasons"],
                "text": s["text"][:_SPAN_TEXT_CAP],
                "bbox": s["bbox"],
                "font_size": s["font_size"],
                "opacity": s["opacity"],
            }
            for s in raw[:_SPAN_CAP]
        ]
    return block


def page_has_hidden_text(page: pymupdf.Page) -> bool:
    """Lightweight geometry-only check for the read-path flag.
    Page index is irrelevant here (no aggregation), pass 0."""
    try:
        return bool(_scan_page_geometry(page, 0))
    except Exception:
        return False
