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

from pdf_mcp import extractor
from pdf_mcp.cache import (
    _EXTRACTION_VERSION,
    PDFCache,
    _contains_cjk,
    normalize_ocr_lang,
)
from pdf_mcp.config import PDFConfig
from pdf_mcp.section_detector import Section
from pdf_mcp.docopen import open_pdf
from pdf_mcp.extractor import (
    count_query_tokens,
    estimate_tokens,
    extract_images_from_page,
    extract_metadata,
    extract_text_from_page,
    extract_toc,
    get_best_paragraph_for_query,
    get_paragraph_for_offset,
    parse_page_range,
    reorder_vertical_glyphs,
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
# Cache Helper Tests
# ============================================================================


class TestContainsCJK:
    def test_kanji_true(self):
        assert _contains_cjk("厚木基地") is True

    def test_hiragana_true(self):
        assert _contains_cjk("おわり") is True

    def test_katakana_true(self):
        assert _contains_cjk("カタカナ") is True

    def test_pure_kana_heading_true(self):
        assert _contains_cjk("終活") is True

    def test_hangul_true(self):
        assert _contains_cjk("한국어") is True

    def test_cjk_ext_a_true(self):
        assert _contains_cjk("㐀") is True  # U+3400

    def test_ascii_false(self):
        assert _contains_cjk("hello world") is False

    def test_digits_punct_false(self):
        assert _contains_cjk("123 - 456 (a.b)") is False

    def test_mixed_latin_cjk_true(self):
        assert _contains_cjk("base 基地") is True

    def test_empty_false(self):
        assert _contains_cjk("") is False


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

    def test_clear_all_empties_every_fts_table(self, cache, sample_pdf):
        if not cache.fts_available:
            pytest.skip("FTS5 unavailable on this SQLite build")
        cache.save_page_text(sample_pdf, 0, "quarterly budget report")
        cache.save_page_text(sample_pdf, 1, "厚木基地に関する報告書")
        cache.index_sections(
            sample_pdf,
            [
                Section(
                    title="Overview 概要",
                    start_page=1,
                    end_page=2,
                    text="budget 基地 details",
                    title_source="toc",
                )
            ],
        )

        cache.clear_all()

        with sqlite3.connect(cache.db_path) as conn:
            for table in (
                "pdf_search_fts",
                "pdf_search_fts_cjk",
                "pdf_section_fts",
                "pdf_section_fts_cjk",
            ):
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                assert count == 0, f"{table} still holds rows after clear_all"

    def test_clear_all_shrinks_db_file(self, cache, sample_pdf):
        """clear_all must return freed pages to the filesystem (VACUUM),
        so cache_size_bytes doesn't report megabytes of residual after a
        full clear."""
        filler = "corpus filler text for vacuum sizing " * 200
        for i in range(200):
            cache.save_page_text(sample_pdf, i, f"page {i} {filler}")
        pre = os.path.getsize(cache.db_path)

        cache.clear_all()

        post = os.path.getsize(cache.db_path)
        assert post < pre


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
        """RGBA format detected from a 4-channel decoded image."""
        fake = MagicMock()
        fake.n = 4
        fake.width = 10
        fake.height = 10
        fake.save = MagicMock(
            side_effect=lambda path: Path(path).write_bytes(b"\x89PNG")
        )
        with patch(
            "pdf_mcp.backend.raster.extract_images",
            return_value=[{"key": 1, "image": fake, "placements": []}],
        ):
            doc = open_pdf(sample_pdf_with_images)
            images = extract_images_from_page(
                doc, 0, output_dir=tmp_path, pdf_hash="test"
            )
            doc.close()

        assert images[0]["format"] == "rgba"

    def test_extract_images_unknown_format(self, sample_pdf_with_images, tmp_path):
        """Unknown format detected when the channel count is not 1, 3, or 4."""
        fake = MagicMock()
        fake.n = 2
        fake.width = 10
        fake.height = 10
        fake.save = MagicMock(
            side_effect=lambda path: Path(path).write_bytes(b"\x89PNG")
        )
        with patch(
            "pdf_mcp.backend.raster.extract_images",
            return_value=[{"key": 1, "image": fake, "placements": []}],
        ):
            doc = open_pdf(sample_pdf_with_images)
            images = extract_images_from_page(
                doc, 0, output_dir=tmp_path, pdf_hash="test"
            )
            doc.close()

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

    def test_save_page_text_persists_ocr_lang(self, cache, sample_pdf):
        """The OCR language is recorded alongside the text (issue #25)."""
        cache.save_page_text(sample_pdf, 0, "khmer text", source="ocr", ocr_lang="khm")
        assert cache.get_pages_ocr_lang(sample_pdf, [0]) == {0: "khm"}

    def test_ocr_lang_none_for_extracted_text(self, cache, sample_pdf):
        """Text with a real layer carries no language."""
        cache.save_page_text(sample_pdf, 0, "native text")
        assert cache.get_pages_ocr_lang(sample_pdf, [0]) == {0: None}

    def test_ocr_lang_none_for_legacy_ocr_row(self, cache, sample_pdf):
        """A pre-migration 'ocr' row has an unknown language, not a wrong one."""
        cache.save_page_text(sample_pdf, 0, "ocr text", source="ocr")
        assert cache.get_pages_ocr_lang(sample_pdf, [0]) == {0: None}

    def test_get_pages_ocr_lang_omits_uncached(self, cache, sample_pdf):
        """Uncached pages are omitted rather than reported as unknown."""
        cache.save_page_text(sample_pdf, 0, "ocr text", source="ocr", ocr_lang="eng")
        assert cache.get_pages_ocr_lang(sample_pdf, [0, 1]) == {0: "eng"}

    def test_bulk_extract_clears_ocr_lang(self, cache, sample_pdf):
        """Re-extracting a page drops the OCR language along with the 'ocr'
        source: the row is no longer OCR output, so no language describes it.

        The bulk writer relies on INSERT OR REPLACE defaults for this rather
        than clearing the column explicitly, so pin it.
        """
        cache.save_page_text(
            sample_pdf, 0, "khmer ocr text", source="ocr", ocr_lang="khm"
        )
        assert cache.get_pages_ocr_lang(sample_pdf, [0]) == {0: "khm"}

        cache.save_pages_text(sample_pdf, {0: "native text layer"})

        assert cache.get_page_source(sample_pdf, 0) == "extracted"
        assert cache.get_pages_ocr_lang(sample_pdf, [0]) == {0: None}

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

    @staticmethod
    def _two_block_page(first: str, second: str):
        """A page with two real paragraph blocks (not one block per line)."""
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(50, 50, 520, 170), first, fontsize=10)
        page.insert_textbox(pymupdf.Rect(50, 220, 520, 340), second, fontsize=10)
        handle = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        doc.save(handle.name)
        doc.close()
        return handle.name

    def test_quantitative_query_prefers_a_tied_block_that_has_the_figures(self):
        """On a tie, the block carrying the numbers wins.

        Measured defect: of 20 single-document 10-K questions whose answer
        was on a retrieved page but missing from the excerpt, the gold
        block already TIED the winning score in 10. It lost only because
        `score > best_score` keeps whichever block came first in document
        order. For a question asking "how much", a tied block containing
        no figures cannot be the better answer.
        """
        query = "What total gross margin percentage during 2024?"
        prose = (
            "Total gross margin percentage during 2024 declined relative to "
            "prior periods, reflecting a different product mix."
        )
        figures = (
            "Total gross margin percentage during 2024: 46.2% versus 44.1% "
            "and 43.3% for the two preceding periods."
        )
        path = self._two_block_page(prose, figures)
        try:
            doc = pymupdf.open(path)
            blocks = [b[4] for b in doc[0].get_text("blocks", sort=True) if b[6] == 0]
            assert len(blocks) == 2, blocks
            # Precondition: the blocks must genuinely TIE, or this test
            # would pass on the old code and prove nothing. Three earlier
            # drafts of this fixture did exactly that.
            scores = [count_query_tokens(b, query) for b in blocks]
            assert scores[0] == scores[1], f"fixture does not tie: {scores}"

            text, _idx = get_best_paragraph_for_query(doc[0], query)
            assert text is not None
            assert "46.2%" in text, f"picked the figure-less block: {text!r}"
            doc.close()
        finally:
            os.unlink(path)

    def test_non_quantitative_query_keeps_document_order_on_a_tie(self):
        """The figure preference must not fire on non-numeric questions.

        Otherwise any page mixing prose with a table would start returning
        the table for definition and risk questions.
        """
        first = (
            "The Company faces risks relating to cybersecurity incidents "
            "that could disrupt its operations and harm its reputation."
        )
        second = (
            "Cybersecurity spending totalled 1,234 in 2024 and 5,678 in "
            "2023 across the segments listed above in this filing."
        )
        path = self._two_block_page(first, second)
        try:
            doc = pymupdf.open(path)
            text, _idx = get_best_paragraph_for_query(
                doc[0], "What cybersecurity risks does the Company disclose?"
            )
            assert text is not None
            assert "risks relating to cybersecurity" in text
            doc.close()
        finally:
            os.unlink(path)

    def test_hyphenated_page_text_matches_unhyphenated_query_token(self):
        """'pretraining' must match a block that spells it 'pre-training'.

        Measured defect (described-13, 2026-07-28): the caller's query
        'pretraining speedup' scored the span-bearing abstract at 0
        because substring matching is hyphen-blind, so the picker could
        not choose it under any tie-break.
        """
        first = (
            "Figure 1 shows loss curves for the two model variants "
            "described in the sections that follow this one."
        )
        second = (
            "Pre-training large language models is prohibitively "
            "expensive and the total wall-clock budget keeps growing."
        )
        path = self._two_block_page(first, second)
        try:
            doc = pymupdf.open(path)
            text, _idx = get_best_paragraph_for_query(doc[0], "pretraining")
            assert text is not None, "hyphen-blind matching scored 0 everywhere"
            assert "Pre-training" in text
            doc.close()
        finally:
            os.unlink(path)

    def test_count_query_tokens_folds_hyphens(self):
        """count_query_tokens must share the picker's hyphen folding, or
        the short-block retry comparison in the server would disagree
        with the picker about coverage."""
        assert count_query_tokens("Pre-training is costly.", "pretraining") == 1

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
            text_no_floor, _ = get_best_paragraph_for_query(page2, "attention")
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


def test_extraction_version_bump_drops_text_and_derived(tmp_path):
    import sqlite3
    from pdf_mcp.cache import PDFCache, _EXTRACTION_VERSION

    cache = PDFCache(cache_dir=tmp_path)
    db = cache.db_path
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO page_text "
            "(file_path, page_num, file_mtime, text, text_length) "
            "VALUES (?, ?, ?, ?, ?)",
            ("/x.pdf", 1, 0.0, "old interleaved text", 20),
        )
        conn.execute("PRAGMA user_version = 0")  # simulate pre-upgrade cache
        conn.commit()

    PDFCache(cache_dir=tmp_path)  # re-init triggers the migration

    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM page_text").fetchone()[0]
        (version,) = conn.execute("PRAGMA user_version").fetchone()
    assert rows == 0
    assert version == _EXTRACTION_VERSION


