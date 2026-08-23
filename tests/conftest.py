# tests/conftest.py
"""Shared test fixtures for pdf-mcp tests."""

import base64
import io
import tempfile
from pathlib import Path
from unittest.mock import patch

import pymupdf
import pytest
from PIL import Image, ImageDraw

from pdf_mcp.cache import PDFCache
from pdf_mcp.url_fetcher import URLFetcher
import pdf_mcp.server as server_module
from tests.tmpfiles import unlink_quietly


@pytest.fixture(autouse=True, scope="session")
def _no_fsync_in_tests():
    """Turn off SQLite fsync for the whole test session.

    Every test builds a fresh cache in a temp dir that dies with the
    test, so durability across a power loss protects nothing here -- but
    each commit still pays a real fsync, and the suite commits thousands
    of times. That cost is invisible on Linux (~1ms) and dominant on
    Windows (~28ms, the same measurement that explained the 5.4x cold
    search gap). Patching sqlite3.connect keeps the change test-only:
    the product's durability is untouched, and spawn workers re-import
    a clean sqlite3 so they are unaffected.
    """
    import sqlite3 as _sqlite3

    real_connect = _sqlite3.connect

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        try:
            conn.execute("PRAGMA synchronous=OFF")
        except Exception:
            pass
        return conn

    _sqlite3.connect = connect
    yield
    _sqlite3.connect = real_connect


@pytest.fixture
def temp_cache_dir():
    """Create a temporary cache directory."""
    # ignore_cleanup_errors: the cache holds cache.db open through its
    # SQLite connection, and Windows refuses to delete an open file, so
    # cleanup raised WinError 32 and then WinError 267 while retrying and
    # failed the test at teardown. The directory is a temp dir the OS
    # reclaims regardless, and cleanup must not fail a passing test.
    # POSIX is unaffected: unlink there succeeds with the file open.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def cache(temp_cache_dir):
    """Create a cache instance with temporary directory."""
    return PDFCache(cache_dir=temp_cache_dir, ttl_hours=1)


@pytest.fixture
def sample_pdf():
    """Create a sample 5-page PDF for testing."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        # Close the handle before anything writes to this path: Windows
        # refuses to write or replace a file that is still open, which
        # turned into 639 errors the first time CI ran there. delete=False
        # means closing early does not remove the file.
        f.close()
        doc = pymupdf.open()

        for i in range(5):
            page = doc.new_page()
            text = f"This is page {i + 1} content.\n\nSome sample text for testing."
            page.insert_text((50, 50), text)

        doc.save(f.name)
        doc.close()

        # Resolve symlinks so paths match what _resolve_path() returns
        resolved = str(Path(f.name).resolve())
        yield resolved

        unlink_quietly(resolved)


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
        f.close()  # see above: Windows open-handle rule
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
        unlink_quietly(f.name)


@pytest.fixture
def sample_pdf_with_toc_sections(tmp_path):
    """A 5-page PDF with set_toc and distinctive body text per section."""
    path = tmp_path / "with_toc_sections.pdf"
    doc = pymupdf.open()
    contents = [
        ("Introduction", "introduction body about graph neural networks"),
        ("Methods", "methods describing graph attention mechanism in detail"),
        ("Results", "results we observed strong performance gains"),
        ("Discussion", "discussion of implications and tradeoffs"),
        ("Conclusion", "conclusion and future work directions"),
    ]
    for title, body in contents:
        page = doc.new_page(width=600, height=800)
        page.insert_text((50, 100), title, fontsize=14)
        page.insert_text((50, 130), body, fontsize=11)
    doc.set_toc(
        [
            [1, "Introduction", 1],
            [1, "Methods", 2],
            [1, "Results", 3],
            [1, "Discussion", 4],
            [1, "Conclusion", 5],
        ]
    )
    doc.save(str(path))
    doc.close()
    return str(path)


@pytest.fixture
def sample_pdf_with_large_toc():
    """Create a PDF with more than 50 TOC entries (triggers truncation)."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.close()  # see above: Windows open-handle rule
        doc = pymupdf.open()

        for i in range(60):
            page = doc.new_page()
            page.insert_text((50, 50), f"Slide {i + 1} content")

        toc = [[1, f"Slide {i + 1}: Topic", i + 1] for i in range(60)]
        doc.set_toc(toc)

        doc.save(f.name)
        doc.close()

        yield f.name
        unlink_quietly(f.name)


