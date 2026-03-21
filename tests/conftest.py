# tests/conftest.py
"""Shared test fixtures for pdf-mcp tests."""

import base64
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pymupdf
import pytest

from pdf_mcp.cache import PDFCache
from pdf_mcp.url_fetcher import URLFetcher
import pdf_mcp.server as server_module


@pytest.fixture
def temp_cache_dir():
    """Create a temporary cache directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def cache(temp_cache_dir):
    """Create a cache instance with temporary directory."""
    return PDFCache(cache_dir=temp_cache_dir, ttl_hours=1)


@pytest.fixture
def sample_pdf():
    """Create a sample 5-page PDF for testing."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        doc = pymupdf.open()

        for i in range(5):
            page = doc.new_page()
            text = f"This is page {i + 1} content.\n\nSome sample text for testing."
            page.insert_text((50, 50), text)

        doc.save(f.name)
        doc.close()

        yield f.name

        os.unlink(f.name)


@pytest.fixture
def isolated_server(temp_cache_dir, monkeypatch):
    """
    Isolate server module globals for testing.
    Returns tuple of (cache, url_fetcher) instances used.
    """
    test_cache = PDFCache(cache_dir=temp_cache_dir, ttl_hours=1)
    test_url_fetcher = URLFetcher(cache_dir=temp_cache_dir / "downloads")

    monkeypatch.setattr(server_module, "cache", test_cache)
    monkeypatch.setattr(server_module, "url_fetcher", test_url_fetcher)

    return test_cache, test_url_fetcher


@pytest.fixture
def sample_pdf_with_toc():
    """Create a PDF with table of contents."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        doc = pymupdf.open()

        for i in range(3):
            page = doc.new_page()
            page.insert_text((50, 50), f"Chapter {i + 1} content")

        # Add TOC
        toc = [
            [1, "Chapter 1", 1],
            [1, "Chapter 2", 2],
            [1, "Chapter 3", 3],
        ]
        doc.set_toc(toc)

        doc.save(f.name)
        doc.close()

        yield f.name
        os.unlink(f.name)


@pytest.fixture
def sample_pdf_with_images():
    """Create a PDF with embedded images."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        doc = pymupdf.open()
        page = doc.new_page()

        # Create a simple colored rectangle as an "image"
        rect = pymupdf.Rect(100, 100, 200, 200)
        page.draw_rect(rect, color=(1, 0, 0), fill=(0, 0, 1))

        # Insert actual image (create minimal PNG)
        # 1x1 red PNG
        png_data = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
            "AAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
        )
        page.insert_image(pymupdf.Rect(50, 50, 80, 80), stream=png_data)

        doc.save(f.name)
        doc.close()

        yield f.name
        os.unlink(f.name)


@pytest.fixture
def mock_url_to_pdf(sample_pdf):
    """Mock URL fetcher to return sample_pdf for any URL."""
    with patch.object(URLFetcher, "is_url", return_value=True):
        with patch.object(URLFetcher, "fetch", return_value=sample_pdf):
            yield sample_pdf


@pytest.fixture
def sample_pdf_grayscale():
    """Create a PDF with a grayscale image."""
    from PIL import Image
    import io

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        doc = pymupdf.open()
        page = doc.new_page()

        # Create grayscale image with PIL
        img = Image.new("L", (50, 50), color=128)  # "L" = grayscale
        img_bytes = io.BytesIO()
        img.save(img_bytes, format="PNG")
        img_bytes.seek(0)

        page.insert_image(pymupdf.Rect(50, 50, 100, 100), stream=img_bytes.read())

        doc.save(f.name)
        doc.close()

        yield f.name
        os.unlink(f.name)


@pytest.fixture
def sample_pdf_rgba():
    """Create a PDF with an RGBA image (transparency)."""
    from PIL import Image
    import io

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        doc = pymupdf.open()
        page = doc.new_page()

        # Create RGBA image with PIL
        img = Image.new(
            "RGBA", (50, 50), color=(255, 0, 0, 128)
        )  # Semi-transparent red
        img_bytes = io.BytesIO()
        img.save(img_bytes, format="PNG")
        img_bytes.seek(0)

        page.insert_image(pymupdf.Rect(50, 50, 100, 100), stream=img_bytes.read())

        doc.save(f.name)
        doc.close()

        yield f.name
        os.unlink(f.name)


@pytest.fixture
def sample_pdf_with_table():
    """Create a PDF with a detectable table (explicit borders required for find_tables)."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        doc = pymupdf.open()
        page = doc.new_page()

        # Outer border (2 cols × 3 rows)
        page.draw_rect(pymupdf.Rect(50, 50, 250, 150), color=(0, 0, 0))
        # Column divider
        page.draw_line(pymupdf.Point(150, 50), pymupdf.Point(150, 150), color=(0, 0, 0))
        # Row dividers
        page.draw_line(pymupdf.Point(50, 83), pymupdf.Point(250, 83), color=(0, 0, 0))
        page.draw_line(pymupdf.Point(50, 116), pymupdf.Point(250, 116), color=(0, 0, 0))

        # Cell text
        page.insert_text((55, 75), "Name")
        page.insert_text((155, 75), "Value")
        page.insert_text((55, 108), "Alpha")
        page.insert_text((155, 108), "1")
        page.insert_text((55, 141), "Beta")
        page.insert_text((155, 141), "2")

        doc.save(f.name)
        doc.close()
        yield f.name
        os.unlink(f.name)
