"""
Tests for pdf-mcp server.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pymupdf
import pytest

from pdf_mcp.cache import PDFCache
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


# ============================================================================
# FTS5 Cache Tests
# ============================================================================


class TestFTS5Cache:
    """Tests for FTS5 full-text search index in PDFCache."""

    # --- Phase 1: Initialization ---

    def test_fts_table_exists_after_init(self, cache, sample_pdf):
        """pdf_search_fts virtual table exists in the database after init."""
        import sqlite3

        with sqlite3.connect(cache.db_path) as conn:
            result = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name='pdf_search_fts'"
            ).fetchone()
        if cache.fts_available:
            assert (
                result is not None
            ), "pdf_search_fts table should exist when FTS5 is available"
        else:
            assert (
                result is None
            ), "pdf_search_fts table should not exist when FTS5 unavailable"

    def test_fts_available_flag_set(self, cache):
        """PDFCache.fts_available attribute is a boolean."""
        assert isinstance(cache.fts_available, bool)

    def test_fts_unavailable_does_not_crash_init(self, temp_cache_dir, monkeypatch):
        """PDFCache initializes without error even when FTS5 CREATE fails."""
        import pdf_mcp.cache as cache_module

        # Replace the FTS5 schema with one that uses a non-existent virtual
        # table module — SQLite raises OperationalError naturally, exercising
        # the same try/except path as a build without FTS5 support.
        monkeypatch.setattr(
            cache_module,
            "_FTS5_TABLE_SCHEMA",
            "CREATE VIRTUAL TABLE IF NOT EXISTS pdf_search_fts"
            " USING no_such_fts_module(text)",
        )
        # Should not raise
        c = PDFCache(cache_dir=temp_cache_dir, ttl_hours=1)
        assert c.fts_available is False

    # --- Phase 2: Population ---

    def test_save_page_text_populates_fts_index(self, cache, sample_pdf):
        """save_page_text inserts a row into pdf_search_fts."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        cache.save_page_text(
            sample_pdf, 0, "The quick brown fox jumped over the lazy dog"
        )

        import sqlite3

        with sqlite3.connect(cache.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM pdf_search_fts WHERE file_path = ?", (sample_pdf,)
            ).fetchone()[0]
        assert count == 1

    def test_save_pages_text_populates_fts_index_all_pages(self, cache, sample_pdf):
        """save_pages_text inserts one FTS row per page."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        pages = {0: "First page text", 1: "Second page text", 2: "Third page text"}
        cache.save_pages_text(sample_pdf, pages)

        import sqlite3

        with sqlite3.connect(cache.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM pdf_search_fts WHERE file_path = ?", (sample_pdf,)
            ).fetchone()[0]
        assert count == 3

    def test_save_page_text_no_duplicate_fts_row(self, cache, sample_pdf):
        """Two save_page_text calls for the same page create exactly one FTS row."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        cache.save_page_text(sample_pdf, 0, "original text")
        cache.save_page_text(sample_pdf, 0, "updated text")

        import sqlite3

        with sqlite3.connect(cache.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM pdf_search_fts WHERE file_path = ?", (sample_pdf,)
            ).fetchone()[0]
        assert count == 1

    def test_save_page_text_updates_fts_content(self, cache, sample_pdf):
        """After two saves for the same page, FTS reflects the latest text."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        cache.save_page_text(sample_pdf, 0, "original content here")
        cache.save_page_text(sample_pdf, 0, "completely different words")

        results = cache.search_fts(
            sample_pdf, "completely", max_results=5, context_chars=50
        )
        assert len(results) == 1
        assert results[0]["page"] == 1  # 1-indexed

    def test_fts_not_populated_when_fts_unavailable(self, temp_cache_dir, sample_pdf):
        """When fts_available is False, save_page_text does not raise."""
        c = PDFCache(cache_dir=temp_cache_dir, ttl_hours=1)
        # Simulate FTS unavailability after construction by directly setting the flag
        c.fts_available = False
        # Must not raise
        c.save_page_text(sample_pdf, 0, "some text")

    # --- Phase 3: Search Methods ---

    def test_search_fts_returns_matches(self, cache, sample_pdf):
        """search_fts returns results when query matches indexed text."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        cache.save_page_text(sample_pdf, 4, "Authentication and authorization systems")
        cache.save_page_text(sample_pdf, 7, "Database connection pooling")

        results = cache.search_fts(
            sample_pdf, "authentication", max_results=10, context_chars=100
        )

        assert len(results) == 1
        assert results[0]["page"] == 5  # 1-indexed (page_num 4 → page 5)

    def test_search_fts_stemming_matches(self, cache, sample_pdf):
        """search_fts matches stemmed forms: 'authenticate' finds 'authentication'."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        cache.save_page_text(sample_pdf, 0, "The authentication system is robust")

        results = cache.search_fts(
            sample_pdf, "authenticate", max_results=5, context_chars=50
        )

        assert (
            len(results) == 1
        ), "Porter stemmer should match 'authenticate' to 'authentication'"

    def test_search_fts_case_insensitive(self, cache, sample_pdf):
        """search_fts matches regardless of case."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        cache.save_page_text(sample_pdf, 0, "lower case text")

        results = cache.search_fts(
            sample_pdf, "LOWER CASE", max_results=5, context_chars=50
        )
        assert len(results) == 1

    def test_search_fts_no_matches_returns_empty(self, cache, sample_pdf):
        """search_fts returns [] when query matches nothing."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        cache.save_page_text(sample_pdf, 0, "completely unrelated text here")

        results = cache.search_fts(
            sample_pdf, "xyznonexistent", max_results=10, context_chars=50
        )
        assert results == []

    def test_search_fts_empty_for_unindexed_file(self, cache):
        """search_fts returns [] when file has no FTS entries."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        results = cache.search_fts(
            "/nonexistent/file.pdf", "anything", max_results=10, context_chars=50
        )
        assert results == []

    def test_search_fts_result_has_required_keys(self, cache, sample_pdf):
        """Each result dict has page, excerpt, score — no match_count."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        cache.save_page_text(sample_pdf, 0, "unique search target keyword here")

        results = cache.search_fts(
            sample_pdf, "unique search target", max_results=5, context_chars=100
        )
        assert len(results) >= 1

        result = results[0]
        assert "page" in result
        assert "excerpt" in result
        assert "score" in result
        assert "match_count" not in result
        assert isinstance(result["page"], int)
        assert isinstance(result["excerpt"], str)
        assert isinstance(result["score"], float)
        assert result["score"] >= 0.0

    def test_get_fts_page_counts_returns_all_matching_pages(self, cache, sample_pdf):
        """get_fts_page_counts returns all matching pages (no LIMIT applied)."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        for i in range(8):
            cache.save_page_text(sample_pdf, i, f"page {i} contains the word fox here")

        counts = cache.get_fts_page_counts(sample_pdf, "fox")

        assert len(counts) == 8
        for page_num, count in counts.items():
            assert isinstance(page_num, int)  # 0-indexed
            assert isinstance(count, int)
            assert count >= 1

    def test_get_fts_page_counts_not_capped_by_max_results(self, cache, sample_pdf):
        """get_fts_page_counts returns all pages even if count > max_results."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        for i in range(10):
            cache.save_page_text(sample_pdf, i, f"page {i} target word present")

        counts = cache.get_fts_page_counts(sample_pdf, "target")

        assert len(counts) == 10

    def test_get_fts_page_counts_reflects_literal_occurrences(self, cache, sample_pdf):
        """Count reflects literal case-insensitive occurrences, not stemmed matches."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        cache.save_page_text(
            sample_pdf, 0, "fox fox fox ran past the fox den"
        )  # 4 occurrences
        cache.save_page_text(sample_pdf, 1, "one single fox here")  # 1 occurrence

        counts = cache.get_fts_page_counts(sample_pdf, "fox")

        assert counts[0] == 4
        assert counts[1] == 1

    def test_get_fts_page_counts_empty_when_no_match(self, cache, sample_pdf):
        """get_fts_page_counts returns {} when query matches nothing."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        cache.save_page_text(sample_pdf, 0, "completely unrelated content here")

        counts = cache.get_fts_page_counts(sample_pdf, "xyznonexistent")
        assert counts == {}

    def test_get_fts_page_counts_scoped_to_file(self, cache, sample_pdf, tmp_path):
        """get_fts_page_counts only returns pages from the specified file."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        import shutil

        other_pdf = str(tmp_path / "other.pdf")
        shutil.copy(sample_pdf, other_pdf)

        cache.save_page_text(sample_pdf, 0, "apple banana cherry")
        cache.save_page_text(other_pdf, 0, "apple banana cherry")

        counts = cache.get_fts_page_counts(sample_pdf, "apple")
        assert len(counts) == 1  # only sample_pdf page 0, not other_pdf

    def test_search_fts_excerpt_contains_match_context(self, cache, sample_pdf):
        """Excerpt is non-empty and contains characters from the matched page text."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        cache.save_page_text(
            sample_pdf, 0, "The quick brown fox jumped over the lazy dog indeed"
        )

        results = cache.search_fts(sample_pdf, "fox", max_results=5, context_chars=50)
        assert len(results) >= 1
        assert len(results[0]["excerpt"]) > 0

    def test_search_fts_max_results_honored(self, cache, sample_pdf):
        """search_fts returns at most max_results rows."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        for i in range(10):
            cache.save_page_text(
                sample_pdf, i, f"page {i} contains the word target here"
            )

        results = cache.search_fts(
            sample_pdf, "target", max_results=3, context_chars=50
        )
        assert len(results) <= 3

    def test_search_fts_results_ordered_by_relevance(self, cache, sample_pdf):
        """Results with higher relevance (more query terms) come first."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        cache.save_page_text(sample_pdf, 0, "The revenue growth was modest this year")
        cache.save_page_text(
            sample_pdf,
            1,
            "Revenue growth revenue growth revenue growth exceeded all targets",
        )

        results = cache.search_fts(
            sample_pdf, "revenue growth", max_results=10, context_chars=50
        )
        assert len(results) == 2
        assert results[0]["score"] >= results[1]["score"]

    def test_search_fts_only_returns_results_for_given_file(
        self, cache, sample_pdf, tmp_path
    ):
        """search_fts scoped to the given file_path only."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        other_pdf = str(tmp_path / "other.pdf")
        import shutil

        shutil.copy(sample_pdf, other_pdf)

        cache.save_page_text(sample_pdf, 0, "apple banana cherry")
        cache.save_page_text(other_pdf, 0, "apple banana cherry")

        results = cache.search_fts(
            sample_pdf, "apple", max_results=10, context_chars=50
        )
        assert all(r["page"] is not None for r in results)
        assert len(results) == 1

    def test_search_fts_query_with_fts5_reserved_word(self, cache, sample_pdf):
        """search_fts does not crash when query is an FTS5 reserved keyword."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        cache.save_page_text(sample_pdf, 0, "We need to AND the results together")

        results = cache.search_fts(sample_pdf, "AND", max_results=5, context_chars=50)
        assert isinstance(results, list)
        assert len(results) == 1

    def test_search_fts_query_with_special_chars_no_crash(self, cache, sample_pdf):
        """search_fts handles queries with parentheses and quotes without raising."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        cache.save_page_text(sample_pdf, 0, "some normal page text")

        try:
            cache.search_fts(sample_pdf, "OR NOT", max_results=5, context_chars=50)
            cache.search_fts(
                sample_pdf, "(parenthesized)", max_results=5, context_chars=50
            )
            cache.search_fts(
                sample_pdf, '"quoted phrase"', max_results=5, context_chars=50
            )
        except Exception as e:
            pytest.fail(
                f"search_fts raised {type(e).__name__} for special-char query: {e}"
            )

    # --- Phase 4: Coverage and Stats ---

    def test_get_fts_index_coverage_unindexed(self, cache, sample_pdf):
        """get_fts_index_coverage returns (0, 0) for a file with no cached text."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        indexed, total = cache.get_fts_index_coverage(sample_pdf)
        assert indexed == 0
        assert total == 0

    def test_get_fts_index_coverage_returns_zeros_when_fts_unavailable(
        self, temp_cache_dir, sample_pdf
    ):
        """get_fts_index_coverage returns (0, N) when fts_available is False."""
        c = PDFCache(cache_dir=temp_cache_dir, ttl_hours=1)
        c.fts_available = True
        c.save_page_text(sample_pdf, 0, "some text")
        c.fts_available = False

        indexed, total = c.get_fts_index_coverage(sample_pdf)
        assert indexed == 0
        assert total >= 1

    def test_get_fts_index_coverage_all_pages_indexed(self, cache, sample_pdf):
        """get_fts_index_coverage returns (N, N) when all saved pages are indexed."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        cache.save_page_text(sample_pdf, 0, "page zero")
        cache.save_page_text(sample_pdf, 1, "page one")
        cache.save_page_text(sample_pdf, 2, "page two")

        indexed, total = cache.get_fts_index_coverage(sample_pdf)
        assert indexed == 3
        assert total == 3

    def test_get_stats_includes_fts_indexed_pages(self, cache, sample_pdf):
        """get_stats() includes fts_indexed_pages key."""
        stats = cache.get_stats()
        assert "fts_indexed_pages" in stats
        assert isinstance(stats["fts_indexed_pages"], int)

    def test_get_stats_fts_count_increases_after_indexing(self, cache, sample_pdf):
        """fts_indexed_pages increases as pages are saved."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        stats_before = cache.get_stats()
        cache.save_page_text(sample_pdf, 0, "some text here")
        stats_after = cache.get_stats()

        assert stats_after["fts_indexed_pages"] == stats_before["fts_indexed_pages"] + 1

    # --- Phase 5: Cache Invalidation ---

    def test_invalidate_file_removes_fts_rows(self, cache, sample_pdf):
        """_invalidate_file removes all FTS rows for the given file."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        cache.save_page_text(sample_pdf, 0, "text to index")
        cache.save_page_text(sample_pdf, 1, "more text to index")

        cache._invalidate_file(sample_pdf)

        import sqlite3

        with sqlite3.connect(cache.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM pdf_search_fts WHERE file_path = ?", (sample_pdf,)
            ).fetchone()[0]
        assert count == 0

    def test_clear_all_empties_fts_table(self, cache, sample_pdf):
        """clear_all() deletes all rows from pdf_search_fts."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        cache.save_page_text(sample_pdf, 0, "hello world")
        cache.clear_all()

        import sqlite3

        with sqlite3.connect(cache.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM pdf_search_fts").fetchone()[0]
        assert count == 0

    def test_clear_expired_removes_fts_rows_for_expired_files(
        self, temp_cache_dir, sample_pdf
    ):
        """clear_expired() removes FTS rows for expired (old accessed_at) files."""
        if not PDFCache(cache_dir=temp_cache_dir, ttl_hours=1).fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        short_ttl_cache = PDFCache(cache_dir=temp_cache_dir, ttl_hours=0)
        short_ttl_cache.save_metadata(sample_pdf, 5, {}, [])
        short_ttl_cache.save_page_text(sample_pdf, 0, "expire me")

        import sqlite3

        with sqlite3.connect(short_ttl_cache.db_path) as conn:
            conn.execute(
                "UPDATE pdf_metadata SET accessed_at = '2000-01-01'"
                " WHERE file_path = ?",
                (sample_pdf,),
            )

        short_ttl_cache.clear_expired()

        with sqlite3.connect(short_ttl_cache.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM pdf_search_fts WHERE file_path = ?", (sample_pdf,)
            ).fetchone()[0]
        assert count == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