def test_extract_text_is_column_major_when_two_columns(monkeypatch):
    import pymupdf
    from pdf_mcp import extractor

    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    for i, y in enumerate((100, 130, 160)):
        page.insert_text((60, y), f"leftrow{i}")
        page.insert_text((360, y), f"rightrow{i}")

    # Mock the pre-gate to return False (ambiguous layout) so the detector runs.
    # Force a two-column split: left half, then right half.
    monkeypatch.setattr(extractor, "is_confidently_single_column", lambda b: False)
    monkeypatch.setattr(
        extractor,
        "detect_column_boxes",
        lambda p: [pymupdf.Rect(0, 0, 300, 800), pymupdf.Rect(300, 0, 600, 800)],
    )
    out = extractor.extract_text_from_page(page)
    doc.close()

    # Column-major: the whole left column precedes the right column.
    assert out.index("leftrow2") < out.index("rightrow0")


def test_extract_text_unchanged_when_single_column(monkeypatch):
    import pymupdf
    from pdf_mcp import extractor

    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((60, 100), "only one column of text here")
    page.insert_text((60, 130), "second line of the column")

    monkeypatch.setattr(extractor, "detect_column_boxes", lambda p: [])
    out = extractor.extract_text_from_page(page)

    expected = "\n\n".join(
        b[4] for b in page.get_text("blocks", sort=True) if b[6] == 0
    )
    doc.close()
    assert out == expected


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


def test_extract_text_skips_empty_columns(monkeypatch):
    import pymupdf
    from pdf_mcp import extractor

    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((60, 100), "left column has text")
    # right half intentionally blank
    monkeypatch.setattr(
        extractor,
        "detect_column_boxes",
        lambda p: [pymupdf.Rect(0, 0, 300, 800), pymupdf.Rect(300, 0, 600, 800)],
    )
    out = extractor.extract_text_from_page(page)
    doc.close()
    assert out == "left column has text"
    assert "\n\n" not in out


def test_is_multi_column_layout_rejects_short_grid():
    """A sparse grid of short cells above a full-width body is NOT multi-column.

    Mirrors an academic title page (e.g. the Transformer paper) whose
    author/affiliation block is laid out in a visual grid: the column detector
    over-segments it into short side-by-side cells alongside one tall full-width
    body box. Reading those column-by-column scrambles the row-major order.
    """
    import pymupdf
    from pdf_mcp.extractor import _is_multi_column_layout

    # One tall full-width body box (h=408) + short author-grid cells (h~31).
    boxes = [pymupdf.Rect(108, 334, 504, 742)]
    for x0, x1 in ((116, 216), (230, 309), (323, 407)):
        boxes.append(pymupdf.Rect(x0, 235, x1, 266))
    assert _is_multi_column_layout(boxes) is False


def test_is_multi_column_layout_accepts_tall_columns():
    import pymupdf
    from pdf_mcp.extractor import _is_multi_column_layout

    boxes = [pymupdf.Rect(0, 0, 300, 800), pymupdf.Rect(300, 0, 600, 800)]
    assert _is_multi_column_layout(boxes) is True


def test_is_multi_column_layout_single_or_empty():
    import pymupdf
    from pdf_mcp.extractor import _is_multi_column_layout

    assert _is_multi_column_layout([]) is False
    assert _is_multi_column_layout([pymupdf.Rect(0, 0, 300, 800)]) is False


def test_is_multi_column_layout_accepts_up_to_ceiling():
    """Genuine multi-column (2..MAX) stays True — academic 2-col, dense ~3-4."""
    import pymupdf
    from pdf_mcp.extractor import _MAX_COLUMNS, _is_multi_column_layout

    # MAX tall, full-height boxes -> still a real (if dense) column layout.
    boxes = [pymupdf.Rect(i * 10, 0, i * 10 + 5, 800) for i in range(_MAX_COLUMNS)]
    assert _is_multi_column_layout(boxes) is True


def test_is_multi_column_layout_rejects_over_segmented():
    """Degenerate over-segmentation (e.g. Sodegaura p4: 74 tall boxes) -> False.

    The detector shatters some vertical/mixed pages into dozens of tall slivers;
    clipping each would produce glyph-soup + duplication. More 'columns' than any
    real layout has => treat as degenerate and fall back to positional sort.
    """
    import pymupdf
    from pdf_mcp.extractor import _MAX_COLUMNS, _is_multi_column_layout

    over = [pymupdf.Rect(i * 5, 0, i * 5 + 4, 800) for i in range(_MAX_COLUMNS + 1)]
    assert _is_multi_column_layout(over) is False
    soup = [pymupdf.Rect(i * 5, 0, i * 5 + 4, 800) for i in range(74)]
    assert _is_multi_column_layout(soup) is False


def test_author_grid_title_page_reads_row_major(monkeypatch):
    """Regression: a multi-author title-page grid extracts in visual row order.

    Without grid suppression the column detector's boxes drive a column-major
    read (down each column), placing the second-row author before later
    first-row authors. The fix routes such a page through positional sort, which
    preserves row order. Asserts a last-first-row name precedes a first
    second-row name — the signature that distinguishes row- from column-major.
    """
    import pymupdf
    from pdf_mcp import extractor

    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    # 3-column x 2-row author grid near the top.
    cols = (60, 230, 400)
    row0 = ("Alpha", "Bravo", "Charlie")
    row1 = ("Delta", "Echo", "Foxtrot")
    for x, name in zip(cols, row0):
        page.insert_text((x, 110), name, fontsize=11)
    for x, name in zip(cols, row1):
        page.insert_text((x, 150), name, fontsize=11)
    # Full-width body paragraph below the grid.
    page.insert_text((50, 400), "Body paragraph spanning the full page width.")

    # Detector boxes mimic the real over-segmentation, ordered column-major so
    # the unguarded path would interleave the grid wrongly: each author a short
    # cell, plus one tall full-width body box.
    cells = []
    for x in cols:
        cells.append(pymupdf.Rect(x - 5, 100, x + 80, 122))  # row0 cell
        cells.append(pymupdf.Rect(x - 5, 140, x + 80, 162))  # row1 cell
    body = pymupdf.Rect(40, 380, 560, 720)
    monkeypatch.setattr(extractor, "detect_column_boxes", lambda p: cells + [body])

    out = extractor.extract_text_from_page(page)
    doc.close()

    # Row-major: the whole first row precedes the second row.
    assert out.index("Charlie") < out.index("Delta")
    assert out.index("Alpha") < out.index("Bravo") < out.index("Charlie")


class TestPageWorkers:
    def _one_page_pdf(self, tmp_path):
        path = str(tmp_path / "render_worker.pdf")
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((50, 50), "Render worker page.")
        doc.save(path)
        doc.close()
        return path

    def test_render_worker_returns_info(self, tmp_path):
        from pdf_mcp.extractor import _render_page_worker

        path = self._one_page_pdf(tmp_path)
        out_dir = tmp_path / "renders"
        out_dir.mkdir()
        page_num, info = _render_page_worker((path, 0, str(out_dir), "abc123", 72))
        assert page_num == 0
        assert isinstance(info, dict)
        assert Path(info["file_path_on_disk"]).exists()
        assert info["size_bytes"] > 0

    def test_render_worker_isolates_bad_page(self, tmp_path):
        from pdf_mcp.extractor import _render_page_worker
        from pdf_mcp.parallel import PageError

        path = self._one_page_pdf(tmp_path)
        out_dir = tmp_path / "renders"
        out_dir.mkdir()
        # Page 99 does not exist -> worker returns a PageError, does not raise.
        page_num, result = _render_page_worker((path, 99, str(out_dir), "abc", 72))
        assert page_num == 99
        assert isinstance(result, PageError)

    def test_ocr_worker_isolates_bad_path(self, tmp_path):
        from pdf_mcp.extractor import _ocr_page_worker
        from pdf_mcp.parallel import PageError

        # Nonexistent file -> pymupdf.open raises -> worker returns PageError.
        page_num, result = _ocr_page_worker(
            (str(tmp_path / "missing.pdf"), 0, "eng", 300, None)
        )
        assert page_num == 0
        assert isinstance(result, PageError)

    def test_render_worker_runs_through_real_pool(self, tmp_path):
        # Picklability + spawn-safety: workers must survive a real pool.
        from pdf_mcp.extractor import _render_page_worker
        from pdf_mcp.parallel import run_pages

        path = self._one_page_pdf(tmp_path)
        out_dir = tmp_path / "renders"
        out_dir.mkdir()
        args = [(path, 0, str(out_dir), "abc", 72)]
        results = run_pages(_render_page_worker, args, max_workers=2)
        assert results[0][0] == 0
        assert isinstance(results[0][1], dict)


def _fake_mode_dict(lines):
    # lines: list of (dir_tuple, n_chars). detect_writing_mode reads each
    # line's "dir" and counts characters from span text length. Spans carry a
    # CJK filler glyph so the CJK pre-gate passes and the dir histogram (not the
    # short-circuit) is what these tests exercise.
    return {
        "blocks": [
            {"lines": [{"dir": d, "spans": [{"text": "あ" * n}]} for d, n in lines]}
        ]
    }


class _FakeModePage:
    """Page double exposing both get_text('text') (for the CJK gate) and
    get_text('dict') (for the dir histogram)."""

    def __init__(self, data):
        self._data = data
        self._text = "".join(
            span["text"]
            for block in data["blocks"]
            for line in block["lines"]
            for span in line["spans"]
        )

    def get_text(self, kind):
        if kind == "text":
            return self._text
        assert kind == "dict"
        return self._data


def test_detect_writing_mode_vertical():
    from pdf_mcp.extractor import detect_writing_mode

    page = _FakeModePage(_fake_mode_dict([((0.0, -1.0), 100)]))
    assert detect_writing_mode(page) == "vertical"


def test_detect_writing_mode_horizontal():
    from pdf_mcp.extractor import detect_writing_mode

    page = _FakeModePage(_fake_mode_dict([((1.0, 0.0), 100)]))
    assert detect_writing_mode(page) == "horizontal"


def test_detect_writing_mode_mixed():
    from pdf_mcp.extractor import detect_writing_mode

    # 60% vertical -> between 0.50 and 0.80 -> mixed
    page = _FakeModePage(_fake_mode_dict([((0.0, -1.0), 60), ((1.0, 0.0), 40)]))
    assert detect_writing_mode(page) == "mixed"


def test_detect_writing_mode_below_min_chars_is_horizontal():
    from pdf_mcp.extractor import detect_writing_mode

    page = _FakeModePage(_fake_mode_dict([((0.0, -1.0), 10)]))  # < _MIN_CHARS
    assert detect_writing_mode(page) == "horizontal"


def test_detect_writing_mode_horizontal_dominant_mixed_still_routes():
    """A horizontal-dominant page with a substantial vertical region (30%, >=30
    vertical chars) is 'mixed' -> reaches the reorder (gate lowered to 0.20)."""
    from pdf_mcp.extractor import detect_writing_mode

    page = _FakeModePage(_fake_mode_dict([((0.0, -1.0), 60), ((1.0, 0.0), 140)]))
    assert detect_writing_mode(page) == "mixed"


