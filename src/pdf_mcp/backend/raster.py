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


class Pixmap:
    """PyMuPDF-Pixmap-shaped wrapper over a Pillow image.

    render_page_as_image reads .width/.height/.n and calls
    .save(path, output=..., jpg_quality=...); extract_images_from_page
    additionally branches colour format on .n. Only that surface exists.
    """

    def __init__(self, image: Any) -> None:
        self._image = image
        self.width = int(image.width)
        self.height = int(image.height)
        self.n = len(image.getbands())

    def to_pil(self) -> Any:
        return self._image

    def save(self, path: str, output: str | None = None, jpg_quality: int = 0) -> None:
        fmt = (output or "png").upper()
        if fmt in ("JPG", "JPEG"):
            image = self._image
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            image.save(path, format="JPEG", quality=jpg_quality or 75)
        else:
            self._image.save(path, format="PNG")


def render_pixmap(
    pdf_path: str,
    page_num: int,
    dpi: int = 150,
    clip: Rect | tuple[float, float, float, float] | None = None,
) -> Pixmap:
    return Pixmap(render_page(pdf_path, page_num, dpi=dpi, clip=clip))


def extract_images(pdf_path: str, page_num: int) -> list[dict[str, Any]]:
    """Distinct embedded raster images, decoded, with their placements.

    Decoding goes through pdfium's bitmap (render=False: the image's own
    pixels, untransformed), not the raw stream bytes: raw data is
    filter-encoded (DCT, Flate over a predictor, ...) and only pdfium
    knows the full decode chain. Deduplication is by raw-stream hash,
    matching Page.get_images, so one logo placed four times extracts
    once with four placements.
    """
    import hashlib

    import pypdfium2.raw as pdfium_raw

    doc = pdfium.PdfDocument(pdf_path)
    try:
        page = doc[page_num]
        from .pagespace import page_transform

        x_off, y_top = page_transform(page)
        out: list[dict[str, Any]] = []
        seen: dict[int, dict[str, Any]] = {}
        for obj in page.get_objects(filter=[pdfium_raw.FPDF_PAGEOBJ_IMAGE]):
            try:
                raw = obj.get_data(decode_simple=False)
                key = int.from_bytes(hashlib.sha256(bytes(raw)).digest()[:6], "big")
            except Exception:  # noqa: BLE001 - fall back to identity by position
                key = id(obj)
            left, bottom, right, top = obj.get_bounds()
            bbox = (left - x_off, y_top - top, right - x_off, y_top - bottom)
            if key in seen:
                seen[key]["placements"].append(bbox)
                continue
            try:
                image = obj.get_bitmap(render=False).to_pil()
            except Exception:  # noqa: BLE001 - one undecodable image
                continue
            # pdfium's bitmap is RGB(A) regardless of the source
            # colorspace, and PyMuPDF reported grayscale sources as
            # grayscale, which pdf_read_pages' image dicts rely on. The
            # colorspace enum cannot carry that signal (an ICC-wrapped
            # gray reports ICCBASED, exactly like an ICC RGB); bits per
            # PIXEL can: 8 or less means one channel.
            try:
                bpp = int(obj.get_metadata().bits_per_pixel)
            except Exception:  # noqa: BLE001
                bpp = 0
            if 0 < bpp <= 8 and image.mode not in ("L", "LA"):
                image = image.convert("L")
            entry = {
                "key": key,
                "image": Pixmap(image),
                "placements": [bbox],
            }
            seen[key] = entry
            out.append(entry)
        return out
    finally:
        doc.close()


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
    import math

    doc = pdfium.PdfDocument(pdf_path)
    try:
        page = doc[page_num]
        image = page.render(scale=dpi / 72.0).to_pil()
        if clip is not None:
            # Crop in PIXELS from the full render, with PyMuPDF's
            # enclosing-rect rounding (floor the origin, ceil the far
            # edge). pdfium's own crop insets round each edge down
            # independently, which came out 2px smaller than PyMuPDF's
            # render of the same rect and failed the halo-render
            # pixel-diff test on shape alone.
            scale = dpi / 72.0
            x0, y0, x1, y1 = (float(v) for v in clip)
            left = max(0, math.floor(x0 * scale))
            top = max(0, math.floor(y0 * scale))
            right = min(image.width, math.ceil(x1 * scale))
            bottom = min(image.height, math.ceil(y1 * scale))
            image = image.crop((left, top, right, bottom))
        return image
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
