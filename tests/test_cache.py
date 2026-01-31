# tests/test_cache.py
"""Tests for pdf_mcp.cache module - edge cases."""

import os
import time
import tempfile
from pathlib import Path

import pytest

from pdf_mcp.cache import PDFCache


@pytest.fixture
def cache_with_data(cache, sample_pdf):
    """Cache pre-populated with test data."""
    cache.save_metadata(sample_pdf, 5, {"title": "Test"}, [])
    cache.save_page_text(sample_pdf, 0, "Page 1 content")
    cache.save_page_text(sample_pdf, 1, "Page 2 content")
    return cache, sample_pdf


class TestCacheValidation:
    """Tests for cache validation edge cases."""

    def test_is_cache_valid_file_deleted(self, cache):
        """Deleted file returns False for cache validity."""
        # Create temp file, cache it, then delete
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4")
            temp_path = f.name

        # Get mtime before deletion
        mtime = os.stat(temp_path).st_mtime

        # Delete the file
        os.unlink(temp_path)

        # _is_cache_valid should return False (OSError)
        result = cache._is_cache_valid(temp_path, mtime)
        assert result is False

    def test_get_metadata_invalidates_on_mtime_change(self, cache, sample_pdf):
        """Changed file mtime invalidates cached metadata."""
        # Save metadata
        cache.save_metadata(sample_pdf, 5, {"title": "Test"}, [])

        # Verify it's cached
        assert cache.get_metadata(sample_pdf) is not None

        # Touch the file to change mtime
        time.sleep(0.1)
        Path(sample_pdf).touch()

        # Should return None and invalidate
        result = cache.get_metadata(sample_pdf)
        assert result is None

    def test_get_page_text_invalid_mtime(self, cache, sample_pdf):
        """Page text with wrong mtime returns None."""
        cache.save_page_text(sample_pdf, 0, "Content")

        # Touch file to change mtime
        time.sleep(0.1)
        Path(sample_pdf).touch()

        result = cache.get_page_text(sample_pdf, 0)
        assert result is None

    def test_get_page_images_invalid_mtime(self, cache, sample_pdf):
        """Page images with wrong mtime returns None."""
        cache.save_page_images(sample_pdf, 0, [
            {"index": 0, "width": 100, "height": 100, "format": "rgb", "data": "base64data"}
        ])

        # Touch file
        time.sleep(0.1)
        Path(sample_pdf).touch()

        result = cache.get_page_images(sample_pdf, 0)
        assert result is None


class TestEmptyInputs:
    """Tests for empty input handling."""

    def test_get_pages_text_empty_list(self, cache, sample_pdf):
        """Empty page list returns empty dict."""
        result = cache.get_pages_text(sample_pdf, [])
        assert result == {}

    def test_save_pages_text_empty_dict(self, cache, sample_pdf):
        """Empty pages dict is a no-op."""
        # Should not raise
        cache.save_pages_text(sample_pdf, {})

        # Verify nothing was saved
        stats = cache.get_stats()
        assert stats["total_pages"] == 0


class TestCacheInvalidation:
    """Tests for cache invalidation."""

    def test_invalidate_file_clears_all_tables(self, cache_with_data):
        """_invalidate_file removes data from all tables."""
        cache, sample_pdf = cache_with_data

        # Add images too
        cache.save_page_images(sample_pdf, 0, [
            {"index": 0, "width": 10, "height": 10, "format": "rgb", "data": "abc"}
        ])

        # Verify data exists
        assert cache.get_metadata(sample_pdf) is not None

        # Manually invalidate
        cache._invalidate_file(sample_pdf)

        # All data should be gone
        stats = cache.get_stats()
        assert stats["total_files"] == 0
        assert stats["total_pages"] == 0
        assert stats["total_images"] == 0

    def test_mtime_change_invalidates_on_access(self, cache, sample_pdf):
        """Accessing stale cache triggers invalidation."""
        cache.save_metadata(sample_pdf, 5, {}, [])
        cache.save_page_text(sample_pdf, 0, "Content")

        # Change file
        time.sleep(0.1)
        Path(sample_pdf).touch()

        # Access triggers invalidation
        cache.get_metadata(sample_pdf)

        # Metadata should be cleared (though page_text cleanup is separate)
        stats = cache.get_stats()
        assert stats["total_files"] == 0
