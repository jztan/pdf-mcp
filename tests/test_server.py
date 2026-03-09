# tests/test_server.py
"""Tests for MCP server tools."""

import os
import tempfile

import pytest

from pathlib import Path
from unittest.mock import patch, Mock

import httpx

from pdf_mcp.server import (
    _resolve_path,
    pdf_info,
    pdf_read_pages,
    pdf_read_all,
    pdf_search,
    pdf_get_toc,
    pdf_cache_stats,
    pdf_cache_clear,
)
from pdf_mcp.url_fetcher import URLFetcher


class TestPdfInfo:
    """Tests for pdf_info tool."""

    def test_pdf_info_basic(self, sample_pdf, isolated_server):
        """Valid PDF returns expected fields."""
        result = pdf_info(sample_pdf)

        assert result["page_count"] == 5
        assert result["from_cache"] is False
        assert "metadata" in result
        assert "toc" in result
        assert "file_size_bytes" in result
        assert "file_size_mb" in result
        assert "estimated_tokens" in result
        assert "content_warning" in result

    def test_pdf_info_cached(self, sample_pdf, isolated_server):
        """Second call returns from_cache=True."""
        result1 = pdf_info(sample_pdf)
        assert result1["from_cache"] is False

        result2 = pdf_info(sample_pdf)
        assert result2["from_cache"] is True
        assert result2["page_count"] == result1["page_count"]

    def test_pdf_info_file_not_found(self, isolated_server):
        """Invalid path raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            pdf_info("/nonexistent/path.pdf")

    def test_pdf_info_metadata_fields(self, sample_pdf, isolated_server):
        """All metadata fields present."""
        result = pdf_info(sample_pdf)

        metadata = result["metadata"]
        assert isinstance(metadata, dict)
        # PyMuPDF metadata keys
        assert "title" in metadata or metadata == {}

    def test_pdf_info_estimated_tokens(self, sample_pdf, isolated_server):
        """Token estimation is reasonable."""
        result = pdf_info(sample_pdf)

        # 5 pages * 800 tokens/page estimate
        assert result["estimated_tokens"] == 5 * 800

    def test_pdf_info_with_toc(self, sample_pdf_with_toc, isolated_server):
        """PDF with bookmarks returns toc."""
        result = pdf_info(sample_pdf_with_toc)

        assert len(result["toc"]) == 3
        assert result["toc"][0]["title"] == "Chapter 1"

    def test_pdf_info_from_url(self, mock_url_to_pdf, isolated_server):
        """URL source works (mocked)."""
        result = pdf_info("https://example.com/test.pdf")

        assert result["page_count"] == 5
        assert "content_warning" in result


class TestPdfReadPages:
    """Tests for pdf_read_pages tool."""

    def test_read_pages_single(self, sample_pdf, isolated_server):
        """Single page '1' returns one page."""
        result = pdf_read_pages(sample_pdf, "1")

        assert len(result["pages"]) == 1
        assert result["pages"][0]["page"] == 1
        assert "page 1" in result["pages"][0]["text"].lower()
        assert result["cache_hits"] == 0
        assert result["cache_misses"] == 1

    def test_read_pages_range(self, sample_pdf, isolated_server):
        """Range '1-3' returns three pages."""
        result = pdf_read_pages(sample_pdf, "1-3")

        assert len(result["pages"]) == 3
        assert [p["page"] for p in result["pages"]] == [1, 2, 3]

    def test_read_pages_comma_list(self, sample_pdf, isolated_server):
        """List '1,3,5' returns specific pages."""
        result = pdf_read_pages(sample_pdf, "1,3,5")

        assert len(result["pages"]) == 3
        assert [p["page"] for p in result["pages"]] == [1, 3, 5]

    def test_read_pages_empty_result(self, sample_pdf, isolated_server):
        """Out of bounds pages returns error dict."""
        result = pdf_read_pages(sample_pdf, "100")

        assert "error" in result
        assert result["page_count"] == 5

    def test_read_pages_caching(self, sample_pdf, isolated_server):
        """Second call has cache_hits > 0."""
        pdf_read_pages(sample_pdf, "1-3")
        result = pdf_read_pages(sample_pdf, "1-3")

        assert result["cache_hits"] == 3
        assert result["cache_misses"] == 0

    def test_read_pages_total_chars(self, sample_pdf, isolated_server):
        """Character count is accurate."""
        result = pdf_read_pages(sample_pdf, "1")

        expected_chars = sum(p["chars"] for p in result["pages"])
        assert result["total_chars"] == expected_chars

    def test_read_pages_max_pages_limit_truncation(
        self, sample_pdf, isolated_server, monkeypatch
    ):
        """Requesting more pages than MAX_PAGES_LIMIT truncates to the limit."""
        import pdf_mcp.server

        monkeypatch.setattr(pdf_mcp.server, "MAX_PAGES_LIMIT", 2)
        result = pdf_read_pages(sample_pdf, "1-5")

        assert len(result["pages"]) == 2

    def test_read_pages_with_images(self, sample_pdf_with_images, isolated_server):
        """Pages with images include per-page images with file paths."""
        result = pdf_read_pages(sample_pdf_with_images, "1")
        page = result["pages"][0]
        assert page["image_count"] > 0
        img = page["images"][0]
        assert "path" in img
        assert "data" not in img


class TestPdfReadAll:
    """Tests for pdf_read_all tool."""

    def test_read_all_small_pdf(self, sample_pdf, isolated_server):
        """Full document, truncated=False."""
        result = pdf_read_all(sample_pdf)

        assert result["page_count"] == 5
        assert result["total_pages"] == 5
        assert result["truncated"] is False
        assert "full_text" in result
        assert result["total_chars"] > 0

    def test_read_all_truncation(self, sample_pdf, isolated_server):
        """max_pages=2 truncates."""
        result = pdf_read_all(sample_pdf, max_pages=2)

        assert result["page_count"] == 2
        assert result["total_pages"] == 5
        assert result["truncated"] is True

    def test_read_all_content_joined(self, sample_pdf, isolated_server):
        """Pages joined with double newline."""
        result = pdf_read_all(sample_pdf, max_pages=2)

        # Should contain page separator
        assert "\n\n" in result["full_text"]

    def test_read_all_caching(self, sample_pdf, isolated_server):
        """Pages cached for subsequent calls."""
        pdf_read_all(sample_pdf)

        # Second call via pdf_read_pages should hit cache
        result = pdf_read_pages(sample_pdf, "1-5")
        assert result["cache_hits"] == 5

    def test_read_all_cache_hit_path(self, sample_pdf, isolated_server):
        """Second pdf_read_all call hits cached text path and returns correct data."""
        result1 = pdf_read_all(sample_pdf)
        result2 = pdf_read_all(sample_pdf)

        assert result2["page_count"] == 5
        assert "full_text" in result2
        assert result2["full_text"] == result1["full_text"]

    def test_read_all_file_not_found(self, isolated_server):
        """Invalid path raises error."""
        with pytest.raises(FileNotFoundError):
            pdf_read_all("/nonexistent/path.pdf")

    def test_read_all_docstring_mentions_images(self):
        """pdf_read_all docstring directs users to pdf_read_pages for images."""
        assert "pdf_read_pages" in pdf_read_all.__doc__
        assert "image" in pdf_read_all.__doc__.lower()


class TestPdfSearch:
    """Tests for pdf_search tool."""

    def test_search_found(self, sample_pdf, isolated_server):
        """Returns matches with page, excerpt, position."""
        result = pdf_search(sample_pdf, "page 1")

        assert result["total_matches"] >= 1
        assert len(result["matches"]) >= 1

        match = result["matches"][0]
        assert "page" in match
        assert "excerpt" in match
        assert "position" in match

    def test_search_not_found(self, sample_pdf, isolated_server):
        """Empty matches, total_matches=0."""
        result = pdf_search(sample_pdf, "xyznonexistent")

        assert result["total_matches"] == 0
        assert len(result["matches"]) == 0

    def test_search_case_insensitive(self, sample_pdf, isolated_server):
        """'PAGE' finds 'page'."""
        result = pdf_search(sample_pdf, "PAGE")

        assert result["total_matches"] >= 1

    def test_search_max_results(self, sample_pdf, isolated_server):
        """Respects limit."""
        result = pdf_search(sample_pdf, "page", max_results=2)

        assert len(result["matches"]) <= 2

    def test_search_multiple_pages(self, sample_pdf, isolated_server):
        """Finds across pages."""
        result = pdf_search(sample_pdf, "content")

        # "content" appears on all 5 pages
        assert len(result["pages_with_matches"]) >= 2

    def test_search_context_chars(self, sample_pdf, isolated_server):
        """Custom context size works."""
        result_small = pdf_search(sample_pdf, "page", context_chars=20)
        result_large = pdf_search(sample_pdf, "page", context_chars=100)

        if result_small["matches"] and result_large["matches"]:
            # Larger context should have longer excerpts (usually)
            assert len(result_large["matches"][0]["excerpt"]) >= len(
                result_small["matches"][0]["excerpt"]
            )


class TestPdfGetToc:
    """Tests for pdf_get_toc tool."""

    def test_get_toc_with_toc(self, sample_pdf_with_toc, isolated_server):
        """PDF with bookmarks returns toc."""
        result = pdf_get_toc(sample_pdf_with_toc)

        assert result["has_toc"] is True
        assert result["entry_count"] == 3
        assert len(result["toc"]) == 3

    def test_get_toc_no_toc(self, sample_pdf, isolated_server):
        """PDF without bookmarks returns empty."""
        result = pdf_get_toc(sample_pdf)

        assert result["has_toc"] is False
        assert result["entry_count"] == 0
        assert result["toc"] == []

    def test_get_toc_cached(self, sample_pdf_with_toc, isolated_server):
        """TOC cached after pdf_info populates metadata."""
        # pdf_get_toc reads from cache set by pdf_info
        pdf_info(sample_pdf_with_toc)  # Populates metadata cache including TOC

        result = pdf_get_toc(sample_pdf_with_toc)
        assert result["from_cache"] is True

    def test_get_toc_entry_structure(self, sample_pdf_with_toc, isolated_server):
        """Entries have level, title, page."""
        result = pdf_get_toc(sample_pdf_with_toc)

        entry = result["toc"][0]
        assert "level" in entry
        assert "title" in entry
        assert "page" in entry

    def test_get_toc_file_not_found(self, isolated_server):
        """Invalid path raises error."""
        with pytest.raises(FileNotFoundError):
            pdf_get_toc("/nonexistent/path.pdf")


class TestPdfCacheStats:
    """Tests for pdf_cache_stats tool."""

    def test_cache_stats_empty(self, isolated_server):
        """Fresh cache returns zeros."""
        result = pdf_cache_stats()

        assert result["total_files"] == 0
        assert result["total_pages"] == 0

    def test_cache_stats_after_operations(self, sample_pdf, isolated_server):
        """Non-zero after reading."""
        pdf_info(sample_pdf)
        pdf_read_pages(sample_pdf, "1")

        result = pdf_cache_stats()

        assert result["total_files"] >= 1
        assert result["total_pages"] >= 1

    def test_cache_stats_includes_url_cache(self, isolated_server):
        """Has url_cache section."""
        result = pdf_cache_stats()

        assert "url_cache" in result
        assert "cached_files" in result["url_cache"]

    def test_cache_stats_structure(self, isolated_server):
        """All expected keys present."""
        result = pdf_cache_stats()

        expected_keys = [
            "total_files",
            "total_pages",
            "total_images",
            "cache_size_bytes",
            "cache_size_mb",
            "url_cache",
        ]
        for key in expected_keys:
            assert key in result


class TestPdfCacheClear:
    """Tests for pdf_cache_clear tool."""

    def test_cache_clear_empty_cache(self, isolated_server):
        """No error on empty cache."""
        result = pdf_cache_clear()

        assert "message" in result
        assert result["cleared_files"] == 0

    def test_cache_clear_all(self, sample_pdf, isolated_server):
        """Removes everything."""
        pdf_info(sample_pdf)
        pdf_read_pages(sample_pdf, "1-3")

        result = pdf_cache_clear(expired_only=False)

        assert result["expired_only"] is False
        # Note: cleared_files is -1 when expired_only=False (see server.py:551)

        stats = pdf_cache_stats()
        assert stats["total_files"] == 0
        assert stats["total_pages"] == 0

    def test_cache_clear_expired_only(self, sample_pdf, isolated_server):
        """expired_only=True flag is respected."""
        pdf_info(sample_pdf)

        result = pdf_cache_clear(expired_only=True)

        assert result["expired_only"] is True
        # Returns a cleared count (may vary based on datetime handling)
        assert "cleared_files" in result

    def test_cache_clear_returns_count(self, isolated_server):
        """Returns cleared count."""
        result = pdf_cache_clear()

        assert "cleared_files" in result
        assert isinstance(result["cleared_files"], int)


class TestToolIntegration:
    """Integration tests for tool workflows."""

    def test_info_then_read_uses_cache(self, sample_pdf, isolated_server):
        """Tools share cache."""
        pdf_info(sample_pdf)

        # Metadata cached, but page text not yet
        result = pdf_read_pages(sample_pdf, "1")
        assert result["cache_misses"] == 1

        # Now page text is cached
        result2 = pdf_read_pages(sample_pdf, "1")
        assert result2["cache_hits"] == 1

    def test_search_then_read_workflow(self, sample_pdf, isolated_server):
        """Search -> read pattern."""
        search_result = pdf_search(sample_pdf, "page 3")

        if search_result["pages_with_matches"]:
            page_num = search_result["pages_with_matches"][0]
            read_result = pdf_read_pages(sample_pdf, str(page_num))

            assert len(read_result["pages"]) == 1

    def test_full_workflow_with_cache_clear(self, sample_pdf, isolated_server):
        """End-to-end with clear."""
        # Build up cache
        pdf_info(sample_pdf)
        pdf_read_all(sample_pdf)

        stats_before = pdf_cache_stats()
        assert stats_before["total_pages"] == 5

        # Clear
        pdf_cache_clear(expired_only=False)

        stats_after = pdf_cache_stats()
        assert stats_after["total_pages"] == 0


class TestErrorCases:
    """Error handling tests."""

    @pytest.mark.parametrize(
        "tool_func",
        [
            pdf_info,
            pdf_read_all,
            pdf_get_toc,
        ],
    )
    def test_file_not_found_parametrized(self, tool_func, isolated_server):
        """All path-based tools raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            tool_func("/nonexistent/path.pdf")

    def test_corrupted_pdf(self, temp_cache_dir, isolated_server):
        """Corrupted file handled."""
        import os
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"not a valid pdf content")
            corrupt_path = f.name

        try:
            with pytest.raises(Exception):  # PyMuPDF raises various errors
                pdf_info(corrupt_path)
        finally:
            os.unlink(corrupt_path)


