"""
Tests for pdf-mcp server.
"""

import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pymupdf
import pytest

from pdf_mcp.cache import PDFCache
from pdf_mcp.config import PDFConfig
from pdf_mcp.extractor import (
    estimate_tokens,
    extract_images_from_page,
    extract_metadata,
    extract_text_from_page,
    extract_toc,
    get_best_paragraph_for_query,
    get_paragraph_for_offset,
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

    def test_search_fts_multi_word_token_and(self, cache, sample_pdf):
        """Multi-word query matches when all tokens appear on the page,
        even if non-contiguous or in different order."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        cache.save_page_text(
            sample_pdf,
            0,
            "our benchmark shows pgvector achieves 12ms p50 latency with HNSW",
        )

        # both words present, non-contiguous → must match
        assert (
            len(
                cache.search_fts(
                    sample_pdf, "pgvector latency", max_results=5, context_chars=50
                )
            )
            == 1
        )
        # reversed order → must match
        assert (
            len(
                cache.search_fts(
                    sample_pdf, "latency pgvector", max_results=5, context_chars=50
                )
            )
            == 1
        )
        # any missing token → no match (AND semantics)
        assert (
            cache.search_fts(
                sample_pdf, "pgvector unicorn", max_results=5, context_chars=50
            )
            == []
        )

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

    # --- FTS fallback and error paths ---

    def test_get_page_tables_stale_mtime_returns_none(self, cache, sample_pdf):
        """get_page_tables returns None when file mtime has changed since caching."""
        import os
        import time

        cache.save_page_tables(sample_pdf, 0, [{"header": ["Col"], "rows": [["v"]]}])
        future = time.time() + 100
        os.utime(sample_pdf, (future, future))
        assert cache.get_page_tables(sample_pdf, 0) is None

    def test_get_stats_fts_indexed_pages_zero_when_unavailable(self, cache, sample_pdf):
        """get_stats returns fts_indexed_pages=0 when fts_available is False."""
        cache.fts_available = False
        stats = cache.get_stats()
        assert stats["fts_indexed_pages"] == 0

    def test_search_fts_returns_empty_when_fts_unavailable(self, cache, sample_pdf):
        """search_fts returns [] immediately when fts_available is False."""
        cache.fts_available = False
        result = cache.search_fts(sample_pdf, "query", max_results=5, context_chars=80)
        assert result == []

    def test_search_fts_returns_empty_on_operational_error(self, cache, sample_pdf):
        """search_fts returns [] when the FTS table is missing (OperationalError)."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        import sqlite3

        with sqlite3.connect(cache.db_path) as conn:
            conn.execute("DROP TABLE IF EXISTS pdf_search_fts")

        result = cache.search_fts(
            sample_pdf, "anything", max_results=5, context_chars=80
        )
        assert result == []

    def test_get_fts_page_counts_returns_empty_when_fts_unavailable(
        self, cache, sample_pdf
    ):
        """get_fts_page_counts returns {} immediately when fts_available is False."""
        cache.fts_available = False
        result = cache.get_fts_page_counts(sample_pdf, "query")
        assert result == {}

    def test_get_fts_page_counts_returns_empty_on_operational_error(
        self, cache, sample_pdf
    ):
        """get_fts_page_counts returns {} when FTS table is missing."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        import sqlite3

        with sqlite3.connect(cache.db_path) as conn:
            conn.execute("DROP TABLE IF EXISTS pdf_search_fts")

        result = cache.get_fts_page_counts(sample_pdf, "query")
        assert result == {}


class TestPDFConfigEmbeddingModel:
    """PDFConfig.embedding_model reads [embedding] model from config.toml."""

    def test_embedding_model_default(self, tmp_path):
        """Returns default model when [embedding] section is absent."""
        cfg = PDFConfig(config_path=tmp_path / "missing.toml")
        assert cfg.embedding_model == "BAAI/bge-small-en-v1.5"

    def test_embedding_model_configured(self, tmp_path):
        """Returns the model name set in [embedding] model = ..."""
        config_file = tmp_path / "config.toml"
        config_file.write_text('[embedding]\nmodel = "BAAI/bge-large-en-v1.5"\n')
        cfg = PDFConfig(config_path=config_file)
        assert cfg.embedding_model == "BAAI/bge-large-en-v1.5"

    def test_embedding_model_section_present_key_absent(self, tmp_path):
        """Returns default when [embedding] section exists but model key absent."""
        config_file = tmp_path / "config.toml"
        config_file.write_text("[embedding]\n")
        cfg = PDFConfig(config_path=config_file)
        assert cfg.embedding_model == "BAAI/bge-small-en-v1.5"


class TestEmbedderByom:
    """Embedder singleton reloads on model change; check_available validates name."""

    def _fake_fastembed(self, monkeypatch, call_log=None):
        """Inject a fake fastembed module into sys.modules."""
        import sys

        log = call_log if call_log is not None else []

        class FakeTextEmbedding:
            def __init__(self, model_name):
                log.append(model_name)

            @staticmethod
            def list_supported_models():
                return [
                    {"model": "BAAI/bge-small-en-v1.5"},
                    {"model": "BAAI/bge-large-en-v1.5"},
                ]

        fake = type(sys)("fastembed")
        fake.TextEmbedding = FakeTextEmbedding
        monkeypatch.setitem(sys.modules, "fastembed", fake)
        return log

    def test_get_model_reloads_on_model_change(self, monkeypatch):
        """_get_model loads a new TextEmbedding when model_name changes."""
        import pdf_mcp.embedder as embedder

        call_log = self._fake_fastembed(monkeypatch, call_log=[])
        monkeypatch.setattr(embedder, "_model", None)
        monkeypatch.setattr(embedder, "_model_name_loaded", None)

        embedder._get_model("BAAI/bge-small-en-v1.5")
        embedder._get_model("BAAI/bge-small-en-v1.5")  # cached — no reload
        embedder._get_model("BAAI/bge-large-en-v1.5")  # different — reload

        assert call_log == ["BAAI/bge-small-en-v1.5", "BAAI/bge-large-en-v1.5"]

    def test_check_available_unknown_model_raises_valueerror(self, monkeypatch):
        """check_available raises ValueError for an unknown model name."""
        import pdf_mcp.embedder as embedder

        self._fake_fastembed(monkeypatch)

        with pytest.raises(ValueError, match="Unknown embedding model 'bad-model'"):
            embedder.check_available("bad-model")

    def test_check_available_unknown_model_lists_supported(self, monkeypatch):
        """ValueError message includes the supported model names."""
        import pdf_mcp.embedder as embedder

        self._fake_fastembed(monkeypatch)

        with pytest.raises(ValueError, match="BAAI/bge-small-en-v1.5"):
            embedder.check_available("bad-model")

    def test_check_available_known_model_passes(self, monkeypatch):
        """check_available does not raise for a known model name."""
        import pdf_mcp.embedder as embedder

        self._fake_fastembed(monkeypatch)
        embedder.check_available("BAAI/bge-small-en-v1.5")  # must not raise


class TestPageEmbeddingsTable:
    """page_embeddings table and index are created by PDFCache.__init__."""

    def test_page_embeddings_table_exists(self, temp_cache_dir):
        """PDFCache creates page_embeddings table on init."""
        cache = PDFCache(cache_dir=temp_cache_dir)
        with sqlite3.connect(cache.db_path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert "page_embeddings" in tables

    def test_page_embeddings_index_exists(self, temp_cache_dir):
        """idx_page_embeddings_path index is created alongside the table."""
        cache = PDFCache(cache_dir=temp_cache_dir)
        with sqlite3.connect(cache.db_path) as conn:
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
        assert "idx_page_embeddings_path" in indexes


class TestPageEmbeddingsCRUD:
    """get/save page embeddings round-trip and mtime invalidation."""

    def test_save_and_get_round_trip(self, temp_cache_dir, sample_pdf):
        """save_page_embeddings → get_page_embeddings returns identical bytes."""
        cache = PDFCache(cache_dir=temp_cache_dir)
        raw = bytes(range(256)) * 6  # 1536 bytes = 384 float32s

        cache.save_page_embeddings(sample_pdf, {0: raw}, "BAAI/bge-small-en-v1.5")
        result = cache.get_page_embeddings(sample_pdf, [0], "BAAI/bge-small-en-v1.5")

        assert 0 in result
        assert result[0] == raw

    def test_get_returns_empty_when_nothing_saved(self, temp_cache_dir, sample_pdf):
        """get_page_embeddings returns {} when no embeddings are cached."""
        cache = PDFCache(cache_dir=temp_cache_dir)
        assert (
            cache.get_page_embeddings(sample_pdf, [0, 1, 2], "BAAI/bge-small-en-v1.5")
            == {}
        )

    def test_get_empty_page_nums_returns_empty(self, temp_cache_dir, sample_pdf):
        """get_page_embeddings([]) returns {} without hitting the database."""
        cache = PDFCache(cache_dir=temp_cache_dir)
        assert cache.get_page_embeddings(sample_pdf, [], "BAAI/bge-small-en-v1.5") == {}

    def test_get_multiple_pages(self, temp_cache_dir, sample_pdf):
        """Multiple pages saved and retrieved correctly."""
        cache = PDFCache(cache_dir=temp_cache_dir)
        raw0 = b"\x00" * 1536
        raw1 = b"\xff" * 1536
        raw2 = b"\x80" * 1536

        cache.save_page_embeddings(
            sample_pdf, {0: raw0, 1: raw1, 2: raw2}, "BAAI/bge-small-en-v1.5"
        )
        result = cache.get_page_embeddings(
            sample_pdf, [0, 1, 2], "BAAI/bge-small-en-v1.5"
        )

        assert set(result.keys()) == {0, 1, 2}
        assert result[0] == raw0
        assert result[1] == raw1
        assert result[2] == raw2

    def test_get_only_returns_requested_pages(self, temp_cache_dir, sample_pdf):
        """get_page_embeddings only returns the pages in page_nums."""
        cache = PDFCache(cache_dir=temp_cache_dir)
        cache.save_page_embeddings(
            sample_pdf, {0: b"\x01" * 1536, 1: b"\x02" * 1536}, "BAAI/bge-small-en-v1.5"
        )
        result = cache.get_page_embeddings(sample_pdf, [0], "BAAI/bge-small-en-v1.5")

        assert 0 in result
        assert 1 not in result

    def test_mtime_invalidation(self, temp_cache_dir, sample_pdf):
        """Embeddings are stale after the PDF's mtime changes."""
        import os
        import time

        cache = PDFCache(cache_dir=temp_cache_dir)
        cache.save_page_embeddings(
            sample_pdf, {0: b"\x00" * 1536}, "BAAI/bge-small-en-v1.5"
        )

        time.sleep(0.01)
        os.utime(sample_pdf, None)  # bump mtime

        result = cache.get_page_embeddings(sample_pdf, [0], "BAAI/bge-small-en-v1.5")
        assert result == {}


class TestPageEmbeddingsByom:
    """page_embeddings has model column; cache evicts rows from other models."""

    def test_page_embeddings_has_model_column(self, temp_cache_dir):
        """New cache has model column in page_embeddings."""
        cache = PDFCache(cache_dir=temp_cache_dir)
        with sqlite3.connect(cache.db_path) as conn:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(page_embeddings)").fetchall()
            }
        assert "model" in cols

    def test_migration_adds_model_column_to_existing_db(self, temp_cache_dir):
        """Existing page_embeddings table without model column gets it on init."""
        db_path = temp_cache_dir / "cache.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute("""
                CREATE TABLE page_embeddings (
                    file_path TEXT NOT NULL,
                    page_num  INTEGER NOT NULL,
                    file_mtime REAL NOT NULL,
                    embedding BLOB NOT NULL,
                    PRIMARY KEY (file_path, page_num)
                )
            """)
        PDFCache(cache_dir=temp_cache_dir)
        with sqlite3.connect(db_path) as conn:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(page_embeddings)").fetchall()
            }
        assert "model" in cols

    def test_save_and_get_round_trip_with_model(self, temp_cache_dir, sample_pdf):
        """save → get returns identical bytes for the same model."""
        cache = PDFCache(cache_dir=temp_cache_dir)
        raw = b"\xab" * 1536
        cache.save_page_embeddings(sample_pdf, {0: raw}, "BAAI/bge-small-en-v1.5")
        result = cache.get_page_embeddings(sample_pdf, [0], "BAAI/bge-small-en-v1.5")
        assert result == {0: raw}

    def test_model_change_evicts_stale_rows(self, temp_cache_dir, sample_pdf):
        """get_page_embeddings deletes rows from a different model before returning."""
        cache = PDFCache(cache_dir=temp_cache_dir)
        raw = b"\xab" * 1536
        cache.save_page_embeddings(sample_pdf, {0: raw}, "BAAI/bge-small-en-v1.5")

        result = cache.get_page_embeddings(sample_pdf, [0], "BAAI/bge-large-en-v1.5")
        assert result == {}

        with sqlite3.connect(cache.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM page_embeddings WHERE file_path = ?",
                (sample_pdf,),
            ).fetchone()[0]
        assert count == 0

    def test_migration_existing_rows_get_default_model(
        self, temp_cache_dir, sample_pdf
    ):
        """Rows inserted before migration get model='BAAI/bge-small-en-v1.5' default."""
        db_path = temp_cache_dir / "cache.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute("""
                CREATE TABLE page_embeddings (
                    file_path TEXT NOT NULL,
                    page_num  INTEGER NOT NULL,
                    file_mtime REAL NOT NULL,
                    embedding BLOB NOT NULL,
                    PRIMARY KEY (file_path, page_num)
                )
            """)
            conn.execute(
                "INSERT INTO page_embeddings VALUES (?, 0, 0.0, ?)",
                (sample_pdf, b"\x00" * 1536),
            )
        PDFCache(cache_dir=temp_cache_dir)
        with sqlite3.connect(db_path) as conn:
            model_val = conn.execute(
                "SELECT model FROM page_embeddings WHERE file_path = ?",
                (sample_pdf,),
            ).fetchone()[0]
        assert model_val == "BAAI/bge-small-en-v1.5"


class TestPageRendersCache:
    """Tests for page_renders table and renders_dir."""

    def test_renders_dir_created(self, temp_cache_dir):
        """PDFCache creates renders_dir on init."""
        from pdf_mcp.cache import PDFCache

        c = PDFCache(cache_dir=temp_cache_dir, ttl_hours=1)
        assert c.renders_dir.exists()
        assert c.renders_dir != c.images_dir

    def test_renders_dir_permissions(self, temp_cache_dir):
        """renders_dir has 0o700 permissions."""
        import stat
        from pdf_mcp.cache import PDFCache

        c = PDFCache(cache_dir=temp_cache_dir, ttl_hours=1)
        mode = stat.S_IMODE(c.renders_dir.stat().st_mode)
        assert mode == 0o700

    def test_get_page_render_miss(self, cache):
        """Returns None when no render cached."""
        assert cache.get_page_render("/some/file.pdf", 0, 200) is None

    def test_save_and_get_page_render(self, cache, sample_pdf):
        """Round-trip: save render dict then retrieve it."""
        fake_path = cache.renders_dir / "test_render.png"
        fake_path.write_bytes(b"fakepng")
        render_dict = {
            "file_path_on_disk": str(fake_path),
            "size_bytes": 7,
            "width": 100,
            "height": 200,
        }
        import os

        mtime = os.stat(sample_pdf).st_mtime
        cache.save_page_render(sample_pdf, 0, mtime, 200, render_dict)
        result = cache.get_page_render(sample_pdf, 0, 200)
        assert result is not None
        assert result["width"] == 100
        assert result["height"] == 200
        assert result["file_path_on_disk"] == str(fake_path)

    def test_get_page_render_different_dpi_miss(self, cache, sample_pdf):
        """Different DPI is a cache miss."""
        fake_path = cache.renders_dir / "test_render200.png"
        fake_path.write_bytes(b"fakepng")
        import os

        mtime = os.stat(sample_pdf).st_mtime
        cache.save_page_render(
            sample_pdf,
            0,
            mtime,
            200,
            {
                "file_path_on_disk": str(fake_path),
                "size_bytes": 7,
                "width": 100,
                "height": 200,
            },
        )
        assert cache.get_page_render(sample_pdf, 0, 300) is None

    def test_get_page_render_missing_file_returns_none(self, cache, sample_pdf):
        """Returns None if the PNG file has been deleted from disk."""
        import os

        mtime = os.stat(sample_pdf).st_mtime
        cache.save_page_render(
            sample_pdf,
            0,
            mtime,
            200,
            {
                "file_path_on_disk": "/nonexistent/render.png",
                "size_bytes": 1,
                "width": 10,
                "height": 10,
            },
        )
        assert cache.get_page_render(sample_pdf, 0, 200) is None

    def test_save_page_render_orphan_guard(self, cache, sample_pdf):
        """Saving a new render for same page/dpi unlinks the old PNG."""
        import os

        mtime = os.stat(sample_pdf).st_mtime
        old_path = cache.renders_dir / "old_render.png"
        old_path.write_bytes(b"old")
        cache.save_page_render(
            sample_pdf,
            0,
            mtime,
            200,
            {
                "file_path_on_disk": str(old_path),
                "size_bytes": 3,
                "width": 10,
                "height": 10,
            },
        )
        new_path = cache.renders_dir / "new_render.png"
        new_path.write_bytes(b"new")
        cache.save_page_render(
            sample_pdf,
            0,
            mtime,
            200,
            {
                "file_path_on_disk": str(new_path),
                "size_bytes": 3,
                "width": 10,
                "height": 10,
            },
        )
        assert not old_path.exists()
        assert new_path.exists()


class TestPageTextSource:
    """Tests for source column on page_text."""

    def test_save_page_text_default_source_is_extracted(self, cache, sample_pdf):
        """save_page_text with no source arg defaults to 'extracted'."""
        cache.save_page_text(sample_pdf, 0, "hello world")
        source = cache.get_page_source(sample_pdf, 0)
        assert source == "extracted"

    def test_save_page_text_ocr_source(self, cache, sample_pdf):
        """save_page_text with source='ocr' is stored and retrieved."""
        cache.save_page_text(sample_pdf, 0, "ocr text", source="ocr")
        assert cache.get_page_source(sample_pdf, 0) == "ocr"

    def test_get_page_source_miss(self, cache):
        """Returns None for uncached page."""
        assert cache.get_page_source("/nonexistent.pdf", 0) is None

    def test_get_pages_source_bulk(self, cache, sample_pdf):
        """get_pages_source returns dict of sources for multiple pages."""
        cache.save_page_text(sample_pdf, 0, "native text", source="extracted")
        cache.save_page_text(sample_pdf, 1, "ocr text", source="ocr")
        sources = cache.get_pages_source(sample_pdf, [0, 1, 2])
        assert sources[0] == "extracted"
        assert sources[1] == "ocr"
        assert 2 not in sources  # page 2 not cached

    def test_get_page_text_return_type_unchanged(self, cache, sample_pdf):
        """get_page_text still returns str, not a tuple."""
        cache.save_page_text(sample_pdf, 0, "hello", source="ocr")
        result = cache.get_page_text(sample_pdf, 0)
        assert isinstance(result, str)
        assert result == "hello"


class TestTextCoverageCache:
    """Tests for text_coverage_json on pdf_metadata."""

    def test_save_metadata_without_coverage(self, cache, sample_pdf):
        """save_metadata with no coverage stores None for text_coverage."""
        cache.save_metadata(sample_pdf, 5, {}, [])
        result = cache.get_metadata(sample_pdf)
        assert result is not None
        assert result["text_coverage"] is None

    def test_save_and_get_text_coverage(self, cache, sample_pdf):
        """Coverage saved round-trips correctly."""
        coverage = [
            {"page": 1, "text_chars": 100, "raster_images": 0},
            {"page": 2, "text_chars": 0, "raster_images": 1},
        ]
        cache.save_metadata(sample_pdf, 2, {}, [], text_coverage=coverage)
        result = cache.get_metadata(sample_pdf)
        assert result["text_coverage"] == coverage

    def test_save_coverage_update(self, cache, sample_pdf):
        """Calling save_metadata again with coverage replaces old value."""
        cache.save_metadata(sample_pdf, 2, {}, [], text_coverage=None)
        coverage = [{"page": 1, "text_chars": 50, "raster_images": 0}]
        cache.save_metadata(sample_pdf, 2, {}, [], text_coverage=coverage)
        result = cache.get_metadata(sample_pdf)
        assert result["text_coverage"] == coverage


class TestRenderCacheHousekeeping:
    """Tests for _invalidate_file, clear_expired, clear_all, get_stats with renders."""

    def test_invalidate_file_deletes_render_rows_and_files(self, cache, sample_pdf):
        """_invalidate_file removes page_renders DB rows and unlinks PNG files."""
        import os

        mtime = os.stat(sample_pdf).st_mtime
        png = cache.renders_dir / "inv_test.png"
        png.write_bytes(b"x")
        cache.save_page_render(
            sample_pdf,
            0,
            mtime,
            200,
            {"file_path_on_disk": str(png), "size_bytes": 1, "width": 10, "height": 10},
        )
        cache._invalidate_file(sample_pdf)
        assert cache.get_page_render(sample_pdf, 0, 200) is None
        assert not png.exists()

    def test_clear_all_removes_renders_dir_contents(self, cache, sample_pdf):
        """clear_all removes render PNGs."""
        import os

        mtime = os.stat(sample_pdf).st_mtime
        png = cache.renders_dir / "clear_test.png"
        png.write_bytes(b"x")
        cache.save_page_render(
            sample_pdf,
            0,
            mtime,
            200,
            {"file_path_on_disk": str(png), "size_bytes": 1, "width": 10, "height": 10},
        )
        cache.clear_all()
        assert not png.exists()
        assert cache.get_page_render(sample_pdf, 0, 200) is None

    def test_get_stats_includes_total_renders(self, cache, sample_pdf):
        """get_stats returns total_renders count."""
        import os

        result = cache.get_stats()
        assert "total_renders" in result
        assert result["total_renders"] == 0

        mtime = os.stat(sample_pdf).st_mtime
        png = cache.renders_dir / "stats_test.png"
        png.write_bytes(b"x")
        cache.save_page_render(
            sample_pdf,
            0,
            mtime,
            200,
            {"file_path_on_disk": str(png), "size_bytes": 1, "width": 10, "height": 10},
        )
        result = cache.get_stats()
        assert result["total_renders"] == 1

    def test_get_stats_cache_size_includes_renders_dir(self, cache, sample_pdf):
        """cache_size_bytes includes render PNG file sizes."""
        import os

        before = cache.get_stats()["cache_size_bytes"]
        png = cache.renders_dir / "size_test.png"
        png.write_bytes(b"x" * 1000)
        mtime = os.stat(sample_pdf).st_mtime
        cache.save_page_render(
            sample_pdf,
            0,
            mtime,
            200,
            {
                "file_path_on_disk": str(png),
                "size_bytes": 1000,
                "width": 10,
                "height": 10,
            },
        )
        after = cache.get_stats()["cache_size_bytes"]
        assert after > before


class TestExtractorRenderAndOcr:
    """Tests for render_page_as_png, check_tesseract_available, ocr_page."""

    def test_render_page_as_png_creates_file(self, sample_pdf, temp_cache_dir):
        """render_page_as_png saves a PNG to disk and returns metadata."""
        import pymupdf as _pymupdf
        from pdf_mcp.extractor import render_page_as_png

        doc = _pymupdf.open(sample_pdf)
        try:
            result = render_page_as_png(doc, 0, temp_cache_dir, "testhash", dpi=72)
        finally:
            doc.close()
        assert Path(result["file_path_on_disk"]).exists()
        assert result["size_bytes"] > 0
        assert result["width"] > 0
        assert result["height"] > 0

    def test_render_page_as_png_dimensions_scale_with_dpi(
        self, sample_pdf, temp_cache_dir
    ):
        """Higher DPI produces larger pixel dimensions."""
        import pymupdf as _pymupdf
        from pdf_mcp.extractor import render_page_as_png

        doc = _pymupdf.open(sample_pdf)
        try:
            low = render_page_as_png(doc, 0, temp_cache_dir, "hash_low", dpi=72)
            high = render_page_as_png(doc, 0, temp_cache_dir, "hash_high", dpi=200)
        finally:
            doc.close()
        assert high["width"] > low["width"]
        assert high["height"] > low["height"]

    def test_render_page_as_png_file_permissions(self, sample_pdf, temp_cache_dir):
        """Rendered PNG has 0o600 permissions."""
        import stat
        import pymupdf as _pymupdf
        from pdf_mcp.extractor import render_page_as_png

        doc = _pymupdf.open(sample_pdf)
        try:
            result = render_page_as_png(doc, 0, temp_cache_dir, "perm_hash", dpi=72)
        finally:
            doc.close()
        mode = stat.S_IMODE(Path(result["file_path_on_disk"]).stat().st_mode)
        assert mode == 0o600

    def test_render_page_as_png_deterministic_filename(
        self, sample_pdf, temp_cache_dir
    ):
        """Filename contains hash, page number, and DPI."""
        import pymupdf as _pymupdf
        from pdf_mcp.extractor import render_page_as_png

        doc = _pymupdf.open(sample_pdf)
        try:
            result = render_page_as_png(doc, 2, temp_cache_dir, "myhash", dpi=150)
        finally:
            doc.close()
        filename = Path(result["file_path_on_disk"]).name
        assert "myhash" in filename
        assert "p2" in filename
        assert "150dpi" in filename

    def test_check_tesseract_available_raises_when_missing(self):
        """check_tesseract_available raises RuntimeError when binary not on PATH."""
        from unittest.mock import patch
        from pdf_mcp.extractor import check_tesseract_available

        with patch("subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(RuntimeError, match="Tesseract not found"):
                check_tesseract_available()

    def test_check_tesseract_available_passes_when_present(self):
        """check_tesseract_available does not raise when binary is present."""
        from unittest.mock import patch, MagicMock
        from pdf_mcp.extractor import check_tesseract_available

        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result):
            check_tesseract_available()  # should not raise

    def test_ocr_page_returns_string(self, sample_pdf):
        """ocr_page returns a string (may be empty if tesseract not installed)."""
        import pymupdf as _pymupdf
        import subprocess
        from pdf_mcp.extractor import ocr_page

        try:
            subprocess.run(["tesseract", "--version"], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            pytest.skip("Tesseract not installed")
        doc = _pymupdf.open(sample_pdf)
        try:
            result = ocr_page(doc, 0, lang="eng", dpi=72)
        finally:
            doc.close()
        assert isinstance(result, str)


class TestGetParagraphForOffset:
    """Tests for get_paragraph_for_offset()."""

    def test_offset_in_first_block(self):
        """Offset 0 lands in the first block."""
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((50, 50), "First block text.")
        page.insert_text((50, 200), "Second block text.")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            doc.save(f.name)
            doc.close()
            doc2 = pymupdf.open(f.name)
            page2 = doc2[0]
            text, idx = get_paragraph_for_offset(page2, 0)
            assert text is not None
            assert "First" in text
            assert idx == 0
            doc2.close()
            os.unlink(f.name)

    def test_offset_in_second_block(self):
        """Offset past first block lands in the second block."""
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((50, 50), "AAA")
        page.insert_text((50, 200), "BBB")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            doc.save(f.name)
            doc.close()
            doc2 = pymupdf.open(f.name)
            page2 = doc2[0]
            full_text = page2.get_text("blocks", sort=True)
            text_blocks = [b[4] for b in full_text if b[6] == 0]
            joined = "\n\n".join(text_blocks)
            offset = joined.find("BBB")
            text, idx = get_paragraph_for_offset(page2, offset)
            assert text is not None
            assert "BBB" in text
            assert idx == 1
            doc2.close()
            os.unlink(f.name)

    def test_offset_beyond_text_returns_none(self):
        """Offset past all text returns (None, None)."""
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((50, 50), "Short.")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            doc.save(f.name)
            doc.close()
            doc2 = pymupdf.open(f.name)
            page2 = doc2[0]
            text, idx = get_paragraph_for_offset(page2, 99999)
            assert text is None
            assert idx is None
            doc2.close()
            os.unlink(f.name)

    def test_oversized_block_returns_none(self):
        """Block exceeding max_chars returns (None, None)."""
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((50, 50), "X" * 100)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            doc.save(f.name)
            doc.close()
            doc2 = pymupdf.open(f.name)
            page2 = doc2[0]
            text, idx = get_paragraph_for_offset(page2, 0, max_chars=10)
            assert text is None
            assert idx is None
            doc2.close()
            os.unlink(f.name)


class TestGetBestParagraphForQuery:
    """Tests for get_best_paragraph_for_query()."""

    def test_picks_block_with_most_token_overlap(self):
        """Selects the block containing the most query tokens."""
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((50, 50), "The cat sat on the mat.")
        page.insert_text((50, 200), "Dogs run fast in the park.")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            doc.save(f.name)
            doc.close()
            doc2 = pymupdf.open(f.name)
            page2 = doc2[0]
            text, idx = get_best_paragraph_for_query(page2, "cat mat")
            assert text is not None
            assert "cat" in text
            doc2.close()
            os.unlink(f.name)

    def test_no_overlap_returns_none(self):
        """No matching tokens returns (None, None)."""
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((50, 50), "The cat sat.")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            doc.save(f.name)
            doc.close()
            doc2 = pymupdf.open(f.name)
            page2 = doc2[0]
            text, idx = get_best_paragraph_for_query(page2, "xyz123")
            assert text is None
            assert idx is None
            doc2.close()
            os.unlink(f.name)

    def test_oversized_block_returns_none(self):
        """Best-matching block exceeding max_chars returns (None, None)."""
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((50, 50), "keyword " * 50)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            doc.save(f.name)
            doc.close()
            doc2 = pymupdf.open(f.name)
            page2 = doc2[0]
            text, idx = get_best_paragraph_for_query(page2, "keyword", max_chars=10)
            assert text is None
            assert idx is None
            doc2.close()
            os.unlink(f.name)

    def test_case_insensitive_matching(self):
        """Token matching is case-insensitive."""
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((50, 50), "Machine Learning is great.")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            doc.save(f.name)
            doc.close()
            doc2 = pymupdf.open(f.name)
            page2 = doc2[0]
            text, idx = get_best_paragraph_for_query(page2, "machine learning")
            assert text is not None
            assert "Machine" in text
            doc2.close()
            os.unlink(f.name)

    def test_min_chars_skips_short_blocks(self):
        """Blocks shorter than min_chars are skipped."""
        doc = pymupdf.open()
        page = doc.new_page()
        # Short heading block (< 80 chars)
        page.insert_text((50, 50), "Attention Mechanism")
        # Longer body block (> 80 chars)
        page.insert_text(
            (50, 200),
            (
                "The attention mechanism computes a weighted sum"
                " of values based on the compatibility function"
                " applied to each query-key pair in the sequence."
            ),
        )
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            doc.save(f.name)
            doc.close()
            doc2 = pymupdf.open(f.name)
            page2 = doc2[0]
            # Without min_chars: heading wins (both have "attention",
            # heading is first)
            text_no_floor, _ = get_best_paragraph_for_query(
                page2, "attention"
            )
            assert text_no_floor is not None
            # With min_chars=80: heading skipped, body wins
            text_with_floor, _ = get_best_paragraph_for_query(
                page2, "attention", min_chars=80
            )
            assert text_with_floor is not None
            assert len(text_with_floor) > 80
            assert "weighted sum" in text_with_floor.lower()
            doc2.close()
            os.unlink(f.name)


def test_detect_column_boxes_returns_list_for_page():
    import pymupdf
    from pdf_mcp.extractor import detect_column_boxes

    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((60, 100), "some body text on a page")
    assert isinstance(detect_column_boxes(page), list)
    doc.close()


def test_detect_column_boxes_falls_back_to_empty_on_error():
    from pdf_mcp.extractor import detect_column_boxes

    # A non-page object makes the underlying detector raise -> [].
    assert detect_column_boxes("not a page") == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
