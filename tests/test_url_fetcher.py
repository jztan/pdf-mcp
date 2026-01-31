# tests/test_url_fetcher.py
"""Tests for pdf_mcp.url_fetcher module."""

import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest

from pdf_mcp.url_fetcher import URLFetcher


@pytest.fixture
def url_fetcher(temp_cache_dir):
    """Create URLFetcher with temp directory."""
    return URLFetcher(cache_dir=temp_cache_dir / "downloads")


@pytest.fixture
def valid_pdf_bytes():
    """Valid PDF content (minimal)."""
    return b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"


class TestFetch:
    """Tests for URLFetcher.fetch() method."""

    def test_successful_download(self, url_fetcher, valid_pdf_bytes):
        """Successful download saves file and returns path."""
        url = "https://example.com/test.pdf"

        mock_response = Mock()
        mock_response.content = valid_pdf_bytes
        mock_response.headers = {"content-type": "application/pdf"}
        mock_response.raise_for_status = Mock()

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = Mock(return_value=mock_client.return_value)
            mock_client.return_value.__exit__ = Mock(return_value=False)
            mock_client.return_value.get.return_value = mock_response

            result = url_fetcher.fetch(url)

        assert result.exists()
        assert result.read_bytes() == valid_pdf_bytes

    def test_invalid_content_raises_valueerror(self, url_fetcher):
        """Non-PDF content raises ValueError."""
        url = "https://example.com/notapdf.html"

        mock_response = Mock()
        mock_response.content = b"<html>Not a PDF</html>"
        mock_response.headers = {"content-type": "text/html"}
        mock_response.raise_for_status = Mock()

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = Mock(return_value=mock_client.return_value)
            mock_client.return_value.__exit__ = Mock(return_value=False)
            mock_client.return_value.get.return_value = mock_response

            with pytest.raises(ValueError, match="does not appear to be a PDF"):
                url_fetcher.fetch(url)

    def test_force_refresh_bypasses_cache(self, url_fetcher, valid_pdf_bytes):
        """force_refresh=True re-downloads even if cached."""
        url = "https://example.com/refresh.pdf"

        mock_response = Mock()
        mock_response.content = valid_pdf_bytes
        mock_response.headers = {"content-type": "application/pdf"}
        mock_response.raise_for_status = Mock()

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = Mock(return_value=mock_client.return_value)
            mock_client.return_value.__exit__ = Mock(return_value=False)
            mock_client.return_value.get.return_value = mock_response

            # First fetch
            path1 = url_fetcher.fetch(url)

            # Second fetch with force_refresh - should call httpx again
            path2 = url_fetcher.fetch(url, force_refresh=True)

            # httpx.Client().get should be called twice
            assert mock_client.return_value.get.call_count == 2


class TestGetCacheFilename:
    """Tests for URLFetcher._get_cache_filename() method."""

    def test_pdf_url_extracts_name(self, url_fetcher):
        """PDF URL extracts original filename."""
        url = "https://example.com/path/document.pdf"
        filename = url_fetcher._get_cache_filename(url)

        assert filename.endswith("_document.pdf")
        assert len(filename) > len("document.pdf")  # Has hash prefix

    def test_non_pdf_url_uses_hash(self, url_fetcher):
        """Non-PDF URL uses hash-based filename."""
        url = "https://example.com/api/download?id=123"
        filename = url_fetcher._get_cache_filename(url)

        assert filename.endswith(".pdf")
        assert "_" not in filename

    def test_special_chars_sanitized(self, url_fetcher):
        """Special characters are removed from filename."""
        url = "https://example.com/path/my%20doc!@#$.pdf"
        filename = url_fetcher._get_cache_filename(url)

        # Should only contain alphanumeric, dots, underscores, hyphens
        base = filename.split("_", 1)[-1] if "_" in filename else filename
        assert all(c.isalnum() or c in "._-" for c in base)

    def test_deterministic_hash(self, url_fetcher):
        """Same URL produces same filename."""
        url = "https://example.com/test.pdf"
        filename1 = url_fetcher._get_cache_filename(url)
        filename2 = url_fetcher._get_cache_filename(url)

        assert filename1 == filename2


class TestGetLocalPath:
    """Tests for URLFetcher.get_local_path() method."""

    def test_cache_miss_returns_none(self, url_fetcher):
        """Uncached URL returns None."""
        result = url_fetcher.get_local_path("https://example.com/uncached.pdf")
        assert result is None

    def test_memory_cache_hit(self, url_fetcher, temp_cache_dir):
        """URL in memory cache returns path."""
        url = "https://example.com/cached.pdf"
        cached_path = temp_cache_dir / "downloads" / "test.pdf"
        cached_path.parent.mkdir(parents=True, exist_ok=True)
        cached_path.write_bytes(b"%PDF-1.4")

        url_fetcher._url_to_path[url] = cached_path

        result = url_fetcher.get_local_path(url)
        assert result == cached_path

    def test_stale_memory_cache_returns_none(self, url_fetcher, temp_cache_dir):
        """Memory cache with deleted file returns None."""
        url = "https://example.com/deleted.pdf"
        deleted_path = temp_cache_dir / "downloads" / "deleted.pdf"

        url_fetcher._url_to_path[url] = deleted_path

        result = url_fetcher.get_local_path(url)
        assert result is None

    def test_disk_cache_discovery(self, url_fetcher):
        """File on disk but not in memory is discovered."""
        url = "https://example.com/ondisk.pdf"
        filename = url_fetcher._get_cache_filename(url)
        disk_path = url_fetcher.cache_dir / filename
        disk_path.write_bytes(b"%PDF-1.4")

        # Not in memory cache
        assert url not in url_fetcher._url_to_path

        result = url_fetcher.get_local_path(url)

        assert result == disk_path
        assert url in url_fetcher._url_to_path  # Now in memory


class TestClearCache:
    """Tests for URLFetcher.clear_cache() method."""

    def test_oserror_handling(self, url_fetcher):
        """OSError during deletion is handled gracefully."""
        # Create a file
        test_file = url_fetcher.cache_dir / "test.pdf"
        test_file.write_bytes(b"%PDF")

        with patch.object(Path, "unlink", side_effect=OSError("Permission denied")):
            # Should not raise
            count = url_fetcher.clear_cache()

        # Count may be 0 since unlink failed
        assert isinstance(count, int)