def test_detect_writing_mode_non_cjk_skips_dict_parse():
    """A page with no CJK characters is horizontal without paying for the
    expensive dict parse (vertical/tategaki layout is a CJK phenomenon)."""
    from pdf_mcp.extractor import detect_writing_mode

    class _NoDictPage:
        def get_text(self, kind):
            if kind == "text":
                return "The quick brown fox jumps over the lazy dog. " * 50
            raise AssertionError("dict parse must be skipped for non-CJK pages")

    assert detect_writing_mode(_NoDictPage()) == "horizontal"


def _fake_dict(lines):
    # lines: list of (text, dir, bbox)
    return {
        "blocks": [
            {
                "lines": [
                    {"dir": d, "bbox": b, "spans": [{"text": t}]} for t, d, b in lines
                ]
            }
        ]
    }


class _FakeDictPage:
    def __init__(self, data):
        self._data = data

    def get_text(self, kind):
        assert kind == "dict"
        return self._data


def test_collect_glyphs_tags_orientation_and_skips_blank():
    from pdf_mcp.extractor import _collect_glyphs

    page = _FakeDictPage(
        _fake_dict(
            [
                ("あ", (0.0, -1.0), (10, 0, 20, 12)),  # vertical glyph
                ("the", (1.0, 0.0), (0, 50, 40, 62)),  # horizontal line
                ("   ", (1.0, 0.0), (0, 80, 5, 92)),  # blank -> skipped
            ]
        )
    )
    gs = _collect_glyphs(page)
    assert len(gs) == 2
    assert gs[0] == {
        "text": "あ",
        "x0": 10,
        "y0": 0,
        "x1": 20,
        "y1": 12,
        "vertical": True,
    }
    assert gs[1]["text"] == "the" and gs[1]["vertical"] is False


def _vglyph(x, y0, h=10):
    return {
        "text": "x",
        "x0": x,
        "y0": y0,
        "x1": x + 8,
        "y1": y0 + h,
        "vertical": True,
    }


def test_valley_tiers_single_band_no_split():
    from pdf_mcp.extractor import _valley_tiers

    # one dense band near the top, nothing else -> no interior valley
    gs = [_vglyph(x, y) for x in range(0, 80, 8) for y in range(0, 100, 10)]
    assert _valley_tiers(gs, page_height=800, unit=10) == []


def test_valley_tiers_two_bands_one_boundary():
    from pdf_mcp.extractor import _valley_tiers

    # two dense bands with an empty gap between ~300 and ~500
    top = [_vglyph(x, y) for x in range(0, 80, 8) for y in range(40, 300, 10)]
    bot = [_vglyph(x, y) for x in range(0, 80, 8) for y in range(500, 760, 10)]
    bounds = _valley_tiers(top + bot, page_height=800, unit=10)
    assert len(bounds) == 1
    assert 300 < bounds[0] < 520


def test_reorder_two_columns_right_to_left():
    from pdf_mcp.extractor import reorder_vertical_glyphs

    # left column x=10 reads "あい", right column x=40 reads "うえ"
    # vertical reading order is right-to-left -> "うえ" then "あい"
    gs = [
        {"text": "あ", "x0": 10, "y0": 0, "x1": 18, "y1": 10, "vertical": True},
        {"text": "い", "x0": 10, "y0": 12, "x1": 18, "y1": 22, "vertical": True},
        {"text": "う", "x0": 40, "y0": 0, "x1": 48, "y1": 10, "vertical": True},
        {"text": "え", "x0": 40, "y0": 12, "x1": 48, "y1": 22, "vertical": True},
    ]
    assert reorder_vertical_glyphs(gs, page_height=800) == "うえあい"


def test_reorder_two_tiers_top_then_bottom():
    from pdf_mcp.extractor import reorder_vertical_glyphs

    # top tier (y~40-260) and bottom tier (y~520-740), each one column
    top = [
        {
            "text": "上",
            "x0": 20,
            "y0": 40 + i * 10,
            "x1": 28,
            "y1": 50 + i * 10,
            "vertical": True,
        }
        for i in range(20)
    ]
    bot = [
        {
            "text": "下",
            "x0": 20,
            "y0": 520 + i * 10,
            "x1": 28,
            "y1": 530 + i * 10,
            "vertical": True,
        }
        for i in range(20)
    ]
    out = reorder_vertical_glyphs(top + bot, page_height=800)
    assert out.replace("\n", "").startswith("上")
    assert out.index("上") < out.index("下")  # top tier before bottom tier


def test_reorder_no_vertical_falls_back_to_horizontal_positional():
    from pdf_mcp.extractor import reorder_vertical_glyphs

    gs = [
        {"text": "second", "x0": 0, "y0": 50, "x1": 60, "y1": 62, "vertical": False},
        {"text": "first", "x0": 0, "y0": 10, "x1": 60, "y1": 22, "vertical": False},
    ]
    out = reorder_vertical_glyphs(gs, page_height=800)
    assert out.index("first") < out.index("second")  # top-to-bottom


def test_reorder_mixed_orders_regions_by_position():
    from pdf_mcp.extractor import reorder_vertical_glyphs

    # vertical interview at top, horizontal directory line at bottom
    vtop = [
        {
            "text": "縦",
            "x0": 20,
            "y0": 40 + i * 10,
            "x1": 28,
            "y1": 50 + i * 10,
            "vertical": True,
        }
        for i in range(20)
    ]
    hbot = [
        {
            "text": "directory",
            "x0": 0,
            "y0": 600,
            "x1": 90,
            "y1": 612,
            "vertical": False,
        }
    ]
    out = reorder_vertical_glyphs(vtop + hbot, page_height=800)
    assert out.index("縦") < out.index("directory")


def test_extract_routes_horizontal_to_existing_path(monkeypatch):
    """A horizontal page must NOT touch the reorder path (Latin unchanged)."""
    from pdf_mcp import extractor

    class _Page:
        rect = type("R", (), {"height": 800.0})()

        def get_text(self, kind, **kw):
            if kind == "blocks":
                return [(0, 0, 10, 10, "hello world", 0, 0)]
            return ""

    monkeypatch.setattr(extractor, "detect_writing_mode", lambda p: "horizontal")
    monkeypatch.setattr(extractor, "detect_column_boxes", lambda p: [])
    out = extractor.extract_text_from_page(_Page())
    assert out == "hello world"


def test_extract_routes_vertical_to_reorder(monkeypatch):
    from pdf_mcp import extractor

    class _Page:
        rect = type("R", (), {"height": 800.0})()

        def get_text(self, kind, **kw):
            return {"blocks": []}  # _collect_glyphs sees nothing

    monkeypatch.setattr(extractor, "detect_writing_mode", lambda p: "vertical")
    called = {}
    monkeypatch.setattr(
        extractor,
        "reorder_vertical",
        lambda p: called.update(hit=True) or "REORDERED",
    )
    out = extractor.extract_text_from_page(_Page())
    assert out == "REORDERED" and called.get("hit")


def _zero_height_vglyph(text, x0, y0, x1):
    return {"text": text, "x0": x0, "y0": y0, "x1": x1, "y1": y0, "vertical": True}


def test_reorder_vertical_glyphs_zero_height_no_crash():
    """Degenerate zero-height glyphs / zero page_height must not ZeroDivisionError."""
    # All glyphs have zero height -> median unit == 0.
    glyphs = [
        _zero_height_vglyph("天", 100.0, 50.0, 110.0),
        _zero_height_vglyph("地", 100.0, 60.0, 110.0),
        _zero_height_vglyph("人", 80.0, 50.0, 90.0),
    ]

    # Both a positive and a non-positive page_height exercise the guards.
    out = reorder_vertical_glyphs(glyphs, page_height=800.0)
    assert isinstance(out, str)
    for g in glyphs:
        assert g["text"] in out

    out_zero = reorder_vertical_glyphs(glyphs, page_height=0.0)
    assert isinstance(out_zero, str)
    for g in glyphs:
        assert g["text"] in out_zero


def test_strip_mojibake_removes_indic_keeps_japanese_and_latin():
    from pdf_mcp.extractor import _strip_mojibake

    # mojibake = Bengali/Tamil/Odia (broken-font garbage); keep CJK, kana, ASCII
    assert _strip_mojibake("人ୈ権තදtext") == "人権text"
    assert _strip_mojibake("こんにちは") == "こんにちは"  # kana untouched
    assert _strip_mojibake("ABC123") == "ABC123"  # ASCII untouched


def test_strip_mojibake_keeps_cjk_extension_a():
    from pdf_mcp.extractor import _strip_mojibake

    # rare kanji in CJK Ext-A (0x3400-0x4DBF) are legitimate, must NOT be dropped
    assert _strip_mojibake("㐀䶿人") == "㐀䶿人"


def test_page_rules_finds_horizontal_and_vertical_rules():
    import pymupdf
    from pdf_mcp.extractor import _page_rules

    class _DrawPage:
        rect = pymupdf.Rect(0, 0, 600, 800)

        def get_drawings(self):
            return [
                {"rect": pymupdf.Rect(40, 300, 560, 302), "type": "s"},  # h-rule
                {"rect": pymupdf.Rect(300, 60, 302, 590), "type": "s"},  # v-rule
                {"rect": pymupdf.Rect(10, 10, 30, 30), "type": "f"},  # tiny: neither
            ]

    h, v = _page_rules(_DrawPage())
    assert h == [300.0]
    assert len(v) == 1 and round(v[0][0]) == 300


def test_page_rules_degrades_to_empty_on_drawing_error():
    from pdf_mcp.extractor import _page_rules

    class _BadDrawPage:
        rect = type("R", (), {"width": 600.0, "height": 800.0})()

        def get_drawings(self):
            return [{"type": "s"}]  # malformed: missing "rect" -> must not crash

    assert _page_rules(_BadDrawPage()) == ([], [])


def _hglyph(x, y, t="x"):
    return {"text": t, "x0": x, "y0": y, "x1": x + 8, "y1": y + 10, "vertical": True}


def test_segment_by_rules_vertical_rule_orders_right_then_left():
    from pdf_mcp.extractor import _segment_by_rules

    # left column x=50, right column x=400; vertical rule at x=250 splits them.
    # vertical reading order is right-to-left -> right region first.
    left = [_hglyph(50, y, "L") for y in range(40, 200, 12)]
    right = [_hglyph(400, y, "R") for y in range(40, 200, 12)]
    regions = _segment_by_rules(left + right, [], [(250.0, 0.0, 800.0)], 600, 800)
    assert len(regions) == 2
    assert regions[0][0]["text"] == "R" and regions[1][0]["text"] == "L"


def test_segment_by_rules_horizontal_rule_orders_top_then_bottom():
    from pdf_mcp.extractor import _segment_by_rules

    top = [_hglyph(100, y, "T") for y in range(40, 200, 12)]
    bot = [_hglyph(100, y, "B") for y in range(400, 560, 12)]
    regions = _segment_by_rules(top + bot, [300.0], [], 600, 800)
    assert len(regions) == 2
    assert regions[0][0]["text"] == "T" and regions[1][0]["text"] == "B"