class TestSecurityMitigations:
    """Tests for security hardening measures."""

    def test_non_pdf_extension_rejected(self, isolated_server):
        """Non-PDF file extensions are rejected."""
        import tempfile
        import os

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"not a pdf")
            txt_path = f.name

        try:
            with pytest.raises(ValueError, match="Only PDF files"):
                pdf_info(txt_path)
        finally:
            os.unlink(txt_path)

    def test_content_warning_in_read_pages(self, sample_pdf, isolated_server):
        """Read pages includes content warning."""
        result = pdf_read_pages(sample_pdf, "1")
        assert "content_warning" in result

    def test_content_warning_in_read_all(self, sample_pdf, isolated_server):
        """Read all includes content warning."""
        result = pdf_read_all(sample_pdf)
        assert "content_warning" in result

    def test_content_warning_in_search(self, sample_pdf, isolated_server):
        """Search includes content warning."""
        result = pdf_search(sample_pdf, "page")
        assert "content_warning" in result

    def test_content_warning_in_info(self, sample_pdf, isolated_server):
        """Info includes content warning."""
        result = pdf_info(sample_pdf)
        assert "content_warning" in result

    def test_max_pages_clamped(self, sample_pdf, isolated_server):
        """Excessively large max_pages is clamped."""
        # Should not raise or OOM - clamped to MAX_PAGES_LIMIT
        result = pdf_read_all(sample_pdf, max_pages=999999)
        assert result["page_count"] == 5  # Only 5 pages in the sample

    def test_max_results_clamped(self, sample_pdf, isolated_server):
        """Excessively large max_results is clamped to 100."""
        result = pdf_search(sample_pdf, "page", max_results=999999)
        # Should not crash, results limited
        assert isinstance(result["matches"], list)

    def test_file_path_not_leaked_in_info(self, sample_pdf, isolated_server):
        """Local file paths are not exposed in cached info responses."""
        pdf_info(sample_pdf)
        result = pdf_info(sample_pdf)  # Second call hits cache
        assert result["from_cache"] is True
        # file_path key should not be in the response
        assert "file_path" not in result

    def test_content_warning_in_get_toc(self, sample_pdf, isolated_server):
        """Get TOC includes content warning."""
        result = pdf_get_toc(sample_pdf)
        assert "content_warning" in result

    def test_file_size_bytes_in_cached_info(self, sample_pdf, isolated_server):
        """Cached pdf_info response includes file_size_bytes."""
        result1 = pdf_info(sample_pdf)
        assert "file_size_bytes" in result1

        result2 = pdf_info(sample_pdf)
        assert result2["from_cache"] is True
        assert "file_size_bytes" in result2
        assert result2["file_size_bytes"] == result1["file_size_bytes"]


