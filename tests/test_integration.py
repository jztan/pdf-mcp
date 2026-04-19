"""
End-to-end integration tests for OCR and page rendering.

TestScanDetectionNoOcr: runs everywhere, no system deps.
TestOcrIntegration: skipped if Tesseract is not installed.
"""

import io
import os
import tempfile
from pathlib import Path

import pymupdf
import pytest
from PIL import Image, ImageDraw
from fastmcp.utilities.types import Image as McpImage

from pdf_mcp.extractor import check_tesseract_available
from pdf_mcp.server import pdf_info, pdf_read_pages, pdf_render_pages, pdf_search

KNOWN_TEXT = "Integration test OCR phrase"


def _tesseract_available() -> bool:
    try:
        check_tesseract_available()
        return True
    except RuntimeError:
        return False


@pytest.fixture
def sample_pdf_synthetic_scan(isolated_server):
    img = Image.new("RGB", (600, 100), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.text((10, 30), KNOWN_TEXT, fill=(0, 0, 0))
    img_bytes = io.BytesIO()
    img.save(img_bytes, format="PNG")
    img_bytes.seek(0)

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        doc = pymupdf.open()
        page = doc.new_page(width=600, height=100)
        page.insert_image(pymupdf.Rect(0, 0, 600, 100), stream=img_bytes.read())
        doc.save(f.name)
        doc.close()
        path = str(Path(f.name).resolve())
        yield path
        os.unlink(path)


class TestScanDetectionNoOcr:
    def test_pdf_info_detects_scanned_page(self, sample_pdf_synthetic_scan):
        result = pdf_info(sample_pdf_synthetic_scan)
        coverage = result["text_coverage"]
        assert isinstance(coverage, list)
        assert len(coverage) == 1
        assert coverage[0]["text_chars"] == 0
        assert coverage[0]["raster_images"] >= 1

    def test_render_returns_valid_png(self, sample_pdf_synthetic_scan):
        result = pdf_render_pages(sample_pdf_synthetic_scan, "1", dpi=150)
        assert len(result) >= 2
        assert "pages_rendered" in result[0]
        assert isinstance(result[1], McpImage)
        pil_img = Image.open(io.BytesIO(result[1].data))
        assert pil_img.width > 0
        assert pil_img.height > 0


class TestOcrIntegration:
    pytestmark = pytest.mark.skipif(
        not _tesseract_available(),
        reason="Tesseract not installed",
    )

    def test_ocr_extracts_known_text(self, sample_pdf_synthetic_scan):
        result = pdf_read_pages(sample_pdf_synthetic_scan, "1", ocr=True)
        page = result["pages"][0]
        assert page["source"] == "ocr"
        words = KNOWN_TEXT.lower().split()
        text_lower = page["text"].lower()
        assert any(w in text_lower for w in words), (
            f"None of {words} found in OCR output: {page['text']!r}"
        )

    def test_ocr_text_is_searchable(self, sample_pdf_synthetic_scan):
        pdf_read_pages(sample_pdf_synthetic_scan, "1", ocr=True)
        result = pdf_search(sample_pdf_synthetic_scan, "integration")
        assert len(result["matches"]) > 0
        assert result["matches"][0]["page"] == 1
        assert result["matches"][0]["source"] == "ocr"
