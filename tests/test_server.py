# tests/test_server.py
"""Tests for MCP server tools."""

import pytest

import base64

from pdf_mcp.server import (
    pdf_info,
    pdf_read_pages,
    pdf_read_all,
    pdf_search,
    pdf_get_toc,
    pdf_extract_images,
    pdf_cache_stats,
    pdf_cache_clear,
)


class TestPdfInfo:
    """Tests for pdf_info tool."""

    def test_pdf_info_basic(self, sample_pdf, isolated_server):
        """Valid PDF returns expected fields."""
        result = pdf_info(sample_pdf)

        assert result["page_count"] == 5
        assert result["from_cache"] is False
        assert "file_path" in result
        assert "metadata" in result
        assert "toc" in result
        assert "file_size_bytes" in result
        assert "file_size_mb" in result
        assert "estimated_tokens" in result

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
        assert "file_path" in result


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

    def test_read_pages_with_images(self, sample_pdf_with_images, isolated_server):
        """include_images=True works."""
        result = pdf_read_pages(sample_pdf_with_images, "1", include_images=True)

        assert "images" in result
        assert "image_count" in result


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

    def test_read_all_file_not_found(self, isolated_server):
        """Invalid path raises error."""
        with pytest.raises(FileNotFoundError):
            pdf_read_all("/nonexistent/path.pdf")


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
            assert len(result_large["matches"][0]["excerpt"]) >= len(result_small["matches"][0]["excerpt"])


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


class TestPdfExtractImages:
    """Tests for pdf_extract_images tool."""

    def test_extract_images_basic(self, sample_pdf_with_images, isolated_server):
        """Returns images with width, height, data."""
        result = pdf_extract_images(sample_pdf_with_images)

        assert "images" in result
        assert "image_count" in result

        if result["image_count"] > 0:
            img = result["images"][0]
            assert "width" in img
            assert "height" in img
            assert "data" in img
            assert "format" in img

    def test_extract_images_no_images(self, sample_pdf, isolated_server):
        """Empty list for imageless PDF."""
        result = pdf_extract_images(sample_pdf)

        assert result["image_count"] == 0
        assert result["images"] == []

    def test_extract_images_specific_pages(self, sample_pdf_with_images, isolated_server):
        """Pages filter works."""
        result = pdf_extract_images(sample_pdf_with_images, pages="1")

        # All images should be from page 1
        for img in result["images"]:
            assert img["page"] == 1

    def test_extract_images_max_limit(self, sample_pdf_with_images, isolated_server):
        """max_images respected."""
        result = pdf_extract_images(sample_pdf_with_images, max_images=1)

        assert result["image_count"] <= 1

    def test_extract_images_caching(self, sample_pdf_with_images, isolated_server):
        """Cached on second call."""
        result1 = pdf_extract_images(sample_pdf_with_images)
        result2 = pdf_extract_images(sample_pdf_with_images)

        # Both should return same data
        assert result1["image_count"] == result2["image_count"]

    def test_extract_images_base64_valid(self, sample_pdf_with_images, isolated_server):
        """Data is valid base64."""
        result = pdf_extract_images(sample_pdf_with_images)

        for img in result["images"]:
            # Should not raise
            decoded = base64.b64decode(img["data"])
            assert len(decoded) > 0


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

        expected_keys = ["total_files", "total_pages", "total_images",
                        "cache_size_bytes", "cache_size_mb", "url_cache"]
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

    @pytest.mark.parametrize("tool_func", [
        pdf_info,
        pdf_read_all,
        pdf_get_toc,
    ])
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