def test_segment_by_rules_merges_close_rules_no_glyph_loss():
    from pdf_mcp.extractor import _segment_by_rules

    # a cluster of rules <20pt apart (a table) must NOT shatter or drop glyphs
    glyphs = [_hglyph(100, y, "x") for y in range(40, 560, 12)]
    close_rules = [200.0, 205.0, 210.0, 215.0, 400.0]  # 4 within 20pt
    regions = _segment_by_rules(glyphs, close_rules, [], 600, 800)
    kept = sum(len(r) for r in regions)
    assert kept == len(glyphs)  # no glyph dropped
    assert len(regions) <= 3  # close rules merged, not 6 strips


def test_reorder_vertical_no_rules_uses_single_region(monkeypatch):
    from pdf_mcp import extractor

    class _Page:
        rect = type("R", (), {"width": 600.0, "height": 800.0})()

        def get_text(self, kind):
            return {"blocks": []}

    monkeypatch.setattr(extractor, "_page_rules", lambda p: ([], []))
    calls = {}
    monkeypatch.setattr(
        extractor,
        "reorder_vertical_glyphs",
        lambda g, h: calls.setdefault("n", 0)
        or calls.update(n=calls.get("n", 0) + 1)
        or "SINGLE",
    )
    assert extractor.reorder_vertical(_Page()) == "SINGLE"


def test_reorder_vertical_horizontal_rules_only_does_not_segment(monkeypatch):
    """Only a VERTICAL rule triggers segmentation; horizontal rules alone fall
    through to the whole-page reorder (valley-tier handles horizontal tiering,
    and banding on decorative h-rules scrambles content that flows across them).
    """
    from pdf_mcp import extractor

    class _Page:
        rect = type("R", (), {"width": 600.0, "height": 800.0})()

        def get_text(self, kind):
            return {"blocks": []}

    # h-rules present, NO vertical rule -> must NOT segment
    monkeypatch.setattr(extractor, "_page_rules", lambda p: ([300.0, 500.0], []))
    monkeypatch.setattr(extractor, "reorder_vertical_glyphs", lambda g, h: "SINGLE")

    def _boom(*a, **k):
        raise AssertionError("_segment_by_rules called for h-rules-only page")

    monkeypatch.setattr(extractor, "_segment_by_rules", _boom)
    assert extractor.reorder_vertical(_Page()) == "SINGLE"


def test_reorder_vertical_strips_mojibake_before_reorder(monkeypatch):
    from pdf_mcp import extractor

    class _Page:
        rect = type("R", (), {"width": 600.0, "height": 800.0})()

        def get_text(self, kind):
            return {
                "blocks": [
                    {
                        "lines": [
                            {
                                "dir": (0.0, -1.0),
                                "bbox": (10, 10, 20, 22),
                                "spans": [{"text": "人ୈ権"}],
                            }
                        ]
                    }
                ]
            }

    monkeypatch.setattr(extractor, "_page_rules", lambda p: ([], []))
    captured = {}
    monkeypatch.setattr(
        extractor,
        "reorder_vertical_glyphs",
        lambda g, h: captured.update(text=g[0]["text"]) or "",
    )
    extractor.reorder_vertical(_Page())
    assert captured["text"] == "人権"  # mojibake glyph stripped


class TestRenderPageClip:
    def _doc(self):
        doc = pymupdf.open()
        doc.new_page(width=600, height=800)
        return doc

    def test_clip_produces_smaller_pixmap(self, tmp_path):
        doc = self._doc()
        page = doc[0]
        r = page.rect
        rect = pymupdf.Rect(r.x0, r.y0, r.x0 + r.width * 0.5, r.y0 + r.height * 0.5)
        full = extractor.render_page_as_png(doc, 0, tmp_path, "hash", dpi=72)
        crop = extractor.render_page_as_png(doc, 0, tmp_path, "hash", dpi=72, clip=rect)
        assert crop["width"] < full["width"]
        assert crop["height"] < full["height"]
        doc.close()

    def test_clip_filename_distinct_from_full(self, tmp_path):
        doc = self._doc()
        page = doc[0]
        r = page.rect
        rect = pymupdf.Rect(r.x0, r.y0, r.x0 + r.width * 0.5, r.y0 + r.height * 0.5)
        full = extractor.render_page_as_png(doc, 0, tmp_path, "hash", dpi=72)
        crop = extractor.render_page_as_png(doc, 0, tmp_path, "hash", dpi=72, clip=rect)
        assert full["file_path_on_disk"] != crop["file_path_on_disk"]
        assert "clip" in crop["file_path_on_disk"]
        doc.close()