@pytest.fixture
def sample_pdf_with_images():
    """Create a PDF with embedded images."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.close()  # see above: Windows open-handle rule
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
        unlink_quietly(f.name)


@pytest.fixture
def sample_pdf_dup_image():
    """PDF placing the SAME embedded image twice on one page.

    One xref, two placements.
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.close()  # see above: Windows open-handle rule
        doc = pymupdf.open()
        page = doc.new_page()
        png_data = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
            "AAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
        )
        page.insert_image(pymupdf.Rect(50, 50, 80, 80), stream=png_data)
        page.insert_image(pymupdf.Rect(120, 120, 150, 150), stream=png_data)
        doc.save(f.name)
        doc.close()
        yield f.name
        unlink_quietly(f.name)


@pytest.fixture
def sample_pdf_two_distinct_images():
    """PDF with two DIFFERENT embedded images on one page (two distinct xrefs)."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.close()  # see above: Windows open-handle rule
        doc = pymupdf.open()
        page = doc.new_page()
        red = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
            "AAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
        )
        blue = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAACXBIWXMAAA7E"
            "AAAOxAGVKw4bAAAADElEQVR4nGNgYPgPAAEDAQAIicLsAAAAAElFTkSuQmCC"
        )
        page.insert_image(pymupdf.Rect(50, 50, 80, 80), stream=red)
        page.insert_image(pymupdf.Rect(120, 120, 150, 150), stream=blue)
        doc.save(f.name)
        doc.close()
        yield f.name
        unlink_quietly(f.name)


@pytest.fixture
def sample_pdf_scanned():
    """PDF with zero extractable text but raster images (scan simulation)."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.close()  # see above: Windows open-handle rule
        doc = pymupdf.open()
        page = doc.new_page()
        png_data = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
            "AAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
        )
        page.insert_image(pymupdf.Rect(50, 50, 400, 600), stream=png_data)
        doc.save(f.name)
        doc.close()
        yield str(Path(f.name).resolve())
        unlink_quietly(f.name)


@pytest.fixture
def sample_pdf_mixed():
    """PDF with pages 1-2 having text and pages 3-4 being image-only."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.close()  # see above: Windows open-handle rule
        doc = pymupdf.open()
        png_data = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
            "AAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
        )
        for i in range(2):
            page = doc.new_page()
            page.insert_text((50, 50), f"Page {i + 1} has text content here.")
        for _ in range(2):
            page = doc.new_page()
            page.insert_image(pymupdf.Rect(50, 50, 400, 600), stream=png_data)
        doc.save(f.name)
        doc.close()
        yield str(Path(f.name).resolve())
        unlink_quietly(f.name)


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
        f.close()  # see above: Windows open-handle rule
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
        unlink_quietly(f.name)


@pytest.fixture
def sample_pdf_rgba():
    """Create a PDF with an RGBA image (transparency)."""
    from PIL import Image
    import io

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.close()  # see above: Windows open-handle rule
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
        unlink_quietly(f.name)


@pytest.fixture
def pdf_with_hidden_text():
    """Two-page PDF. Page 1: visible body PLUS a white-on-white hidden span
    carrying a unique token ('zebra'). Page 2: clean body with its own token
    ('omega'). Unique per-page tokens make keyword targeting deterministic."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.close()  # see above: Windows open-handle rule
        doc = pymupdf.open()
        p1 = doc.new_page()
        p1.insert_text((50, 50), "alpha visible body text on page one", fontsize=12)
        p1.insert_text(
            (50, 90),
            "zebra hidden injected secret instructions text here",
            fontsize=12,
            color=(1, 1, 1),
        )
        p2 = doc.new_page()
        p2.insert_text(
            (50, 50), "omega clean visible body text on page two", fontsize=12
        )
        doc.save(f.name)
        doc.close()
        yield f.name
    unlink_quietly(f.name)


