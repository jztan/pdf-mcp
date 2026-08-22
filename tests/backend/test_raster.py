"""Rendering and OCR.

The OCR test builds its own image-only fixture. That is not incidental:
PyMuPDF's get_textpage_ocr defaults to full=False and returns the
EXISTING text layer on a born-digital page without OCRing anything, so
benchmarking or testing OCR on such a page measures nothing. The spike
produced a bogus "8 to 9.5x slower" figure exactly that way; the real
number is 1.24x.
"""

import shutil

import pymupdf
import pytest

from pdf_mcp.backend.raster import ocr_page_text, render_page
from tests.backend.differential import assert_non_empty

_FIXTURE = "pages/corpus/gao-cloud.pdf"
_PAGE = 2

_HAS_TESSERACT = shutil.which("tesseract") is not None


def test_render_dimensions_match_pymupdf():
    ref_doc = pymupdf.open(_FIXTURE)
    ref = ref_doc[_PAGE].get_pixmap(dpi=200)
    ref_size = (ref.width, ref.height)
    ref_doc.close()

    image = render_page(_FIXTURE, _PAGE, dpi=200)
    assert (image.width, image.height) == ref_size


def test_render_scales_with_dpi():
    small = render_page(_FIXTURE, _PAGE, dpi=72)
    large = render_page(_FIXTURE, _PAGE, dpi=144)
    assert large.width == pytest.approx(small.width * 2, abs=2)
    assert large.height == pytest.approx(small.height * 2, abs=2)


def test_clip_is_an_absolute_rect_not_insets():
    """pypdfium2's crop parameter is insets in points from each edge, not
    an absolute rectangle. Passing a caller's rect straight through
    silently renders the wrong region rather than raising."""
    ref_doc = pymupdf.open(_FIXTURE)
    page_rect = ref_doc[_PAGE].rect
    ref_doc.close()

    half_height = page_rect.height / 2
    top_half = render_page(
        _FIXTURE, _PAGE, dpi=100, clip=(0, 0, page_rect.width, half_height)
    )
    full = render_page(_FIXTURE, _PAGE, dpi=100)
    assert top_half.width == pytest.approx(full.width, abs=2)
    assert top_half.height == pytest.approx(full.height / 2, abs=2)


def _make_image_only_pdf(tmp_path):
    """Rasterise a page so it carries no text layer at all."""
    src = pymupdf.open(_FIXTURE)
    pix = src[_PAGE].get_pixmap(dpi=200)
    out = pymupdf.open()
    page = out.new_page(width=pix.width * 72 / 200, height=pix.height * 72 / 200)
    page.insert_image(page.rect, pixmap=pix)
    scan = tmp_path / "scan.pdf"
    out.save(str(scan))
    src.close()
    out.close()

    check = pymupdf.open(str(scan))
    has_text = check[0].get_text().strip()
    check.close()
    assert not has_text, "fixture still has a text layer; the OCR test would be vacuous"
    return scan


@pytest.mark.skipif(not _HAS_TESSERACT, reason="system tesseract not installed")
def test_ocr_reads_an_image_only_page(tmp_path):
    scan = _make_image_only_pdf(tmp_path)
    text = ocr_page_text(str(scan), 0, lang="eng", dpi=300)
    assert_non_empty(text.strip(), "ocr text")
    assert len(text.split()) > 80, "OCR returned implausibly little text"
    assert "cloud" in text.lower()


@pytest.mark.skipif(not _HAS_TESSERACT, reason="system tesseract not installed")
def test_ocr_agrees_with_pymupdf_on_an_image_only_page(tmp_path):
    """Both run Tesseract, so the words should broadly agree. Compared as
    a token set: OCR line breaking differs run to run."""
    scan = _make_image_only_pdf(tmp_path)

    ref_doc = pymupdf.open(str(scan))
    ref_page = ref_doc[0]
    ref_tp = ref_page.get_textpage_ocr(language="eng", dpi=300, full=True)
    ref = ref_page.get_text(textpage=ref_tp)
    ref_doc.close()

    got = ocr_page_text(str(scan), 0, lang="eng", dpi=300)
    ref_words = {w.lower() for w in ref.split() if len(w) > 3}
    got_words = {w.lower() for w in got.split() if len(w) > 3}
    assert_non_empty(ref_words, "pymupdf ocr words")
    assert_non_empty(got_words, "shim ocr words")
    overlap = len(ref_words & got_words) / len(ref_words)
    assert overlap >= 0.75, f"OCR word overlap {overlap:.2f}"


def test_text_layer_is_returned_without_ocr_when_present():
    """Parity with PyMuPDF's get_textpage_ocr(full=False), which returns
    the existing text layer rather than OCRing a born-digital page. It is
    also strictly better: OCRing clean text loses accuracy and costs
    seconds."""
    got = ocr_page_text(_FIXTURE, _PAGE, lang="eng", dpi=300)
    ref_doc = pymupdf.open(_FIXTURE)
    ref = ref_doc[_PAGE].get_text()
    ref_doc.close()
    assert_non_empty(got, "text")
    ref_words = {w for w in ref.split() if len(w) > 4}
    got_words = {w for w in got.split() if len(w) > 4}
    assert len(ref_words & got_words) / len(ref_words) > 0.9