class TestRenderPageAsImage:
    """Codec-aware rendering (render_page_as_image)."""

    def test_png_path_unchanged(self, sample_pdf, tmp_path):
        import pymupdf
        from pdf_mcp.extractor import render_page_as_image

        doc = pymupdf.open(sample_pdf)
        try:
            info = render_page_as_image(doc, 0, tmp_path, "abc", dpi=72)
        finally:
            doc.close()

        assert info["file_path_on_disk"].endswith("abc_p0_render_72dpi.png")
        assert info["codec"] == "png"
        assert info["quality"] == 0
        assert Path(info["file_path_on_disk"]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    def test_jpeg_codec_writes_jpeg_with_quality_in_name(self, sample_pdf, tmp_path):
        import pymupdf
        from pdf_mcp.extractor import render_page_as_image

        doc = pymupdf.open(sample_pdf)
        try:
            info = render_page_as_image(
                doc, 0, tmp_path, "abc", dpi=72, codec="jpeg", quality=60
            )
        finally:
            doc.close()

        assert info["file_path_on_disk"].endswith("abc_p0_render_72dpi_q60.jpg")
        assert info["codec"] == "jpeg"
        assert info["quality"] == 60
        # JPEG SOI marker
        assert Path(info["file_path_on_disk"]).read_bytes()[:2] == b"\xff\xd8"

    def test_png_and_jpeg_never_collide_on_disk(self, sample_pdf, tmp_path):
        import pymupdf
        from pdf_mcp.extractor import render_page_as_image

        doc = pymupdf.open(sample_pdf)
        try:
            png = render_page_as_image(doc, 0, tmp_path, "abc", dpi=72)
            jpg = render_page_as_image(
                doc, 0, tmp_path, "abc", dpi=72, codec="jpeg", quality=80
            )
            q60 = render_page_as_image(
                doc, 0, tmp_path, "abc", dpi=72, codec="jpeg", quality=60
            )
        finally:
            doc.close()

        paths = {
            png["file_path_on_disk"],
            jpg["file_path_on_disk"],
            q60["file_path_on_disk"],
        }
        assert len(paths) == 3

    def test_render_page_as_png_wrapper_still_works(self, sample_pdf, tmp_path):
        import pymupdf
        from pdf_mcp.extractor import render_page_as_png

        doc = pymupdf.open(sample_pdf)
        try:
            info = render_page_as_png(doc, 0, tmp_path, "abc", dpi=72)
        finally:
            doc.close()

        assert info["codec"] == "png"
        assert info["file_path_on_disk"].endswith(".png")

    def test_unknown_codec_raises(self, sample_pdf, tmp_path):
        import pymupdf
        from pdf_mcp.extractor import render_page_as_image

        doc = pymupdf.open(sample_pdf)
        try:
            with pytest.raises(ValueError, match="codec"):
                render_page_as_image(doc, 0, tmp_path, "abc", dpi=72, codec="webp")
        finally:
            doc.close()


class TestCJKHelpers:
    def test_contains_cjk_true_for_kanji_kana_hangul(self):
        from pdf_mcp.cache import _contains_cjk

        assert _contains_cjk("厚木基地")
        assert _contains_cjk("終活")
        assert _contains_cjk("한국")
        assert _contains_cjk("カタカナ")

    def test_contains_cjk_true_for_fullwidth_and_compat(self):
        from pdf_mcp.cache import _contains_cjk

        assert _contains_cjk("１２３")  # fullwidth digits 0xFF10-19
        assert _contains_cjk("豈")  # compatibility ideograph

    def test_contains_cjk_false_for_latin_and_empty(self):
        from pdf_mcp.cache import _contains_cjk

        assert not _contains_cjk("hello world 2024")
        assert not _contains_cjk("")

    def test_cjk_split_spaces_each_cjk_char_keeps_latin_whole(self):
        from pdf_mcp.cache import _cjk_split

        assert _cjk_split("厚木基地をめぐる") == "厚 木 基 地 を め ぐ る"
        assert _cjk_split("2024年") == "2024 年"
        assert _cjk_split("PDF形式") == "PDF 形 式"
        assert _cjk_split("令和6年度") == "令 和 6 年 度"

    def test_cjk_split_idempotent_on_spaced_input(self):
        from pdf_mcp.cache import _cjk_split

        assert _cjk_split("終 活") == "終 活"

    def test_cjk_split_pure_latin_unchanged(self):
        from pdf_mcp.cache import _cjk_split

        assert _cjk_split("machine learning") == "machine learning"


def test_cjk_fts_tables_created(tmp_path):
    from pdf_mcp.cache import PDFCache

    cache = PDFCache(cache_dir=tmp_path)
    with sqlite3.connect(cache.db_path) as conn:
        names = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "pdf_search_fts_cjk" in names
    assert "pdf_section_fts_cjk" in names


def test_save_page_text_populates_cjk_table(tmp_path):
    from pdf_mcp.cache import PDFCache, _cjk_split

    cache = PDFCache(cache_dir=tmp_path)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")  # path must exist for mtime
    cache.save_page_text(str(pdf), 0, "厚木基地をめぐる活動")
    cache.save_page_text(str(pdf), 1, "English only page")

    with sqlite3.connect(cache.db_path) as conn:
        cjk_rows = conn.execute(
            "SELECT page_num, text FROM pdf_search_fts_cjk"
        ).fetchall()
    # CJK page stored split; English page absent from CJK table
    assert len(cjk_rows) == 1
    assert cjk_rows[0][0] == 0
    assert cjk_rows[0][1] == _cjk_split("厚木基地をめぐる活動")


def test_escape_fts5_query_cjk_builds_phrases():
    from pdf_mcp.cache import _escape_fts5_query_cjk

    assert _escape_fts5_query_cjk("厚木基地") == '"厚 木 基 地"'
    assert _escape_fts5_query_cjk("2024年") == '"2024 年"'
    # multiple whitespace tokens AND-joined
    assert _escape_fts5_query_cjk("終活 健康") == '"終 活" "健 康"'


def test_cjk_keyword_search_finds_embedded_term(tmp_path):
    from pdf_mcp.cache import PDFCache

    cache = PDFCache(cache_dir=tmp_path)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")
    # embedded (not whitespace-delimited) — the porter path returns 0 here
    cache.save_page_text(str(pdf), 0, "本紙は厚木基地をめぐる課題を扱う。")
    results = cache.search_fts(str(pdf), "厚木基地", max_results=10, context_chars=80)
    assert [r["page"] for r in results] == [1]
    # excerpt is original text, NOT space-mangled split text
    assert "厚 木 基 地" not in results[0]["excerpt"]
    assert "厚木基地" in results[0]["excerpt"]


def test_cjk_keyword_search_two_char_term(tmp_path):
    from pdf_mcp.cache import PDFCache

    cache = PDFCache(cache_dir=tmp_path)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")
    cache.save_page_text(str(pdf), 0, "今月の特集は終活についてです。")
    results = cache.search_fts(str(pdf), "終活", max_results=10, context_chars=80)
    assert [r["page"] for r in results] == [1]


def test_cjk_multi_token_search_finds_separate_sentences(tmp_path):
    """Multi-term CJK keyword query: each term appears in a SEPARATE
    sentence (not adjacent). Per-token contiguity should still accept the
    page and center the excerpt on the earliest-occurring token."""
    from pdf_mcp.cache import PDFCache

    cache = PDFCache(cache_dir=tmp_path)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")
    cache.save_page_text(
        str(pdf), 0, "東京の天気は晴れです。大阪の交通情報を伝えます。"
    )
    results = cache.search_fts(str(pdf), "東京 大阪", max_results=10, context_chars=80)
    assert [r["page"] for r in results] == [1]
    assert results[0]["excerpt"] is not None
    assert "東京" in results[0]["excerpt"]


def test_cjk_single_token_search_excerpt_unchanged(tmp_path):
    """Single-token CJK query behavior is byte-identical to the pre-fix
    code path (benchmark-pinned: 厚木基地 recall)."""
    from pdf_mcp.cache import PDFCache

    cache = PDFCache(cache_dir=tmp_path)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")
    cache.save_page_text(str(pdf), 0, "本紙は厚木基地をめぐる課題を扱う。")
    results = cache.search_fts(str(pdf), "厚木基地", max_results=10, context_chars=80)
    assert [r["page"] for r in results] == [1]
    text = "本紙は厚木基地をめぐる課題を扱う。"
    idx = text.find("厚木基地")
    half = max(0, (80 - len("厚木基地")) // 2)
    start = max(0, idx - half)
    end = min(len(text), idx + len("厚木基地") + half)
    expected = text[start:end]
    assert results[0]["excerpt"] == expected


def test_cjk_single_token_cross_separator_false_positive_dropped(tmp_path):
    """A token whose characters are not contiguous in the original page
    text (only whitespace between them, collapsed away by _cjk_split) is
    caught by the per-token contiguity post-filter, even though FTS itself
    marks it as a match. This is the false-positive protection the filter
    exists for."""
    from pdf_mcp.cache import PDFCache

    cache = PDFCache(cache_dir=tmp_path)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")
    # "大" ends one block, "阪" starts an unrelated block; only whitespace
    # separates them in the original text, so FTS (which collapses
    # whitespace via _cjk_split) treats them as adjacent, but the literal
    # substring "大阪" never appears in the original page text.
    cache.save_page_text(str(pdf), 0, "大\n\n阪")
    results = cache.search_fts(str(pdf), "大阪", max_results=10, context_chars=80)
    assert results == []


def test_cjk_multi_token_missing_token_returns_no_results(tmp_path):
    """Multi-token query where one token's characters are present (so FTS
    AND-matches) but never contiguous in the original text: the per-token
    post-filter still drops the whole hit, even though the other token
    ("大阪") is genuinely contiguous."""
    from pdf_mcp.cache import PDFCache

    cache = PDFCache(cache_dir=tmp_path)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")
    # "大阪" is a real contiguous match; "東" and "京" are only
    # whitespace-separated (no other characters between them), so FTS
    # (which collapses whitespace via _cjk_split) matches "東京" as a
    # phrase, but the literal substring "東京" never appears.
    cache.save_page_text(str(pdf), 0, "大阪の話です。東\n\n京の天気も伝えます。")
    results = cache.search_fts(str(pdf), "東京 大阪", max_results=10, context_chars=80)
    assert results == []


def test_english_search_unchanged_by_cjk_path(tmp_path):
    from pdf_mcp.cache import PDFCache

    cache = PDFCache(cache_dir=tmp_path)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")
    cache.save_page_text(str(pdf), 0, "machine learning models")
    # Porter stemming still works: query 'model' matches 'models'
    results = cache.search_fts(str(pdf), "model", max_results=10, context_chars=80)
    assert [r["page"] for r in results] == [1]


def test_migration_backfills_cjk_from_existing_page_text(tmp_path):
    from pdf_mcp.cache import PDFCache, _cjk_split

    cache = PDFCache(cache_dir=tmp_path)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")
    cache.save_page_text(str(pdf), 0, "厚木基地の記事")

    # Simulate a pre-feature cache: drop the CJK tables, keep page_text.
    with sqlite3.connect(cache.db_path) as conn:
        conn.execute("DROP TABLE pdf_search_fts_cjk")
        conn.execute("DROP TABLE pdf_section_fts_cjk")
        pt_before = conn.execute("SELECT COUNT(*) FROM page_text").fetchone()[0]

    # Re-open: migration should rebuild the CJK table from page_text.
    cache2 = PDFCache(cache_dir=tmp_path)
    with sqlite3.connect(cache2.db_path) as conn:
        rows = conn.execute(
            "SELECT text FROM pdf_search_fts_cjk WHERE file_path = ?", (str(pdf),)
        ).fetchall()
        pt_after = conn.execute("SELECT COUNT(*) FROM page_text").fetchone()[0]
    assert rows == [(_cjk_split("厚木基地の記事"),)]
    assert pt_after == pt_before  # page_text preserved, no re-extraction


def _cols(db_path, table):
    with sqlite3.connect(db_path) as conn:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_migration_adds_content_trust_columns(tmp_path):
    cache = PDFCache(cache_dir=tmp_path)
    assert "content_trust_json" in _cols(cache.db_path, "pdf_metadata")
    assert "has_hidden_text" in _cols(cache.db_path, "page_text")
    # Global version table must exist (replaces per-row trust_version).
    assert _cols(cache.db_path, "content_trust_meta") != set()


def test_migration_adds_ocr_lang_to_pre_existing_db(tmp_path):
    """A database whose page_text predates ocr_lang gains the column on open,
    and its existing OCR rows report an unknown language (issue #25)."""
    cache = PDFCache(cache_dir=tmp_path)
    db = cache.db_path

    # Rebuild page_text as it looked before the column existed, carrying one
    # legacy OCR row.
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE page_text")
        conn.execute(
            "CREATE TABLE page_text (file_path TEXT NOT NULL,"
            " page_num INTEGER NOT NULL, file_mtime REAL NOT NULL,"
            " text TEXT NOT NULL, text_length INTEGER NOT NULL,"
            " source TEXT DEFAULT 'extracted',"
            " PRIMARY KEY (file_path, page_num))"
        )
        conn.execute(
            "INSERT INTO page_text (file_path, page_num, file_mtime, text,"
            " text_length, source) VALUES ('p.pdf', 0, 1.0, 'old ocr', 7, 'ocr')"
        )

    PDFCache(cache_dir=tmp_path)  # re-open triggers the migration

    assert "ocr_lang" in _cols(db, "page_text")
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT text, source, ocr_lang FROM page_text WHERE file_path = 'p.pdf'"
        ).fetchone()
    # Text and source are preserved. The language is stored as '' rather than
    # NULL because ocr_lang joined the primary key (issue #27) and SQLite
    # permits NULL there, which would break uniqueness. What #25 actually
    # guaranteed is asserted below: an unknown language is still unknown, and
    # still forces a re-OCR rather than being served for any language asked.
    assert row == ("old ocr", "ocr", "")

    # ('' -> None at the API boundary is covered by TestLanguageAwareReads;
    # this fixture's p.pdf does not exist on disk, so mtime validation drops
    # the row before it can be read back here.)
    from pdf_mcp.server import _is_ocr_cache_hit

    assert not _is_ocr_cache_hit(
        "ocr", {0: "old ocr"}, 0, requested_lang="khm", cached_lang=None
    )
    assert not _is_ocr_cache_hit(
        "ocr", {0: "old ocr"}, 0, requested_lang="eng", cached_lang=None
    )


def test_trust_version_invalidation_nulls_caches(tmp_path, monkeypatch):
    """Global content_trust_meta version triggers cache wipe on upgrade."""
    from pdf_mcp import content_trust

    # First open: stamps content_trust_meta with the current version.
    cache = PDFCache(cache_dir=tmp_path)
    db = cache.db_path

    # Seed a metadata row and a page row with stale content-trust data.
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO pdf_metadata (file_path, file_mtime, file_size,"
            " page_count, content_trust_json)"
            " VALUES ('p.pdf', 1.0, 10, 1, '{\"suspicious\": true}')"
        )
        conn.execute(
            "INSERT INTO page_text (file_path, page_num, file_mtime, text,"
            " text_length, has_hidden_text) VALUES ('p.pdf', 0, 1.0, 'x', 1, 1)"
        )
        # Backdate the global stamp so the next open sees stored < current.
        conn.execute("UPDATE content_trust_meta SET trust_version = 0 WHERE id = 0")

    monkeypatch.setattr(content_trust, "_TRUST_VERSION", 99)
    PDFCache(cache_dir=tmp_path)  # reopen triggers global invalidation

    with sqlite3.connect(db) as conn:
        ct = conn.execute(
            "SELECT content_trust_json FROM pdf_metadata WHERE file_path='p.pdf'"
        ).fetchone()[0]
        hh = conn.execute(
            "SELECT has_hidden_text FROM page_text WHERE file_path='p.pdf'"
        ).fetchone()[0]
        meta_ver = conn.execute(
            "SELECT trust_version FROM content_trust_meta WHERE id = 0"
        ).fetchone()[0]

    assert ct is None
    assert hh is None
    assert meta_ver == 99


def test_clear_expired_keeps_freshly_accessed_entry(tmp_path):
    """clear_expired must not purge an entry accessed 'now'.

    Regression: the cutoff was datetime.now().isoformat() — local timezone,
    'T' date/time separator — string-compared against accessed_at stored via
    SQLite CURRENT_TIMESTAMP (UTC, space separator). On a host ahead of UTC
    the space-vs-'T' ordering (0x20 < 0x54) made same-date fresh rows sort as
    expired, deleting them on startup. Fixed by computing the cutoff with
    SQLite's own clock so both sides are UTC with identical formatting.
    """
    cache = PDFCache(cache_dir=tmp_path)  # default ttl_hours = 24
    with sqlite3.connect(cache.db_path) as conn:
        # accessed_at defaults to CURRENT_TIMESTAMP — 'now' in the DB clock.
        conn.execute(
            "INSERT INTO pdf_metadata (file_path, file_mtime, file_size,"
            " page_count) VALUES ('fresh.pdf', 1.0, 10, 1)"
        )

    cleared = cache.clear_expired()

    with sqlite3.connect(cache.db_path) as conn:
        row = conn.execute(
            "SELECT file_path FROM pdf_metadata WHERE file_path = 'fresh.pdf'"
        ).fetchone()
    assert row is not None, "a just-accessed entry must not be expired"
    assert cleared == 0


def _seed_meta_and_page(cache, path="d.pdf"):
    import os

    # Create a real file so _is_cache_valid passes (mtime comparison).
    p = os.path.join(os.path.dirname(cache.db_path), "d.pdf")
    with open(p, "wb") as fh:
        fh.write(b"%PDF-1.4\n%%EOF\n")
    cache.save_metadata(p, page_count=1, metadata={}, toc=[])
    cache.save_page_text(p, 0, "hello world")
    return p


def test_save_and_get_content_trust(tmp_path):
    cache = PDFCache(cache_dir=tmp_path)
    p = _seed_meta_and_page(cache)
    assert cache.get_content_trust(p) is None
    cache.save_content_trust(p, {"suspicious": True, "hidden_text_runs": 2})
    got = cache.get_content_trust(p)
    assert got["suspicious"] is True
    # Saving content-trust must not clobber existing metadata.
    assert cache.get_metadata(p)["page_count"] == 1


def test_per_page_hidden_flag_roundtrip(tmp_path):
    cache = PDFCache(cache_dir=tmp_path)
    p = _seed_meta_and_page(cache)
    # Not computed yet => None
    assert cache.get_pages_hidden_flag(p, [0]) == {0: None}
    cache.save_pages_hidden_flag(p, {0: True})
    assert cache.get_pages_hidden_flag(p, [0]) == {0: True}


def test_get_metadata_exposes_content_trust(tmp_path):
    cache = PDFCache(cache_dir=tmp_path)
    p = _seed_meta_and_page(cache)
    cache.save_content_trust(p, {"suspicious": False})
    assert cache.get_metadata(p)["content_trust"] == {"suspicious": False}


def test_render_write_is_atomic_no_tmp_residue(tmp_path):
    """render_page_as_png writes via a temp file + atomic replace, leaving
    no *.tmp residue and a valid PNG (regression guard for the torn-file
    race when an orphaned pool worker writes the same deterministic path)."""
    import pymupdf
    from pathlib import Path
    from pdf_mcp.extractor import render_page_as_png

    doc = pymupdf.open()
    doc.new_page(width=200, height=200)
    try:
        result = render_page_as_png(doc, 0, tmp_path, "hashabc", dpi=72)
    finally:
        doc.close()

    out = Path(result["file_path_on_disk"])
    assert out.exists() and out.stat().st_size > 0
    assert out.read_bytes().startswith(b"\x89PNG")
    # No leftover temp files from the atomic write.
    assert list(tmp_path.glob("*.tmp")) == []


def test_multicolumn_dedup_overlapping_boxes(monkeypatch):
    """Overlapping column boxes must NOT duplicate a block's text.

    The old clip path extracted the overlap region under each box, so a
    block in the overlap appeared twice. The rawdict assembly assigns each
    block to exactly one box.
    """
    import pymupdf
    from pdf_mcp import extractor

    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((60, 100), "leftonly alpha")  # box0 only
    page.insert_text((250, 100), "sharedmiddle")  # fully inside the overlap
    page.insert_text((520, 100), "rightonly delta")  # box1 only

    monkeypatch.setattr(extractor, "is_confidently_single_column", lambda b: False)
    # Two boxes overlapping on x=200..400. "sharedmiddle" at x=250 sits fully
    # inside BOTH, so the old clip path extracted it under each box (twice).
    monkeypatch.setattr(
        extractor,
        "detect_column_boxes",
        lambda p: [pymupdf.Rect(0, 0, 400, 800), pymupdf.Rect(200, 0, 600, 800)],
    )
    out = extractor.extract_text_from_page(page)
    doc.close()

    assert out.count("sharedmiddle") == 1  # assigned to exactly one box
    assert out.count("leftonly alpha") == 1
    assert out.count("rightonly delta") == 1


def test_multicolumn_reading_order_column_major(monkeypatch):
    """Whole left column precedes the right column (reading order)."""
    import pymupdf
    from pdf_mcp import extractor

    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    for i, y in enumerate((100, 130, 160)):
        page.insert_text((60, y), f"leftrow{i}")
        page.insert_text((360, y), f"rightrow{i}")

    monkeypatch.setattr(extractor, "is_confidently_single_column", lambda b: False)
    monkeypatch.setattr(
        extractor,
        "detect_column_boxes",
        lambda p: [pymupdf.Rect(0, 0, 300, 800), pymupdf.Rect(300, 0, 600, 800)],
    )
    out = extractor.extract_text_from_page(page)
    doc.close()

    assert out.index("leftrow2") < out.index("rightrow0")


def test_multicolumn_helper_is_deterministic(monkeypatch):
    """The helper returns identical output across repeated calls."""
    import pymupdf
    from pdf_mcp import extractor

    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    for i, y in enumerate((100, 130, 160, 190)):
        page.insert_text((60, y), f"leftrow{i} text")
        page.insert_text((360, y), f"rightrow{i} text")
    boxes = [pymupdf.Rect(0, 0, 300, 800), pymupdf.Rect(300, 0, 600, 800)]
    outs = {extractor._assemble_columns_from_rawdict(page, boxes) for _ in range(15)}
    doc.close()
    assert len(outs) == 1


def test_multicolumn_falls_back_on_helper_error(monkeypatch):
    """If the assembly helper raises, extraction degrades to positional sort."""
    import pymupdf
    from pdf_mcp import extractor

    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((60, 100), "some body text on the page")

    monkeypatch.setattr(extractor, "is_confidently_single_column", lambda b: False)
    monkeypatch.setattr(
        extractor,
        "detect_column_boxes",
        lambda p: [pymupdf.Rect(0, 0, 300, 800), pymupdf.Rect(300, 0, 600, 800)],
    )
    monkeypatch.setattr(
        extractor,
        "_assemble_columns_from_rawdict",
        lambda p, b: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    out = extractor.extract_text_from_page(page)
    expected = "\n\n".join(
        b[4] for b in page.get_text("blocks", sort=True) if b[6] == 0
    )
    doc.close()
    assert out == expected  # positional-sort fallback, not an exception


class TestMergeRowFragments:
    """Pure-helper tests: (text, bbox, baseline) fragments -> merged text."""

    def _frag(self, text, x0, x1, baseline, y0=100.0, y1=110.0):
        return (text, (x0, y0, x1, y1), baseline)

    def test_same_baseline_small_gap_joins_without_space(self):
        from pdf_mcp.extractor import _merge_row_fragments

        frags = [
            self._frag("Slo", 228.2, 245.6, 110.0),
            self._frag("wl", 245.6, 258.0, 110.0),
            self._frag("y", 258.0, 264.0, 110.0),
        ]
        assert _merge_row_fragments(frags) == "Slowly"

    def test_same_baseline_word_gap_joins_with_space(self):
        from pdf_mcp.extractor import _merge_row_fragments

        frags = [
            self._frag("Slowly", 228.2, 264.0, 110.0),
            self._frag("growing", 267.6, 310.0, 110.0),  # gap 3.6
        ]
        assert _merge_row_fragments(frags) == "Slowly growing"

    def test_negative_gap_kerning_joins_without_space(self):
        from pdf_mcp.extractor import _merge_row_fragments

        frags = [
            self._frag("gr", 270.0, 280.0, 110.0),
            self._frag("o", 279.2, 285.0, 110.0),  # gap -0.8
        ]
        assert _merge_row_fragments(frags) == "gro"

    def test_different_baselines_stay_separate_lines(self):
        from pdf_mcp.extractor import _merge_row_fragments

        frags = [
            self._frag("body text line", 60.0, 200.0, 110.0),
            self._frag("2", 200.5, 205.0, 105.0, y0=96.0, y1=104.0),  # superscript
            self._frag("next line", 60.0, 150.0, 122.0, y0=112.0, y1=122.0),
        ]
        out = _merge_row_fragments(frags)
        assert out.splitlines() == ["body text line", "2", "next line"]

    def test_rows_ordered_by_baseline_fragments_by_x(self):
        from pdf_mcp.extractor import _merge_row_fragments

        frags = [
            self._frag("second", 60.0, 100.0, 122.0, y0=112.0, y1=122.0),
            self._frag("world", 120.0, 160.0, 110.0),
            self._frag("hello", 60.0, 100.0, 110.0),
        ]
        assert _merge_row_fragments(frags) == "hello world\nsecond"

    def test_single_fragment_passthrough(self):
        from pdf_mcp.extractor import _merge_row_fragments

        assert _merge_row_fragments([self._frag("only", 0, 10, 50)]) == "only"

    def test_empty_returns_empty(self):
        from pdf_mcp.extractor import _merge_row_fragments

        assert _merge_row_fragments([]) == ""

    def test_large_negative_gap_deep_overlap_joins_with_space(self):
        # height=10 (default y0=100,y1=110) -> threshold = max(1.0, 0.25*10)
        # = 2.5. gap = 204.0 - 210.0 = -6.0, |gap|=6.0 well above threshold.
        # A one-sided `gap > threshold` rule (any negative gap joins
        # directly) would glue this into "Tisub"; the shipped rule requires
        # |gap| > threshold, so a deep overlap still gets a space.
        from pdf_mcp.extractor import _merge_row_fragments

        frags = [
            self._frag("Ti", 200.0, 210.0, 110.0),
            self._frag("sub", 204.0, 220.0, 110.0),
        ]
        assert _merge_row_fragments(frags) == "Ti sub"

    def test_gap_between_quarter_and_third_height_joins_with_space(self):
        # height=10 -> 0.25*h=2.5, 0.3*h=3.0. gap=2.7 sits strictly between
        # them: the shipped 0.25 multiplier requires a space (2.7 > 2.5),
        # but a 0.3 multiplier would not (2.7 < 3.0), so this fails if the
        # multiplier reverts to 0.3.
        from pdf_mcp.extractor import _merge_row_fragments

        frags = [
            self._frag("al", 200.0, 210.0, 110.0),
            self._frag("pha", 212.7, 225.0, 110.0),
        ]
        assert _merge_row_fragments(frags) == "al pha"

    def test_gap_just_under_quarter_height_joins_without_space(self):
        # height=10 -> 0.25*h=2.5. gap=2.3 < 2.5, so no space: the mirror
        # of the previous test, pinning the same 0.25 multiplier from the
        # other side.
        from pdf_mcp.extractor import _merge_row_fragments

        frags = [
            self._frag("al", 200.0, 210.0, 110.0),
            self._frag("pha", 212.3, 225.0, 110.0),
        ]
        assert _merge_row_fragments(frags) == "alpha"


def test_multicolumn_letterspaced_heading_not_fragmented():
    """Regression: a small-caps/letter-spaced heading split by rawdict into
    same-row fragments must reconstruct contiguously, not newline-joined.
    Skips if the local corpus is absent."""
    import pymupdf
    import pytest as _pytest
    from pathlib import Path
    from pdf_mcp import extractor

    pdf = (
        Path(__file__).parent.parent
        / "benchmark_data"
        / ".reading_order_pdfs"
        / "0706.0954.pdf"
    )
    if not pdf.exists():
        _pytest.skip("local reading-order corpus not present")

    doc = pymupdf.open(str(pdf))
    text = extractor.extract_text_from_page(doc[10])  # page 11
    doc.close()
    assert "Slowly growing diffeomorphisms" in text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class TestFTS5OrFallback:
    """An AND-joined query needs every token on the same page, so a
    natural-language question returns nothing when one word is absent.
    Falling back to OR keeps question-shaped queries usable."""

    def test_or_fallback_expression_joins_tokens_with_or(self):
        from pdf_mcp.cache import _fts5_or_fallback

        assert _fts5_or_fallback("revenue decline 2024") == (
            '"revenue" OR "decline" OR "2024"'
        )

    def test_or_fallback_is_none_for_a_single_token(self):
        from pdf_mcp.cache import _fts5_or_fallback

        # One token: the AND form already is the OR form, so there is
        # nothing to retry and the caller must not run a second query.
        assert _fts5_or_fallback("revenue") is None

    def test_or_fallback_is_none_for_a_two_token_query(self):
        """Two terms is a deliberate conjunction ("pgvector unicorn"), and
        AND's precision guarantee is the point of it. Only longer,
        question-shaped queries -- where some words are incidental
        connective tissue -- earn the fallback."""
        from pdf_mcp.cache import _fts5_or_fallback

        assert _fts5_or_fallback("pgvector unicorn") is None

    def test_or_fallback_is_none_when_no_tokens_survive(self):
        from pdf_mcp.cache import _fts5_or_fallback

        assert _fts5_or_fallback("   ***   ") is None

    def test_question_shaped_query_finds_the_page(self, cache, sample_pdf):
        """The real bug: every AND token must appear, so one absent word
        ('decline' vs the page's 'decreased') zeroed the whole query."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        cache.save_page_text(
            sample_pdf, 24, "Greater China net sales decreased during 2024"
        )
        cache.save_page_text(sample_pdf, 30, "Unrelated liquidity discussion")

        results = cache.search_fts(
            sample_pdf,
            "Greater China net sales decline in 2024",
            max_results=10,
            context_chars=100,
        )

        assert results, "question-shaped query returned nothing"
        assert results[0]["page"] == 25

    def test_and_match_still_wins_when_all_tokens_present(self, cache, sample_pdf):
        """The fallback must not change ranking for queries that already
        match: a page carrying every token outranks a page carrying one."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        cache.save_page_text(sample_pdf, 0, "alpha beta gamma together on one page")
        cache.save_page_text(sample_pdf, 1, "alpha only here")

        results = cache.search_fts(
            sample_pdf, "alpha beta gamma", max_results=10, context_chars=100
        )

        assert len(results) == 1, "AND matched, so the fallback must not fire"
        assert results[0]["page"] == 1

    def test_page_match_counts_agree_with_returned_matches(self, cache, sample_pdf):
        """Invariant: pages in `matches` must also appear in the per-page
        counts, so the fallback has to apply to both paths."""
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        cache.save_page_text(
            sample_pdf, 24, "Greater China net sales decreased during 2024"
        )

        query = "Greater China net sales decline in 2024"
        results = cache.search_fts(sample_pdf, query, max_results=10, context_chars=100)
        counts = cache.get_fts_page_counts(sample_pdf, query)

        assert results, "precondition: search returned matches"
        for match in results:
            assert (match["page"] - 1) in counts, (
                f"page {match['page']} returned by search_fts but missing"
                " from get_fts_page_counts"
            )


class TestFTS5OrFallbackIsOptional:
    """Corpus search must not relax each document independently: a document
    that lacks the terms contributing nothing is what lets the one document
    holding a real match win. The fallback is therefore opt-out."""

    def test_fallback_can_be_disabled(self, cache, sample_pdf):
        if not cache.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        cache.save_page_text(
            sample_pdf, 24, "Greater China net sales decreased during 2024"
        )
        query = "Greater China net sales decline in 2024"

        assert cache.search_fts(
            sample_pdf, query, max_results=10, context_chars=100
        ), "precondition: the fallback finds this page by default"

        assert (
            cache.search_fts(
                sample_pdf,
                query,
                max_results=10,
                context_chars=100,
                allow_or_fallback=False,
            )
            == []
        ), "with the fallback disabled, strict AND semantics apply"


class TestNativeRenderDpiCap:
    def test_image_only_page_reports_native_dpi(self, scanned_page_pdf):
        import pymupdf
        from pdf_mcp.extractor import native_render_dpi_cap

        doc = pymupdf.open(scanned_page_pdf)
        try:
            assert native_render_dpi_cap(doc, 0) == 144
        finally:
            doc.close()

    def test_text_page_has_no_cap(self, sample_pdf):
        import pymupdf
        from pdf_mcp.extractor import native_render_dpi_cap

        doc = pymupdf.open(sample_pdf)
        try:
            assert native_render_dpi_cap(doc, 0) is None
        finally:
            doc.close()

    def test_text_over_raster_has_no_cap(self, scanned_page_pdf, tmp_path):
        """A raster with text on top must NOT be capped: the text renders
        sharper above the image's native resolution."""
        import pymupdf
        from pdf_mcp.extractor import native_render_dpi_cap

        doc = pymupdf.open(scanned_page_pdf)
        try:
            doc[0].insert_text((50, 50), "caption text")
            annotated = tmp_path / "annotated.pdf"
            doc.save(annotated)
        finally:
            doc.close()

        doc2 = pymupdf.open(annotated)
        try:
            assert native_render_dpi_cap(doc2, 0) is None
        finally:
            doc2.close()

    def test_partial_coverage_has_no_cap(self, tmp_path):
        """An image covering half the page is a figure, not a scan."""
        import pymupdf
        from pdf_mcp.extractor import native_render_dpi_cap

        src = pymupdf.Document()
        page = src.new_page(width=450, height=600)
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 900, 600))
        pix.set_rect(pix.irect, (200, 200, 200))
        page.insert_image(pymupdf.Rect(0, 0, 450, 300), pixmap=pix)
        out = tmp_path / "figure.pdf"
        src.save(out)
        src.close()

        doc = pymupdf.open(out)
        try:
            assert native_render_dpi_cap(doc, 0) is None
        finally:
            doc.close()


