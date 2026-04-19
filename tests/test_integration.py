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

from pdf_mcp.extractor import check_tesseract_available
from pdf_mcp.server import pdf_info, pdf_read_pages, pdf_render_pages, pdf_search

KNOWN_TEXT = "Integration test OCR phrase"


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
