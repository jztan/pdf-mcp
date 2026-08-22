"""`page.get_texttrace()` reconstructed over pypdfium2 raw ctypes.

`content_trust.py` is the hidden-text / prompt-injection detector - a
safety boundary - and was excluded from this spike until now. It depends
on `get_texttrace()`, which its own module docstring calls "the only
PyMuPDF API exposing text render mode", explicitly noting `rawdict`
cannot substitute.

Per span, `_scan_page_geometry` consumes exactly six fields (grepped, not
assumed): `chars`, `type` (render mode), `opacity`, `size`, `color`,
`bbox`. This module reproduces that shape from pdfium page objects:

| field | pdfium source |
| --- | --- |
| `type` | `FPDFTextObj_GetTextRenderMode` |
| `size` | `FPDFTextObj_GetFontSize` |
| `color` + `opacity` | `FPDFPageObj_GetFillColor` (alpha carries ExtGState `ca`) |
| `bbox` | `FPDFPageObj_GetBounds`, y-flipped |
| `chars` | `FPDFTextObj_GetText` via a page-level text handle |

`benchmark_data/content_trust_corpus/` is the point: 14 fixtures split
into deliberate attacks (invisible, white-on-white, transparent, tiny,
off-page, in English and CJK) and clean controls including a stray glyph
and an OCR layer. A safety detector has to keep BOTH halves right;
catching every attack by flagging everything is not a pass, so the
tests assert the clean controls stay clean.
"""

from __future__ import annotations

import ctypes
from typing import Any

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c


def _bounds(obj: Any) -> tuple[float, float, float, float]:
    left, bottom, right, top = (ctypes.c_float() for _ in range(4))
    pdfium_c.FPDFPageObj_GetBounds(
        obj,
        ctypes.byref(left),
        ctypes.byref(bottom),
        ctypes.byref(right),
        ctypes.byref(top),
    )
    return (left.value, bottom.value, right.value, top.value)


def _fill_rgba(obj: Any) -> tuple[int, int, int, int]:
    r, g, b, a = (ctypes.c_uint() for _ in range(4))
    ok = pdfium_c.FPDFPageObj_GetFillColor(
        obj, ctypes.byref(r), ctypes.byref(g), ctypes.byref(b), ctypes.byref(a)
    )
    if not ok:
        return (0, 0, 0, 255)
    return (r.value, g.value, b.value, a.value)


def _obj_text(obj: Any, textpage: Any) -> str:
    """UTF-16LE text of one text object, via the page's text handle."""
    n = pdfium_c.FPDFTextObj_GetText(obj, textpage, None, 0)
    if n <= 2:  # just the NUL terminator
        return ""
    buf = ctypes.create_string_buffer(n)
    pdfium_c.FPDFTextObj_GetText(
        obj, textpage, ctypes.cast(buf, ctypes.POINTER(ctypes.c_ushort)), n
    )
    return buf.raw[: n - 2].decode("utf-16-le", errors="replace")


def _spans_for_page(page: Any) -> list[dict[str, Any]]:
    """PyMuPDF-`get_texttrace()`-shaped spans, restricted to the fields
    content_trust.py actually reads."""
    from .pagespace import page_transform

    x_off, y_top = page_transform(page)
    textpage = page.get_textpage()
    n_objs = pdfium_c.FPDFPage_CountObjects(page.raw)
    out: list[dict[str, Any]] = []

    for i in range(n_objs):
        obj = pdfium_c.FPDFPage_GetObject(page.raw, i)
        if pdfium_c.FPDFPageObj_GetType(obj) != pdfium_c.FPDF_PAGEOBJ_TEXT:
            continue

        text = _obj_text(obj, textpage.raw)
        if not text:
            continue

        size = ctypes.c_float()
        pdfium_c.FPDFTextObj_GetFontSize(obj, ctypes.byref(size))
        mode = pdfium_c.FPDFTextObj_GetTextRenderMode(obj)
        r, g, b, a = _fill_rgba(obj)
        x0, y0_pdf, x1, y1_pdf = _bounds(obj)

        out.append(
            {
                # PyMuPDF reports render mode as an int; pdfium's enum uses
                # the same PDF `Tr` numbering (3 == invisible), which is the
                # value content_trust.py compares against.
                "type": int(mode),
                "size": float(size.value),
                # PyMuPDF gives colour as 0..1 floats and opacity separately.
                # pdfium folds ExtGState `ca` into the fill alpha channel, so
                # alpha/255 reconstructs `opacity`.
                "color": (round(r / 255, 4), round(g / 255, 4), round(b / 255, 4)),
                "opacity": round(a / 255, 4),
                # y-flip to top-left space, matching PyMuPDF's page rect.
                "bbox": (x0 - x_off, y_top - y1_pdf, x1 - x_off, y_top - y0_pdf),
                # Each entry's [0] must be an integer CODEPOINT, not a
                # character: content_trust.py reconstructs the span text via
                # `chr(c[0])` (line ~157) as well as taking len(chars). A
                # first version emitted the character itself and every attack
                # fixture raised TypeError inside _scan_page_geometry, which
                # swallows per-page exceptions into `pages_errored` - so the
                # detector silently reported "not suspicious" for all 10
                # attacks rather than failing loudly.
                "chars": [(ord(c), 0.0, 0.0, 0.0) for c in text],
            }
        )
    return out


def get_texttrace(pdf_path: str, page_num: int) -> list[dict[str, Any]]:
    doc = pdfium.PdfDocument(pdf_path)
    try:
        return _spans_for_page(doc[page_num])
    finally:
        doc.close()


__all__ = ["get_texttrace"]