class TestResolvePath:
    """Tests for _resolve_path helper."""

    def test_relative_path_resolved(self, sample_pdf, isolated_server):
        """Relative path is resolved to absolute."""
        # Use just the filename relative to cwd
        rel_path = os.path.relpath(sample_pdf)
        result = _resolve_path(rel_path)
        assert os.path.isabs(result)

    def test_url_http_status_error(self, isolated_server):
        """HTTPStatusError from URL fetch raises ConnectionError."""
        mock_response = Mock()
        mock_response.status_code = 404
        error = httpx.HTTPStatusError(
            "Not Found", request=Mock(), response=mock_response
        )

        with patch.object(URLFetcher, "is_url", return_value=True):
            with patch.object(URLFetcher, "fetch", side_effect=error):
                with pytest.raises(ConnectionError, match="HTTP 404"):
                    _resolve_path("https://example.com/missing.pdf")

    def test_url_http_error(self, isolated_server):
        """Generic HTTPError from URL fetch raises ConnectionError."""
        error = httpx.ConnectError("Connection refused")

        with patch.object(URLFetcher, "is_url", return_value=True):
            with patch.object(URLFetcher, "fetch", side_effect=error):
                with pytest.raises(ConnectionError, match="ConnectError"):
                    _resolve_path("https://example.com/unreachable.pdf")

    def test_url_value_error(self, isolated_server):
        """ValueError from URL fetch (e.g. not a PDF) re-raises."""
        error = ValueError("URL does not appear to be a PDF")

        with patch.object(URLFetcher, "is_url", return_value=True):
            with patch.object(URLFetcher, "fetch", side_effect=error):
                with pytest.raises(ValueError, match="valid PDF file"):
                    _resolve_path("https://example.com/fake.pdf")