class TestRenderCacheCodecKey:
    def test_jpeg_render_is_not_served_for_a_png_request(self, tmp_path):
        from pdf_mcp.cache import PDFCache

        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        jpg = tmp_path / "r.jpg"
        jpg.write_bytes(b"\xff\xd8jpegbytes")
        cache = PDFCache(cache_dir=tmp_path)
        mtime = pdf.stat().st_mtime

        cache.save_page_render(
            str(pdf),
            0,
            mtime,
            200,
            {
                "file_path_on_disk": str(jpg),
                "size_bytes": jpg.stat().st_size,
                "width": 100,
                "height": 200,
                "codec": "jpeg",
                "quality": 60,
            },
        )

        assert cache.get_page_render(str(pdf), 0, 200) is None
        hit = cache.get_page_render(str(pdf), 0, 200, codec="jpeg", quality=60)
        assert hit is not None
        assert hit["codec"] == "jpeg"
        assert hit["quality"] == 60

    def test_two_jpeg_qualities_coexist(self, tmp_path):
        from pdf_mcp.cache import PDFCache

        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        cache = PDFCache(cache_dir=tmp_path)
        mtime = pdf.stat().st_mtime

        for q in (80, 60):
            f = tmp_path / f"r{q}.jpg"
            f.write_bytes(b"\xff\xd8" + bytes([q]) * 10)
            cache.save_page_render(
                str(pdf),
                0,
                mtime,
                200,
                {
                    "file_path_on_disk": str(f),
                    "size_bytes": f.stat().st_size,
                    "width": 100,
                    "height": 200,
                    "codec": "jpeg",
                    "quality": q,
                },
            )

        q80 = cache.get_page_render(str(pdf), 0, 200, codec="jpeg", quality=80)
        q60 = cache.get_page_render(str(pdf), 0, 200, codec="jpeg", quality=60)
        assert q80 is not None and q60 is not None
        assert q80["file_path_on_disk"] != q60["file_path_on_disk"]

    def test_png_defaults_are_backward_compatible(self, tmp_path):
        """A save without codec/quality keys stores a PNG row readable by a
        default get_page_render call."""
        from pdf_mcp.cache import PDFCache

        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        png = tmp_path / "r.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n")
        cache = PDFCache(cache_dir=tmp_path)

        cache.save_page_render(
            str(pdf),
            0,
            pdf.stat().st_mtime,
            200,
            {
                "file_path_on_disk": str(png),
                "size_bytes": png.stat().st_size,
                "width": 10,
                "height": 20,
            },
        )

        hit = cache.get_page_render(str(pdf), 0, 200)
        assert hit is not None
        assert hit["codec"] == "png"
        assert hit["quality"] == 0

    def test_legacy_table_is_dropped_and_its_files_unlinked(self, tmp_path):
        """A pre-existing page_renders table without codec/quality cannot be
        ALTERed into the new primary key, so it is dropped. Its PNGs must be
        unlinked first or they leak in the renders dir forever."""
        import sqlite3

        from pdf_mcp.cache import PDFCache

        db = tmp_path / "cache.db"
        orphan = tmp_path / "old.png"
        orphan.write_bytes(b"\x89PNG\r\n\x1a\n")
        with sqlite3.connect(db) as conn:
            conn.execute("""CREATE TABLE page_renders (
                       file_path TEXT NOT NULL,
                       page_num INTEGER NOT NULL,
                       file_mtime REAL NOT NULL,
                       dpi INTEGER NOT NULL,
                       file_path_on_disk TEXT NOT NULL,
                       size_bytes INTEGER NOT NULL,
                       width INTEGER NOT NULL,
                       height INTEGER NOT NULL,
                       PRIMARY KEY (file_path, page_num, dpi))""")
            conn.execute(
                "INSERT INTO page_renders VALUES (?,?,?,?,?,?,?,?)",
                ("/x.pdf", 0, 1.0, 200, str(orphan), 8, 10, 20),
            )

        PDFCache(cache_dir=tmp_path)

        assert not orphan.exists()
        with sqlite3.connect(db) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(page_renders)")}
        assert {"codec", "quality"}.issubset(cols)