@pytest.fixture
def sample_pdf_with_table():
    """Create a PDF with a detectable table.

    Explicit borders are required for find_tables() to detect it.
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.close()  # see above: Windows open-handle rule
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
        unlink_quietly(f.name)


@pytest.fixture
def corpus_dir(tmp_path):
    """Directory of 3 small PDFs with differing page counts.

    Pages carry two text blocks with repeated terms ("budget") so
    corpus fixtures follow the realistic multi-block pattern rather
    than unique-phrase-per-page.
    """
    d = tmp_path / "corpus"
    d.mkdir()
    for name, pages in [("alpha.pdf", 2), ("bravo.pdf", 4), ("charlie.pdf", 1)]:
        doc = pymupdf.open()
        for i in range(pages):
            page = doc.new_page()
            page.insert_text(
                (50, 50),
                f"Report section {i + 1}. The quarterly budget increased.",
            )
            page.insert_text(
                (50, 300),
                f"Second block on page {i + 1}. Budget details and notes.",
            )
        doc.save(str(d / name))
        doc.close()
    return d


@pytest.fixture
def big_scan_pdf(tmp_path):
    """A 3-page PDF of noisy full-page rasters. PNG blows the 900_000 base64
    budget at 200 DPI; JPEG q80 fits comfortably. Deterministic seed so the
    sizes do not drift between runs.

    Pure per-pixel white noise is nearly incompressible under BOTH PNG and
    JPEG (JPEG only got ~2.4x on it in a spike measurement), so the raster
    is built as a photo-like texture instead: a smooth low-frequency base
    (upsampled from a small random grid, which JPEG's DCT quantizes away to
    almost nothing) plus per-pixel grain (which defeats PNG's row-predictor
    filters, since neighboring pixels are not smoothly related).
    """
    import numpy as np
    import pymupdf
    from PIL import Image

    rng = np.random.default_rng(20260809)
    w, h, block, grain_amp = 1200, 1600, 40, 20
    base_small = rng.integers(0, 256, size=(h // block, w // block, 3), dtype=np.uint8)
    base_img = Image.fromarray(base_small, "RGB").resize((w, h), Image.BICUBIC)
    base = np.asarray(base_img).astype(np.int16)
    grain = rng.integers(-grain_amp, grain_amp + 1, size=(h, w, 3), dtype=np.int16)
    noise = np.clip(base + grain, 0, 255).astype(np.uint8).tobytes()

    pix = pymupdf.Pixmap(pymupdf.csRGB, w, h, noise, 0)

    doc = pymupdf.Document()
    for _ in range(3):
        page = doc.new_page(width=432, height=576)
        page.insert_image(page.rect, pixmap=pix)
    out = tmp_path / "big_scan.pdf"
    doc.save(out)

    # F9 guard: this fixture only exercises the PNG-blows-budget /
    # JPEG-fits cascade as long as it stays on the right side of the
    # 900_000-base64-byte budget at 200 DPI. The PNG at 825_512 base64
    # bytes measured a razor-thin 8.3% margin under budget; a libjpeg or
    # Pillow version bump nudging encoded sizes by that much would
    # silently turn the whole TestRenderEncodeCascade class into a
    # downsample test that still passes green. Fail loudly here instead,
    # as "fixture no longer exercises the cascade", rather than as a
    # confusing assertion failure deep in the render tests.
    from pdf_mcp.server import RENDER_RESULT_BYTE_BUDGET, _encoded_len

    render_page = doc[0]
    render_pix = render_page.get_pixmap(dpi=200)
    png_len = _encoded_len(render_pix.tobytes("png"))
    jpeg_len = _encoded_len(render_pix.tobytes("jpeg", jpg_quality=80))
    doc.close()

    assert png_len > RENDER_RESULT_BYTE_BUDGET, (
        f"big_scan_pdf PNG at 200 DPI is {png_len} base64 bytes, no longer "
        f"over the {RENDER_RESULT_BYTE_BUDGET} budget; regenerate the "
        "fixture so the cascade test class still exercises JPEG fallback"
    )
    assert jpeg_len <= RENDER_RESULT_BYTE_BUDGET, (
        f"big_scan_pdf JPEG q80 at 200 DPI is {jpeg_len} base64 bytes, over "
        f"the {RENDER_RESULT_BYTE_BUDGET} budget; regenerate the fixture so "
        "the cascade test class still exercises the JPEG success path"
    )

    return str(out)


@pytest.fixture
def scanned_page_pdf(tmp_path):
    """A one-page PDF whose entire page is a single embedded raster, with no
    text and no vector drawings. Mimics a phone scan."""
    import pymupdf

    # 900x1200 px raster placed on a 450x600 pt page -> native 144 DPI.
    src = pymupdf.Document()
    src_page = src.new_page(width=450, height=600)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 900, 1200))
    pix.set_rect(pix.irect, (240, 240, 240))
    src_page.insert_image(src_page.rect, pixmap=pix)
    out = tmp_path / "scan.pdf"
    src.save(out)
    src.close()
    return str(out)


@pytest.fixture
def mixed_native_cap_pdf(tmp_path):
    """Two full-page-scan pages with different native raster resolutions:
    page 1 at 400 DPI, page 2 at 100 DPI. A request whose DPI is clamped
    down by page 2's low cap must still flag page 1 as downsampled, since
    page 1 had headroom of its own that the sibling page's cap ate into.
    """
    import pymupdf

    doc = pymupdf.Document()

    # 2400x3200 px on a 432x576 pt page -> native 400 DPI.
    page1 = doc.new_page(width=432, height=576)
    pix1 = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 2400, 3200))
    pix1.set_rect(pix1.irect, (200, 200, 200))
    page1.insert_image(page1.rect, pixmap=pix1)

    # 600x800 px on the same 432x576 pt page -> native 100 DPI.
    page2 = doc.new_page(width=432, height=576)
    pix2 = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 600, 800))
    pix2.set_rect(pix2.irect, (200, 200, 200))
    page2.insert_image(page2.rect, pixmap=pix2)

    out = tmp_path / "mixed_native_cap.pdf"
    doc.save(out)
    doc.close()
    return str(out)


@pytest.fixture
def native_cap_257_pdf(tmp_path):
    """A single full-page-scan page at 257 native DPI: the same effective
    resolution as benchmark_data/.render_legibility_pdfs/scan-tutorial-1.pdf,
    reproduced synthetically so the test doesn't depend on a benchmark
    corpus file."""
    import pymupdf

    # 1542x2056 px on a 432x576 pt page -> native 257 DPI.
    doc = pymupdf.Document()
    page = doc.new_page(width=432, height=576)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 1542, 2056))
    pix.set_rect(pix.irect, (200, 200, 200))
    page.insert_image(page.rect, pixmap=pix)
    out = tmp_path / "native_cap_257.pdf"
    doc.save(out)
    doc.close()
    return str(out)


KNOWN_TEXT = "Integration test OCR phrase"


@pytest.fixture
def sample_pdf_synthetic_scan(isolated_server):
    """A one-page image-only PDF carrying KNOWN_TEXT as pixels.

    Shared by the OCR integration tests and the ocr_lang cache-key tests,
    which stub OCR and so need no Tesseract.
    """
    img = Image.new("RGB", (600, 100), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.text((10, 30), KNOWN_TEXT, fill=(0, 0, 0))
    img_bytes = io.BytesIO()
    img.save(img_bytes, format="PNG")
    img_bytes.seek(0)

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.close()  # see above: Windows open-handle rule
        doc = pymupdf.open()
        page = doc.new_page(width=600, height=100)
        page.insert_image(pymupdf.Rect(0, 0, 600, 100), stream=img_bytes.read())
        doc.save(f.name)
        doc.close()
        path = str(Path(f.name).resolve())
        yield path
        unlink_quietly(path)