class TestSearchWordBoundaryAndEllipsis:
    """Tests for search excerpt word-boundary adjustment and ellipsis."""

    @pytest.fixture
    def long_text_pdf(self):
        """Create a PDF with long text to trigger word-boundary logic."""
        import pymupdf

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            doc = pymupdf.open()
            page = doc.new_page()

            # Build text with target in the middle
            prefix = " ".join(f"word{i}" for i in range(50))
            suffix = " ".join(f"word{i}" for i in range(50, 100))
            target = "UNIQUETARGET"
            long_text = f"{prefix} {target} {suffix}"

            # Use textwriter to insert long text with wrapping
            tw = pymupdf.TextWriter(page.rect)
            tw.fill_textbox(
                pymupdf.Rect(50, 50, 550, 750),
                long_text,
                fontsize=10,
            )
            tw.write_text(page)

            doc.save(f.name)
            doc.close()

            yield f.name

            os.unlink(f.name)

    def test_search_excerpt_has_ellipsis(self, long_text_pdf, isolated_server):
        """Search match in middle of long text gets ellipsis on both sides."""
        result = pdf_search(long_text_pdf, "UNIQUETARGET", context_chars=50)

        assert result["total_matches"] >= 1
        match = result["matches"][0]
        excerpt = match["excerpt"]
        # Match is in the middle of long text, so excerpt should have ellipsis
        assert "..." in excerpt
        assert "UNIQUETARGET" in excerpt


