"""Page rasterisation and OCR.

Rendering goes through pypdfium2. OCR goes through pytesseract, which
drives the same Tesseract binary PyMuPDF's binding drives, so this
swaps the wrapper rather than the engine.
"""

from __future__ import annotations

from typing import Any

import pypdfium2 as pdfium

from .geometry import Rect

#: Below this many characters a page is treated as having no usable text
#: layer, so OCR runs. A handful of stray glyphs on a scan (a page number
#: stamped by the scanner) must not suppress it.
_TEXT_LAYER_MIN_CHARS = 32


def render_page(
    pdf_path: str,
    page_num: int,
    dpi: int = 150,
    clip: Rect | tuple[float, float, float, float] | None = None,
) -> Any:
    """Render one page to a Pillow image.

    `clip` is an absolute rectangle in PyMuPDF's top-left page space, as
    every caller in pdf_mcp supplies. pypdfium2's own `crop` argument is
    insets in points from each edge, so passing a rect straight through
    renders the wrong region silently instead of raising.
    """
    doc = pdfium.PdfDocument(pdf_path)
    try:
        page = doc[page_num]
        crop = (0.0, 0.0, 0.0, 0.0)
        if clip is not None:
            width, height = page.get_size()
            x0, y0, x1, y1 = (float(v) for v in clip)
            crop = (
                max(0.0, x0),
                max(0.0, height - y1),
                max(0.0, width - x1),
                max(0.0, y0),
            )
        return page.render(scale=dpi / 72.0, crop=crop).to_pil()
    finally:
        doc.close()


def _text_layer(pdf_path: str, page_num: int) -> str:
    from .text import get_text

    try:
        return str(get_text(pdf_path, page_num, "text"))
    except Exception:  # noqa: BLE001 - absence of text is not an error here
        return ""


def ocr_page_text(
    pdf_path: str,
    page_num: int,
    lang: str = "eng",
    dpi: int = 300,
    tessdata: str | None = None,
    full: bool = False,
) -> str:
    """Text for one page, OCRing only when there is nothing to read.

    `full=False` mirrors PyMuPDF's get_textpage_ocr default, which
    returns the EXISTING text layer on a born-digital page rather than
    OCRing it. Preserving that is both parity and the better behaviour:
    OCRing clean text loses accuracy and costs seconds per page.

    It is also the trap that invalidated the spike's OCR benchmark. A
    page with a text layer never reaches Tesseract, so timing OCR on one
    measures the text extractor, not OCR. The spike reported OCR as 8 to
    9.5x slower on that basis.

    Measured here on a genuinely image-only page (gao-cloud p3
    rasterised at 200 dpi, then OCRed at 300): 0.79s for PyMuPDF against
    1.24s through pytesseract, so 1.56x, for 332 words either way and a
    98.8% word-set overlap. Both drive the same Tesseract binary, and
    server.py already parallelises OCR across pages.
    """
    if not full:
        existing = _text_layer(pdf_path, page_num)
        if len(existing.strip()) >= _TEXT_LAYER_MIN_CHARS:
            return existing

    import pytesseract

    image = render_page(pdf_path, page_num, dpi=dpi)
    config = f"--tessdata-dir {tessdata}" if tessdata else ""
    return str(pytesseract.image_to_string(image, lang=lang, config=config))


def page_images(pdf_path: str, page_num: int) -> list[dict[str, Any]]:
    """Raster image placements with a stable key per distinct image.

    pdfium exposes no xref, and callers use one to DEDUPE: pdf_info
    counts distinct raster images per page, so keying on the page-object
    index would count one logo placed four times as four images. The key
    is a hash of the image's RAW stream bytes instead, so repeated
    placements of the same XObject collapse exactly as an xref would.
    """
    import ctypes
    import hashlib

    import pypdfium2.raw as pdfium_raw

    doc = pdfium.PdfDocument(pdf_path)
    try:
        page = doc[page_num]
        from .pagespace import page_transform

        x_off, y_top = page_transform(page)
        out: list[dict[str, Any]] = []
        for index in range(pdfium_raw.FPDFPage_CountObjects(page.raw)):
            obj = pdfium_raw.FPDFPage_GetObject(page.raw, index)
            if pdfium_raw.FPDFPageObj_GetType(obj) != pdfium_raw.FPDF_PAGEOBJ_IMAGE:
                continue

            size = pdfium_raw.FPDFImageObj_GetImageDataRaw(obj, None, 0)
            raw = b""
            if size > 0:
                buf = ctypes.create_string_buffer(size)
                pdfium_raw.FPDFImageObj_GetImageDataRaw(obj, buf, size)
                raw = buf.raw[:size]

            meta = pdfium_raw.FPDF_IMAGEOBJ_METADATA()
            pdfium_raw.FPDFImageObj_GetImageMetadata(obj, page.raw, ctypes.byref(meta))

            left, bottom, right, top = (ctypes.c_float() for _ in range(4))
            pdfium_raw.FPDFPageObj_GetBounds(
                obj,
                ctypes.byref(left),
                ctypes.byref(bottom),
                ctypes.byref(right),
                ctypes.byref(top),
            )
            key = (
                int.from_bytes(hashlib.sha256(raw).digest()[:6], "big")
                if raw
                else index
            )
            out.append(
                {
                    "key": key,
                    "bbox": (
                        left.value - x_off,
                        y_top - top.value,
                        right.value - x_off,
                        y_top - bottom.value,
                    ),
                    "width": int(meta.width),
                    "height": int(meta.height),
                    "bpc": int(meta.bits_per_pixel),
                    "raw": raw,
                }
            )
        return out
    finally:
        doc.close()


def get_image_info(pdf_path: str, page_num: int) -> list[dict[str, Any]]:
    """Raster image placements on a page, PyMuPDF's get_image_info shape.

    content_trust needs these to exempt an OCR text layer: OCR text is
    genuinely invisible (render mode 3) and sits over the scan it
    describes, so without the image bboxes _covered_by_image cannot tell
    it apart from an injected invisible span, and a clean scanned
    document is reported as an attack.
    """
    import ctypes

    import pypdfium2.raw as pdfium_raw

    doc = pdfium.PdfDocument(pdf_path)
    try:
        page = doc[page_num]
        from .pagespace import page_transform

        x_off, y_top = page_transform(page)
        out: list[dict[str, Any]] = []
        for index in range(pdfium_raw.FPDFPage_CountObjects(page.raw)):
            obj = pdfium_raw.FPDFPage_GetObject(page.raw, index)
            if pdfium_raw.FPDFPageObj_GetType(obj) != pdfium_raw.FPDF_PAGEOBJ_IMAGE:
                continue
            left, bottom, right, top = (ctypes.c_float() for _ in range(4))
            pdfium_raw.FPDFPageObj_GetBounds(
                obj,
                ctypes.byref(left),
                ctypes.byref(bottom),
                ctypes.byref(right),
                ctypes.byref(top),
            )
            out.append(
                {
                    "number": len(out),
                    # y-flipped into PyMuPDF's top-left page space.
                    "bbox": (
                        left.value - x_off,
                        y_top - top.value,
                        right.value - x_off,
                        y_top - bottom.value,
                    ),
                }
            )
        return out
    finally:
        doc.close()