class TestNormalizeOcrLang:
    """Case and whitespace must not create separate cache rows; language
    ORDER must, because Tesseract's output depends on it (issue #27)."""

    def test_lowercases(self):
        assert normalize_ocr_lang("KHM") == "khm"

    def test_strips_whitespace(self):
        assert normalize_ocr_lang("  khm+eng  ") == "khm+eng"

    def test_none_becomes_sentinel(self):
        assert normalize_ocr_lang(None) == ""

    def test_whitespace_only_becomes_sentinel(self):
        assert normalize_ocr_lang("   ") == ""

    def test_does_not_reorder(self):
        assert normalize_ocr_lang("KHM+ENG") != normalize_ocr_lang("eng+khm")
        assert normalize_ocr_lang("KHM+ENG") == normalize_ocr_lang("khm+eng")


def _make_old_shape_db(db_path):
    """A cache.db as it exists before the widening: 2-column PK, nullable
    ocr_lang, one extracted row and one OCR row."""
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE page_text (
            file_path TEXT NOT NULL,
            page_num INTEGER NOT NULL,
            file_mtime REAL NOT NULL,
            text TEXT NOT NULL,
            text_length INTEGER NOT NULL,
            source TEXT DEFAULT 'extracted',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            has_hidden_text INTEGER DEFAULT NULL,
            ocr_lang TEXT DEFAULT NULL,
            PRIMARY KEY (file_path, page_num)
        );
        """)
    conn.executemany(
        "INSERT INTO page_text (file_path, page_num, file_mtime, text,"
        " text_length, source, ocr_lang) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("/a.pdf", 0, 1.0, "extracted text", 14, "extracted", None),
            ("/a.pdf", 1, 1.0, "ocr text", 8, "ocr", "khm+eng"),
        ],
    )
    # A real cache carries the current extraction version. Without this the
    # _EXTRACTION_VERSION check drops page_text before the PK migration runs,
    # and the test would be exercising the drop path instead.
    conn.execute(f"PRAGMA user_version = {_EXTRACTION_VERSION}")
    conn.commit()
    conn.close()


class TestPageTextPrimaryKeyMigration:
    def test_migration_preserves_rows_and_backfills_sentinel(self, tmp_path):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        db_path = cache_dir / "cache.db"
        _make_old_shape_db(str(db_path))

        PDFCache(cache_dir=cache_dir)

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT page_num, text, source, ocr_lang FROM page_text ORDER BY page_num"
        ).fetchall()
        nulls = conn.execute(
            "SELECT COUNT(*) FROM page_text WHERE ocr_lang IS NULL"
        ).fetchone()[0]
        conn.close()

        assert rows == [
            (0, "extracted text", "extracted", ""),
            (1, "ocr text", "ocr", "khm+eng"),
        ]
        assert nulls == 0

    def test_new_pk_lets_two_languages_coexist(self, tmp_path):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        PDFCache(cache_dir=cache_dir)

        conn = sqlite3.connect(str(cache_dir / "cache.db"))
        conn.executemany(
            "INSERT INTO page_text (file_path, page_num, file_mtime, text,"
            " text_length, source, ocr_lang) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("/a.pdf", 0, 1.0, "kh first", 8, "ocr", "khm+eng"),
                ("/a.pdf", 0, 1.0, "en first", 8, "ocr", "eng+khm"),
            ],
        )
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM page_text WHERE file_path = '/a.pdf'"
        ).fetchone()[0]
        conn.close()

        assert count == 2

    def test_migration_is_idempotent(self, tmp_path):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        db_path = cache_dir / "cache.db"
        _make_old_shape_db(str(db_path))

        PDFCache(cache_dir=cache_dir)
        PDFCache(cache_dir=cache_dir)

        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM page_text").fetchone()[0]
        conn.close()
        assert count == 2


class TestSavePageTextNormalizesLang:
    """ocr_lang is normalized into the cache key: case and whitespace share a
    row, distinct orderings do not (issue #27)."""

    def _langs(self, cache, path):
        with sqlite3.connect(cache.db_path) as conn:
            return conn.execute(
                "SELECT ocr_lang, text FROM page_text WHERE file_path = ?"
                " ORDER BY ocr_lang",
                (path,),
            ).fetchall()

    def test_case_variants_share_one_row(self, cache, sample_pdf):
        cache.save_page_text(sample_pdf, 0, "first", source="ocr", ocr_lang="KHM+ENG")
        cache.save_page_text(sample_pdf, 0, "second", source="ocr", ocr_lang="khm+eng")
        assert self._langs(cache, sample_pdf) == [("khm+eng", "second")]

    def test_whitespace_variants_share_one_row(self, cache, sample_pdf):
        cache.save_page_text(sample_pdf, 0, "first", source="ocr", ocr_lang="khm+eng")
        cache.save_page_text(
            sample_pdf, 0, "second", source="ocr", ocr_lang="  khm+eng  "
        )
        assert self._langs(cache, sample_pdf) == [("khm+eng", "second")]

    def test_distinct_orderings_are_separate_rows(self, cache, sample_pdf):
        cache.save_page_text(sample_pdf, 0, "kh", source="ocr", ocr_lang="khm+eng")
        cache.save_page_text(sample_pdf, 0, "en", source="ocr", ocr_lang="eng+khm")
        assert self._langs(cache, sample_pdf) == [
            ("eng+khm", "en"),
            ("khm+eng", "kh"),
        ]

    def test_extracted_row_uses_sentinel(self, cache, sample_pdf):
        cache.save_page_text(sample_pdf, 0, "plain")
        assert self._langs(cache, sample_pdf) == [("", "plain")]

    def test_ocr_row_survives_re_extraction(self, cache, sample_pdf):
        """Re-extracting a page no longer destroys its cached OCR text: they
        are different artifacts and now occupy different rows."""
        cache.save_page_text(sample_pdf, 0, "ocr text", source="ocr", ocr_lang="khm")
        cache.save_pages_text(sample_pdf, {0: "extracted text"})
        assert self._langs(cache, sample_pdf) == [
            ("", "extracted text"),
            ("khm", "ocr text"),
        ]


class TestLanguageAwareReads:
    """With ocr_lang in the key a page can hold several rows, so every read
    must say which one it means (issue #27)."""

    def _two_langs(self, cache, sample_pdf):
        cache.save_page_text(sample_pdf, 0, "kh text", source="ocr", ocr_lang="khm+eng")
        cache.save_page_text(sample_pdf, 0, "en text", source="ocr", ocr_lang="eng+khm")
        return sample_pdf

    def test_returns_requested_language(self, cache, sample_pdf):
        self._two_langs(cache, sample_pdf)
        assert cache.get_pages_text(sample_pdf, [0], ocr_lang="khm+eng") == {
            0: "kh text"
        }
        assert cache.get_pages_text(sample_pdf, [0], ocr_lang="eng+khm") == {
            0: "en text"
        }

    def test_requested_language_is_normalized(self, cache, sample_pdf):
        self._two_langs(cache, sample_pdf)
        assert cache.get_pages_text(sample_pdf, [0], ocr_lang="KHM+ENG ") == {
            0: "kh text"
        }

    def test_unknown_language_is_a_miss(self, cache, sample_pdf):
        self._two_langs(cache, sample_pdf)
        assert cache.get_pages_text(sample_pdf, [0], ocr_lang="tha") == {}

    def test_extracted_row_answers_any_language(self, cache, sample_pdf):
        cache.save_pages_text(sample_pdf, {0: "real text layer"})
        assert cache.get_pages_text(sample_pdf, [0], ocr_lang="tha") == {
            0: "real text layer"
        }
        assert cache.get_pages_source(sample_pdf, [0], ocr_lang="tha") == {
            0: "extracted"
        }

    def test_real_text_layer_beats_ocr_row(self, cache, sample_pdf):
        """A page with a real text layer is never OCR'd, whatever language is
        asked for, so the extracted row wins when both are usable."""
        cache.save_page_text(sample_pdf, 0, "ocr text", source="ocr", ocr_lang="khm")
        cache.save_pages_text(sample_pdf, {0: "real text layer"})
        assert cache.get_pages_text(sample_pdf, [0], ocr_lang="khm") == {
            0: "real text layer"
        }
        assert cache.get_pages_source(sample_pdf, [0], ocr_lang="khm") == {
            0: "extracted"
        }

    def test_empty_extracted_row_loses_to_ocr_text(self, cache, sample_pdf):
        """Extracting a scanned page yields '', and that empty row must not
        shadow real OCR text: doing so would force a pointless re-OCR."""
        cache.save_page_text(sample_pdf, 0, "ocr text", source="ocr", ocr_lang="khm")
        cache.save_pages_text(sample_pdf, {0: ""})
        assert cache.get_pages_text(sample_pdf, [0], ocr_lang="khm") == {0: "ocr text"}
        assert cache.get_pages_ocr_lang(sample_pdf, [0], ocr_lang="khm") == {0: "khm"}

    def test_unaware_reader_returns_latest_row(self, cache, sample_pdf):
        self._two_langs(cache, sample_pdf)
        assert cache.get_pages_text(sample_pdf, [0]) == {0: "en text"}
        assert cache.get_page_text(sample_pdf, 0) == "en text"

    def test_unaware_reader_agrees_with_fts(self, cache, sample_pdf):
        """save_page_text DELETE-then-INSERTs the FTS row, so FTS holds the
        last text written. A language-unaware read must return that same text,
        or a search hit and its excerpt would come from different rows."""
        self._two_langs(cache, sample_pdf)
        with sqlite3.connect(cache.db_path) as conn:
            fts = conn.execute(
                "SELECT text FROM pdf_search_fts WHERE file_path = ?",
                (sample_pdf,),
            ).fetchall()
        assert [t for (t,) in fts] == ["en text"]
        assert cache.get_page_text(sample_pdf, 0) == "en text"

    def test_unaware_reader_is_deterministic_within_one_second(self, cache, sample_pdf):
        """created_at has one-second resolution, so rowid is what actually
        orders these. Without that tiebreak this flakes."""
        for i in range(5):
            cache.save_page_text(
                sample_pdf, 0, f"text {i}", source="ocr", ocr_lang=f"l{i}"
            )
        assert cache.get_page_text(sample_pdf, 0) == "text 4"


class TestCountsArePerPageNotPerRow:
    """A page can now hold several rows (one per ocr_lang). Anything that
    counts "pages" must count pages, not rows (issue #27)."""

    def test_fts_coverage_counts_pages_not_rows(self, cache, sample_pdf):
        cache.save_page_text(sample_pdf, 0, "kh text", source="ocr", ocr_lang="khm+eng")
        cache.save_page_text(sample_pdf, 0, "en text", source="ocr", ocr_lang="eng+khm")
        cache.save_page_text(sample_pdf, 1, "page two", source="ocr", ocr_lang="khm")

        indexed, total = cache.get_fts_index_coverage(sample_pdf)

        # Two pages cached, three rows. Reporting 3 would break the
        # `indexed == total == doc_pages` fast-path check in pdf_search and
        # silently drop the document onto the slower per-query index.
        assert total == 2
        assert indexed == 2

    def test_stats_total_pages_counts_pages_not_rows(self, cache, sample_pdf):
        cache.save_page_text(sample_pdf, 0, "kh", source="ocr", ocr_lang="khm+eng")
        cache.save_page_text(sample_pdf, 0, "en", source="ocr", ocr_lang="eng+khm")

        assert cache.get_stats()["total_pages"] == 1


def test_nul_bearing_text_is_not_treated_as_empty(cache, sample_pdf):
    """Real cached pages contain embedded NULs. SQLite's LENGTH() stops at
    the first one, so a NUL-leading page would look empty and lose the
    row-preference ordering to a genuinely empty row."""
    cache.save_page_text(
        sample_pdf, 0, "\x00leading nul but real text", source="ocr", ocr_lang="khm"
    )
    cache.save_pages_text(sample_pdf, {0: ""})

    assert cache.get_pages_text(sample_pdf, [0], ocr_lang="khm") == {
        0: "\x00leading nul but real text"
    }