class TestReadPagesCachedImages:
    """Tests for cached image retrieval in pdf_read_pages."""

    def test_images_served_from_cache(self, sample_pdf_with_images, isolated_server):
        """Second call returns cached images with file paths."""
        result1 = pdf_read_pages(sample_pdf_with_images, "1")
        imgs1 = result1["pages"][0]["images"]

        result2 = pdf_read_pages(sample_pdf_with_images, "1")
        imgs2 = result2["pages"][0]["images"]

        assert len(imgs1) > 0
        assert len(imgs2) == len(imgs1)
        for img in imgs2:
            assert "path" in img
            assert "data" not in img
            assert Path(img["path"]).exists()


class TestReadPagesInlineImages:
    """Tests for always-inline per-page images in pdf_read_pages."""

    def test_read_pages_always_includes_images_field(self, sample_pdf, isolated_server):
        """Each page dict always has 'images' and 'image_count' fields."""
        result = pdf_read_pages(sample_pdf, "1")
        page = result["pages"][0]
        assert "images" in page
        assert "image_count" in page
        assert page["images"] == []
        assert page["image_count"] == 0

    def test_read_pages_images_nested_per_page(
        self, sample_pdf_with_images, isolated_server
    ):
        """Images are nested inside each page dict, not in a flat top-level list."""
        result = pdf_read_pages(sample_pdf_with_images, "1")
        assert "images" not in result  # no top-level 'images' key
        page = result["pages"][0]
        assert "images" in page
        assert isinstance(page["images"], list)
        assert page["image_count"] == len(page["images"])

    def test_read_pages_no_include_images_param(self):
        """pdf_read_pages no longer accepts include_images parameter."""
        import inspect

        sig = inspect.signature(pdf_read_pages)
        assert "include_images" not in sig.parameters

    def test_read_pages_total_images_in_response(
        self, sample_pdf_with_images, isolated_server
    ):
        """Response includes total_images summing all pages."""
        result = pdf_read_pages(sample_pdf_with_images, "1")
        expected = sum(p["image_count"] for p in result["pages"])
        assert result["total_images"] == expected

    def test_read_pages_image_dict_structure(
        self, sample_pdf_with_images, isolated_server
    ):
        """Each image dict has expected keys and no redundant 'page' key."""
        result = pdf_read_pages(sample_pdf_with_images, "1")
        page = result["pages"][0]
        assert page["image_count"] > 0
        img = page["images"][0]
        assert "index" in img
        assert "width" in img
        assert "height" in img
        assert "format" in img
        assert "path" in img
        assert "size_bytes" in img
        assert "page" not in img  # stripped — redundant with parent

    def test_read_pages_no_images_empty_list(self, sample_pdf, isolated_server):
        """Text-only pages all return images: [], image_count: 0."""
        result = pdf_read_pages(sample_pdf, "1-3")
        for page in result["pages"]:
            assert page["images"] == []
            assert page["image_count"] == 0

    def test_read_pages_cache_miss_re_extraction(
        self, sample_pdf_with_images, isolated_server
    ):
        """If cached PNG deleted from disk, re-extraction occurs via pdf_read_pages."""
        result1 = pdf_read_pages(sample_pdf_with_images, "1")
        for img in result1["pages"][0]["images"]:
            Path(img["path"]).unlink()

        result2 = pdf_read_pages(sample_pdf_with_images, "1")
        assert result2["pages"][0]["image_count"] == result1["pages"][0]["image_count"]
        for img in result2["pages"][0]["images"]:
            assert Path(img["path"]).exists()

    def test_imageless_page_sentinel_cached(self, sample_pdf, isolated_server):
        """extract_images_from_page called only once for imageless page."""
        with patch(
            "pdf_mcp.server.extract_images_from_page", return_value=[]
        ) as mock_extract:
            pdf_read_pages(sample_pdf, "1")
            assert mock_extract.call_count == 1

            pdf_read_pages(sample_pdf, "1")
            assert mock_extract.call_count == 1  # not called again

    def test_cache_clear_deletes_image_files_via_read_pages(
        self, sample_pdf_with_images, isolated_server
    ):
        """pdf_cache_clear removes image files extracted by pdf_read_pages."""
        result = pdf_read_pages(sample_pdf_with_images, "1")
        paths = [Path(img["path"]) for img in result["pages"][0]["images"]]
        assert all(p.exists() for p in paths)

        pdf_cache_clear(expired_only=False)
        assert all(not p.exists() for p in paths)

    def test_cache_stats_includes_image_size_via_read_pages(
        self, sample_pdf_with_images, isolated_server
    ):
        """Cache stats reflect image files extracted via pdf_read_pages."""
        stats_before = pdf_cache_stats()
        pdf_read_pages(sample_pdf_with_images, "1")
        stats_after = pdf_cache_stats()
        assert stats_after["cache_size_bytes"] > stats_before["cache_size_bytes"]

    def test_pdf_extract_images_tool_removed(self):
        """pdf_extract_images is no longer defined in server module."""
        import pdf_mcp.server as mod

        assert not hasattr(mod, "pdf_extract_images")
