"""
Tests for pdf-mcp server.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pymupdf
import pytest

from pdf_mcp.extractor import (
    estimate_tokens,
    extract_images_from_page,
    extract_metadata,
    extract_text_from_page,
    extract_toc,
    parse_page_range,
)


# ============================================================================
# Page Range Parser Tests
# ============================================================================


class TestParsePageRange:
    def test_none_returns_all(self):
        result = parse_page_range(None, 10)
        assert result == list(range(10))

    def test_list_input(self):
        result = parse_page_range([1, 3, 5], 10)
        assert result == [0, 2, 4]  # 0-indexed

    def test_single_page_string(self):
        result = parse_page_range("5", 10)
        assert result == [4]  # 0-indexed

    def test_range_string(self):
        result = parse_page_range("1-5", 10)
        assert result == [0, 1, 2, 3, 4]

    def test_complex_range(self):
        result = parse_page_range("1-3,5,8-10", 10)
        assert result == [0, 1, 2, 4, 7, 8, 9]

    def test_out_of_range_filtered(self):
        result = parse_page_range("1,5,15", 10)
        assert result == [0, 4]  # 15 is filtered out

    def test_duplicates_removed(self):
        result = parse_page_range("1,1,2,2", 10)
        assert result == [0, 1]

    def test_trailing_comma_skips_empty(self):
        result = parse_page_range("1,2,", 10)
        assert result == [0, 1]


# ============================================================================
# Cache Tests
# ============================================================================


class TestPDFCache:
    def test_save_and_get_metadata(self, cache, sample_pdf):
        metadata = {"title": "Test", "author": "Tester"}
        toc = [{"level": 1, "title": "Chapter 1", "page": 1}]

        cache.save_metadata(sample_pdf, 5, metadata, toc)

        result = cache.get_metadata(sample_pdf)

        assert result is not None
        assert result["page_count"] == 5
        assert result["metadata"]["title"] == "Test"
        assert len(result["toc"]) == 1

    def test_get_nonexistent_metadata(self, cache):
        result = cache.get_metadata("/nonexistent/file.pdf")
        assert result is None

    def test_save_and_get_page_text(self, cache, sample_pdf):
        cache.save_page_text(sample_pdf, 0, "Page 1 content")
        cache.save_page_text(sample_pdf, 1, "Page 2 content")

        assert cache.get_page_text(sample_pdf, 0) == "Page 1 content"
        assert cache.get_page_text(sample_pdf, 1) == "Page 2 content"
        assert cache.get_page_text(sample_pdf, 2) is None

    def test_get_pages_text_batch(self, cache, sample_pdf):
        cache.save_page_text(sample_pdf, 0, "Page 1")
        cache.save_page_text(sample_pdf, 1, "Page 2")
        cache.save_page_text(sample_pdf, 2, "Page 3")

        result = cache.get_pages_text(sample_pdf, [0, 1, 2, 3])

        assert 0 in result
        assert 1 in result
        assert 2 in result
        assert 3 not in result  # Not cached

    def test_cache_stats(self, cache, sample_pdf):
        cache.save_metadata(sample_pdf, 5, {}, [])
        cache.save_page_text(sample_pdf, 0, "Test content")

        stats = cache.get_stats()

        assert stats["total_files"] == 1
        assert stats["total_pages"] == 1
        assert stats["cache_size_bytes"] > 0

    def test_clear_all(self, cache, sample_pdf):
        cache.save_metadata(sample_pdf, 5, {}, [])
        cache.save_page_text(sample_pdf, 0, "Test")

        cache.clear_all()

        stats = cache.get_stats()
        assert stats["total_files"] == 0
        assert stats["total_pages"] == 0


# ============================================================================
# Extractor Tests
# ============================================================================


class TestExtractor:
    def test_extract_text_from_page(self, sample_pdf):
        doc = pymupdf.open(sample_pdf)
        page = doc[0]

        text = extract_text_from_page(page)

        assert "page 1" in text.lower()
        doc.close()

    def test_extract_metadata(self, sample_pdf):
        doc = pymupdf.open(sample_pdf)

        metadata = extract_metadata(doc)

        assert isinstance(metadata, dict)
        assert "title" in metadata
        assert "author" in metadata
        doc.close()

    def test_extract_toc(self, sample_pdf):
        doc = pymupdf.open(sample_pdf)

        toc = extract_toc(doc)

        # Sample PDF has no TOC
        assert isinstance(toc, list)
        doc.close()

    def test_estimate_tokens(self):
        text = "Hello world this is a test"
        tokens = estimate_tokens(text)

        # ~4 chars per token
        assert 5 <= tokens <= 10

    def test_extract_images_rgba_format(self, sample_pdf_with_images, tmp_path):
        """RGBA format detected when pix.n == 4."""
        mock_pix = MagicMock()
        mock_pix.n = 4
        mock_pix.alpha = 1
        mock_pix.width = 10
        mock_pix.height = 10
        mock_pix.save = MagicMock(
            side_effect=lambda path: Path(path).write_bytes(b"\x89PNG")
        )

        with patch("pdf_mcp.extractor.pymupdf.Pixmap", return_value=mock_pix):
            doc = pymupdf.open(sample_pdf_with_images)
            images = extract_images_from_page(
                doc, 0, output_dir=tmp_path, pdf_hash="test"
            )
            doc.close()

        assert len(images) >= 1
        assert images[0]["format"] == "rgba"

    def test_extract_images_unknown_format(self, sample_pdf_with_images, tmp_path):
        """Unknown format detected when pix.n is not 1, 3, or 4."""
        mock_pix = MagicMock()
        mock_pix.n = 2
        mock_pix.alpha = 0
        mock_pix.width = 10
        mock_pix.height = 10
        mock_pix.save = MagicMock(
            side_effect=lambda path: Path(path).write_bytes(b"\x89PNG")
        )

        with patch("pdf_mcp.extractor.pymupdf.Pixmap", return_value=mock_pix):
            doc = pymupdf.open(sample_pdf_with_images)
            images = extract_images_from_page(
                doc, 0, output_dir=tmp_path, pdf_hash="test"
            )
            doc.close()

        assert len(images) >= 1
        assert images[0]["format"] == "unknown"

    def test_extract_images_save_fail_cleanup_fail(
        self, sample_pdf_with_images, tmp_path
    ):
        fake_dir = tmp_path / "not_a_dir"
        fake_dir.write_bytes(b"I am a file")

        doc = pymupdf.open(sample_pdf_with_images)
        images = extract_images_from_page(doc, 0, output_dir=fake_dir, pdf_hash="test")
        doc.close()

        assert images == []


# ============================================================================
# Integration Tests
# ============================================================================


class TestIntegration:
    def test_full_workflow(self, cache, sample_pdf):
        """Test a complete read workflow with caching."""
        doc = pymupdf.open(sample_pdf)

        # First call - extract and cache
        page = doc[0]
        text = extract_text_from_page(page)
        cache.save_page_text(sample_pdf, 0, text)

        # Close and reopen (simulating new MCP call)
        doc.close()

        # Second call - should hit cache
        cached_text = cache.get_page_text(sample_pdf, 0)

        assert cached_text == text
        assert "page 1" in cached_text.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
