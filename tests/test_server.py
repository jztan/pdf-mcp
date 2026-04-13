# tests/test_server.py
"""Tests for MCP server tools."""

import os
import tempfile

import numpy as np
import pytest

from pathlib import Path
from unittest.mock import patch, Mock

import httpx

from pdf_mcp.server import (
    _resolve_path,
    _python_search,
    _rrf_fuse,
    pdf_info,
    pdf_read_pages,
    pdf_read_all,
    pdf_search,
    pdf_get_toc,
    pdf_cache_stats,
    pdf_cache_clear,
)
from pdf_mcp.url_fetcher import URLFetcher


class TestRrfFuse:
    """Unit tests for _rrf_fuse() — pure RRF math, no PDF required."""

    def test_scores_both_lists(self):
        """Page in both lists accumulates both RRF terms."""
        # page 5: kw_rank=1 → 1/61; sem_rank=2 → 1/62
        # page 10: kw_rank=2 → 1/62; sem_rank=1 → 1/61
        # Both equal → tie broken by ascending page: [5, 10]
        result = _rrf_fuse([5, 10], [10, 5], max_results=10)
        scores = dict(result)
        assert abs(scores[5] - (1 / 61 + 1 / 62)) < 1e-6
        assert abs(scores[10] - (1 / 62 + 1 / 61)) < 1e-6
        pages = [p for p, _ in result]
        assert pages == [5, 10]  # tie broken by ascending page

    def test_keyword_only_page(self):
        """Page in keyword list only gets 1/(60+rank), semantic term = 0."""
        result = _rrf_fuse([3], [], max_results=10)
        assert len(result) == 1
        page, score = result[0]
        assert page == 3
        assert abs(score - 1 / 61) < 1e-6

    def test_semantic_only_page(self):
        """Page in semantic list only gets 1/(60+rank), keyword term = 0."""
        result = _rrf_fuse([], [7], max_results=10)
        assert len(result) == 1
        page, score = result[0]
        assert page == 7
        assert abs(score - 1 / 61) < 1e-6

    def test_tie_breaking_ascending_page(self):
        """Equal RRF scores (both rank 1 in different lists) → ascending page wins."""
        result = _rrf_fuse([20], [5], max_results=10)
        pages = [p for p, _ in result]
        assert pages == [5, 20]

    def test_max_results_honored(self):
        """Result truncated to max_results even with many candidates."""
        result = _rrf_fuse(list(range(20)), list(range(20, 40)), max_results=5)
        assert len(result) == 5

    def test_sorted_descending_score(self):
        """Pages that appear in both lists rank above pages in only one."""
        # Pages 1,2,3 appear in both lists — higher RRF score
        # Pages 4,5,6 appear only in keyword list — lower RRF score
        result = _rrf_fuse([1, 2, 3, 4, 5, 6], [1, 2, 3], max_results=10)
        scores = [s for _, s in result]
        assert scores == sorted(scores, reverse=True)
        # First 3 results should be pages 1, 2, 3
        top_pages = [p for p, _ in result[:3]]
        assert set(top_pages) == {1, 2, 3}


class TestPdfSearchModes:
    """Tests for pdf_search mode parameter routing."""

    # ── helpers ─────────────────────────────────────────────────────────

    def _make_encode(self, dim: int = 384):
        """
        Return (encode, encode_query) mocks with deterministic vectors.
        Page i gets a unit vector at dimension (i % dim).
        Query gets a unit vector at dimension 0 → page 0 scores highest.
        """

        def encode(texts):
            result = np.zeros((len(texts), dim), dtype=np.float32)
            for i in range(len(texts)):
                result[i, i % dim] = 1.0
            return result

        def encode_query(text):
            v = np.zeros(dim, dtype=np.float32)
            v[0] = 1.0
            return v

        return encode, encode_query

    # ── mode validation ──────────────────────────────────────────────────

    def test_invalid_mode_returns_error(self, sample_pdf, isolated_server):
        """Unknown mode string returns error dict before opening the PDF."""
        result = pdf_search(sample_pdf, "page", mode="fuzzy")

        assert "error" in result
        assert "fuzzy" in result["error"]
        assert "auto" in result["error"]
        assert "keyword" in result["error"]
        assert "semantic" in result["error"]

    def test_invalid_mode_includes_query(self, sample_pdf, isolated_server):
        """Error response includes the original query for context."""
        result = pdf_search(sample_pdf, "hello", mode="bad")
        assert result.get("query") == "hello"

    # ── mode="keyword" ───────────────────────────────────────────────────

    def test_keyword_mode_returns_search_mode_keyword(
        self, sample_pdf, isolated_server
    ):
        """mode='keyword' always returns search_mode='keyword'."""
        result = pdf_search(sample_pdf, "page", mode="keyword")

        assert result.get("search_mode") == "keyword"

    def test_keyword_mode_has_total_matches(self, sample_pdf, isolated_server):
        """mode='keyword' response includes total_matches and page_match_counts."""
        result = pdf_search(sample_pdf, "page", mode="keyword")

        assert "total_matches" in result
        assert "page_match_counts" in result

    def test_keyword_mode_ignores_fastembed(self, sample_pdf, isolated_server):
        """mode='keyword' never calls embedder even when fastembed is installed."""
        with patch("pdf_mcp.embedder.encode") as mock_encode:
            pdf_search(sample_pdf, "page", mode="keyword")
            mock_encode.assert_not_called()

    # ── mode="semantic", no fastembed ────────────────────────────────────

    def test_semantic_mode_no_fastembed_returns_error(
        self, sample_pdf, isolated_server
    ):
        """mode='semantic' without fastembed returns error + install_hint."""
        with patch(
            "pdf_mcp.embedder.check_available",
            side_effect=ImportError("fastembed not installed"),
        ):
            result = pdf_search(sample_pdf, "query", mode="semantic")

        assert "error" in result
        assert "install_hint" in result

    def test_semantic_mode_no_fastembed_check_before_path(
        self, isolated_server, tmp_path
    ):
        """mode='semantic' fastembed check happens before path resolution."""
        with patch(
            "pdf_mcp.embedder.check_available",
            side_effect=ImportError("fastembed not installed"),
        ):
            # Non-existent path — should still return error, not FileNotFoundError
            result = pdf_search("/nonexistent/file.pdf", "query", mode="semantic")

        assert "error" in result
        assert "install_hint" in result

    # ── mode="semantic", fastembed available ─────────────────────────────

    def test_semantic_mode_returns_search_mode_semantic(
        self, sample_pdf, isolated_server
    ):
        """mode='semantic' with fastembed returns search_mode='semantic'."""
        encode, encode_query = self._make_encode()

        with (
            patch("pdf_mcp.embedder.check_available"),
            patch("pdf_mcp.embedder.encode", encode),
            patch("pdf_mcp.embedder.encode_query", encode_query),
        ):
            result = pdf_search(sample_pdf, "test", mode="semantic")

        assert result.get("search_mode") == "semantic"

    def test_semantic_mode_omits_keyword_fields(self, sample_pdf, isolated_server):
        """mode='semantic' response omits total_matches and page_match_counts."""
        encode, encode_query = self._make_encode()

        with (
            patch("pdf_mcp.embedder.check_available"),
            patch("pdf_mcp.embedder.encode", encode),
            patch("pdf_mcp.embedder.encode_query", encode_query),
        ):
            result = pdf_search(sample_pdf, "test", mode="semantic")

        assert "total_matches" not in result
        assert "page_match_counts" not in result

    def test_semantic_mode_matches_shape(self, sample_pdf, isolated_server):
        """mode='semantic' matches have page, excerpt, score, position."""
        encode, encode_query = self._make_encode()

        with (
            patch("pdf_mcp.embedder.check_available"),
            patch("pdf_mcp.embedder.encode", encode),
            patch("pdf_mcp.embedder.encode_query", encode_query),
        ):
            result = pdf_search(sample_pdf, "test", mode="semantic")

        assert "matches" in result
        assert len(result["matches"]) > 0
        for m in result["matches"]:
            assert "page" in m
            assert "excerpt" in m
            assert "score" in m
            assert "position" in m
            assert m["position"] == 0

    def test_semantic_mode_excerpt_uses_context_chars(
        self, sample_pdf, isolated_server
    ):
        """mode='semantic' excerpt is first context_chars chars of page text."""
        encode, encode_query = self._make_encode()

        with (
            patch("pdf_mcp.embedder.check_available"),
            patch("pdf_mcp.embedder.encode", encode),
            patch("pdf_mcp.embedder.encode_query", encode_query),
        ):
            result_50 = pdf_search(
                sample_pdf, "test", mode="semantic", context_chars=50
            )
            result_200 = pdf_search(
                sample_pdf, "test", mode="semantic", context_chars=200
            )

        # Shorter context produces shorter excerpts
        lens_50 = [len(m["excerpt"]) for m in result_50["matches"]]
        lens_200 = [len(m["excerpt"]) for m in result_200["matches"]]
        assert max(lens_50) <= max(lens_200)

    # ── mode="auto" hybrid ───────────────────────────────────────────────

    def test_auto_mode_no_fastembed_returns_keyword(
        self, sample_pdf, isolated_server
    ):
        """mode='auto' without fastembed falls back to search_mode='keyword'."""
        with patch(
            "pdf_mcp.embedder.check_available",
            side_effect=ImportError("fastembed not installed"),
        ):
            result = pdf_search(sample_pdf, "page", mode="auto")

        assert result.get("search_mode") == "keyword"
        assert "total_matches" in result
        assert "page_match_counts" in result

    def test_auto_mode_with_fastembed_returns_hybrid(
        self, sample_pdf, isolated_server
    ):
        """mode='auto' with fastembed available returns search_mode='hybrid'."""
        encode, encode_query = self._make_encode()

        with (
            patch("pdf_mcp.embedder.check_available"),
            patch("pdf_mcp.embedder.encode", encode),
            patch("pdf_mcp.embedder.encode_query", encode_query),
        ):
            result = pdf_search(sample_pdf, "page", mode="auto")

        assert result.get("search_mode") == "hybrid"

    def test_hybrid_has_total_matches(self, sample_pdf, isolated_server):
        """Hybrid mode includes total_matches and page_match_counts (keyword counts)."""
        encode, encode_query = self._make_encode()

        with (
            patch("pdf_mcp.embedder.check_available"),
            patch("pdf_mcp.embedder.encode", encode),
            patch("pdf_mcp.embedder.encode_query", encode_query),
        ):
            result = pdf_search(sample_pdf, "page", mode="auto")

        assert "total_matches" in result
        assert "page_match_counts" in result

    def test_hybrid_max_results_honored(self, sample_pdf, isolated_server):
        """Hybrid mode returns at most max_results matches."""
        encode, encode_query = self._make_encode()

        with (
            patch("pdf_mcp.embedder.check_available"),
            patch("pdf_mcp.embedder.encode", encode),
            patch("pdf_mcp.embedder.encode_query", encode_query),
        ):
            result = pdf_search(sample_pdf, "page", mode="auto", max_results=2)

        assert len(result["matches"]) <= 2

    def test_hybrid_semantic_only_pages_appear(
        self, isolated_server, tmp_path
    ):
        """Pages with no keyword match but high semantic score appear in results."""
        import pymupdf as _pymupdf

        # Page 1: has keyword "banana"; page 2: unrelated text, wins semantically
        pdf_path = str(tmp_path / "hybrid_test.pdf")
        doc = _pymupdf.open()
        p1 = doc.new_page()
        p1.insert_text((50, 50), "banana is a yellow fruit")
        p2 = doc.new_page()
        p2.insert_text((50, 50), "unrelated filler text here for page two content")
        doc.save(pdf_path)
        doc.close()

        dim = 384

        def encode(texts):
            # Page 0 (banana page): unit vec at dim 1
            # Page 1 (filler page): unit vec at dim 0
            result = np.zeros((len(texts), dim), dtype=np.float32)
            result[0, 1] = 1.0
            if len(texts) > 1:
                result[1, 0] = 1.0
            return result

        def encode_query(text):
            # Query matches dim 0 → page 1 (filler) wins semantically
            v = np.zeros(dim, dtype=np.float32)
            v[0] = 1.0
            return v

        with (
            patch("pdf_mcp.embedder.check_available"),
            patch("pdf_mcp.embedder.encode", encode),
            patch("pdf_mcp.embedder.encode_query", encode_query),
        ):
            result = pdf_search(pdf_path, "banana", mode="auto", max_results=5)

        assert result["search_mode"] == "hybrid"
        pages_returned = {m["page"] for m in result["matches"]}
        # Page 2 (1-indexed) should appear even though it has no "banana" keyword
        assert 2 in pages_returned

    def test_hybrid_excerpt_source(self, isolated_server, tmp_path):
        """Keyword-hit pages use FTS snippet; semantic-only pages use truncated text."""
        import pymupdf as _pymupdf

        cache_instance, _ = isolated_server
        if not cache_instance.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        # Page 1 has keyword; page 2 is semantic-only
        pdf_path = str(tmp_path / "excerpt_test.pdf")
        keyword_text = "the quick brown fox jumps over the lazy dog"
        filler_text = "unrelated content fills this second page entirely"
        doc = _pymupdf.open()
        p1 = doc.new_page()
        p1.insert_text((50, 50), keyword_text)
        p2 = doc.new_page()
        p2.insert_text((50, 50), filler_text)
        doc.save(pdf_path)
        doc.close()

        dim = 384

        def encode(texts):
            result = np.zeros((len(texts), dim), dtype=np.float32)
            result[0, 1] = 1.0  # page 0: dim 1
            if len(texts) > 1:
                result[1, 0] = 1.0  # page 1: dim 0 → wins query
            return result

        def encode_query(text):
            v = np.zeros(dim, dtype=np.float32)
            v[0] = 1.0
            return v

        # Pre-populate FTS index
        pdf_read_pages(pdf_path, "1-2")

        with (
            patch("pdf_mcp.embedder.check_available"),
            patch("pdf_mcp.embedder.encode", encode),
            patch("pdf_mcp.embedder.encode_query", encode_query),
        ):
            result = pdf_search(pdf_path, "fox", mode="auto", max_results=5)

        assert result["search_mode"] == "hybrid"
        by_page = {m["page"]: m for m in result["matches"]}

        # Page 1 (keyword hit) — FTS snippet should contain the matched word
        assert 1 in by_page, "Page 1 (keyword hit) must appear in hybrid results"
        assert "fox" in by_page[1]["excerpt"].lower()

        # Page 2 (semantic only) — excerpt is raw page text prefix, not FTS snippet
        assert 2 in by_page, "Page 2 (semantic-only) must appear in hybrid results"
        assert by_page[2]["excerpt"].startswith(filler_text[:20])

    def test_hybrid_embedding_cache_used_on_second_call(
        self, sample_pdf, isolated_server
    ):
        """Second hybrid call hits embedding cache (encode not called again)."""
        encode, encode_query = self._make_encode()
        encode_mock = Mock(side_effect=encode)

        with (
            patch("pdf_mcp.embedder.check_available"),
            patch("pdf_mcp.embedder.encode", encode_mock),
            patch("pdf_mcp.embedder.encode_query", encode_query),
        ):
            pdf_search(sample_pdf, "page", mode="auto")   # first call — encodes
            call_count_after_first = encode_mock.call_count

            pdf_search(sample_pdf, "content", mode="auto")  # second call — cache hit
            call_count_after_second = encode_mock.call_count

        # encode() for page texts should not be called again on second query
        assert call_count_after_second == call_count_after_first

    def test_default_mode_is_auto(self, sample_pdf, isolated_server):
        """Calling pdf_search without mode defaults to 'auto' behaviour."""
        with patch(
            "pdf_mcp.embedder.check_available",
            side_effect=ImportError("no fastembed"),
        ):
            result = pdf_search(sample_pdf, "page")

        # auto + no fastembed → keyword
        assert result.get("search_mode") == "keyword"


class TestPdfInfo:
    """Tests for pdf_info tool."""

    def test_pdf_info_basic(self, sample_pdf, isolated_server):
        """Valid PDF returns expected fields."""
        result = pdf_info(sample_pdf)

        assert result["page_count"] == 5
        assert result["from_cache"] is False
        assert "metadata" in result
        assert "toc" in result
        assert "toc_entry_count" in result
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
        """PDF with bookmarks returns toc inline when within limit."""
        result = pdf_info(sample_pdf_with_toc)

        assert result["toc_entry_count"] == 3
        assert len(result["toc"]) == 3
        assert result["toc"][0]["title"] == "Chapter 1"
        assert "toc_truncated" not in result

    def test_pdf_info_with_toc_entry_count(self, sample_pdf_with_toc, isolated_server):
        """Small TOC includes toc_entry_count."""
        result = pdf_info(sample_pdf_with_toc)

        assert result["toc_entry_count"] == 3
        assert "toc" in result
        assert "toc_truncated" not in result

    def test_pdf_info_large_toc_truncated(
        self, sample_pdf_with_large_toc, isolated_server
    ):
        """TOC with >50 entries is omitted; toc_truncated and toc_entry_count set."""
        result = pdf_info(sample_pdf_with_large_toc)

        assert result["toc_entry_count"] == 60
        assert result["toc_truncated"] is True
        assert "toc" not in result

    def test_pdf_info_large_toc_truncated_cached(
        self, sample_pdf_with_large_toc, isolated_server
    ):
        """Truncation logic applies on cache-hit path too."""
        pdf_info(sample_pdf_with_large_toc)  # populate cache
        result = pdf_info(sample_pdf_with_large_toc)  # cache hit

        assert result["from_cache"] is True
        assert result["toc_entry_count"] == 60
        assert result["toc_truncated"] is True
        assert "toc" not in result

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
        """Keyword mode: no keyword match returns empty matches."""
        result = pdf_search(sample_pdf, "xyznonexistent", mode="keyword")

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
        """Finds across pages — use page_match_counts instead of pages_with_matches."""
        result = pdf_search(sample_pdf, "content")

        # "content" appears on all 5 pages
        assert len(result["page_match_counts"]) >= 2

    def test_search_context_chars(self, sample_pdf, isolated_server):
        """Custom context size works; score field present."""
        result_small = pdf_search(sample_pdf, "page", context_chars=20)
        result_large = pdf_search(sample_pdf, "page", context_chars=100)

        if result_small["matches"] and result_large["matches"]:
            assert len(result_large["matches"][0]["excerpt"]) >= len(
                result_small["matches"][0]["excerpt"]
            )
        if result_small["matches"]:
            assert "score" in result_small["matches"][0]


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
        """All expected keys present, including fts_indexed_pages."""
        result = pdf_cache_stats()

        expected_keys = [
            "total_files",
            "total_pages",
            "total_images",
            "fts_indexed_pages",
            "cache_size_bytes",
            "cache_size_mb",
            "url_cache",
        ]
        for key in expected_keys:
            assert key in result

    def test_cache_stats_includes_fts_indexed_pages(self, isolated_server):
        """pdf_cache_stats response includes fts_indexed_pages."""
        result = pdf_cache_stats()

        assert "fts_indexed_pages" in result
        assert isinstance(result["fts_indexed_pages"], int)
        assert result["fts_indexed_pages"] == 0  # empty cache

    def test_cache_stats_fts_pages_nonzero_after_search(
        self, sample_pdf, isolated_server
    ):
        """fts_indexed_pages > 0 after pdf_search builds the FTS index."""
        cache_instance, _ = isolated_server
        if not cache_instance.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        pdf_search(sample_pdf, "page")
        result = pdf_cache_stats()

        assert result["fts_indexed_pages"] > 0


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
        """Search → read pattern — updated for page_match_counts."""
        search_result = pdf_search(sample_pdf, "page 3")

        if search_result["page_match_counts"]:
            page_num = int(list(search_result["page_match_counts"].keys())[0])
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


class TestReadPagesInlineTables:
    """Tests for always-inline per-page tables in pdf_read_pages."""

    def test_read_pages_always_includes_tables_field(self, sample_pdf, isolated_server):
        """Every page dict has 'tables' (list) and 'table_count' (int)."""
        result = pdf_read_pages(sample_pdf, "1")
        page = result["pages"][0]
        assert "tables" in page
        assert "table_count" in page
        assert isinstance(page["tables"], list)
        assert isinstance(page["table_count"], int)
        assert page["tables"] == []
        assert page["table_count"] == 0

    def test_read_pages_tables_nested_per_page(
        self, sample_pdf_with_table, isolated_server
    ):
        """Tables are nested per-page; total_tables is at the top level."""
        result = pdf_read_pages(sample_pdf_with_table, "1")
        assert "tables" not in result  # no top-level 'tables' key
        assert "total_tables" in result
        page = result["pages"][0]
        assert "tables" in page
        assert isinstance(page["tables"], list)
        assert page["table_count"] == len(page["tables"])

    def test_read_pages_tables_structure(self, sample_pdf_with_table, isolated_server):
        """Each table dict has required keys; row_count == 1 + len(rows)."""
        result = pdf_read_pages(sample_pdf_with_table, "1")
        page = result["pages"][0]
        assert page["table_count"] > 0
        table = page["tables"][0]
        assert "index" in table
        assert "bbox" in table
        assert "row_count" in table
        assert "col_count" in table
        assert "header" in table
        assert "rows" in table
        assert "page" not in table
        assert isinstance(table["bbox"], list)
        assert len(table["bbox"]) == 4
        assert isinstance(table["header"], list)
        assert isinstance(table["rows"], list)
        assert table["row_count"] == 1 + len(table["rows"])

    def test_read_pages_tables_cached(self, sample_pdf_with_table, isolated_server):
        """Second call returns identical table data from cache."""
        result1 = pdf_read_pages(sample_pdf_with_table, "1")
        tables1 = result1["pages"][0]["tables"]

        result2 = pdf_read_pages(sample_pdf_with_table, "1")
        tables2 = result2["pages"][0]["tables"]

        assert len(tables1) > 0
        assert tables1 == tables2

    def test_read_pages_total_tables_count(
        self, sample_pdf_with_table, isolated_server
    ):
        """Top-level total_tables equals sum of table_count across all page dicts."""
        result = pdf_read_pages(sample_pdf_with_table, "1")
        expected = sum(p["table_count"] for p in result["pages"])
        assert result["total_tables"] == expected

    def test_tableless_page_cached(self, sample_pdf, isolated_server):
        """extract_tables_from_page called once; [] cached as sentinel."""
        with patch(
            "pdf_mcp.server.extract_tables_from_page", return_value=[]
        ) as mock_extract:
            pdf_read_pages(sample_pdf, "1")
            assert mock_extract.call_count == 1

            pdf_read_pages(sample_pdf, "1")
            assert mock_extract.call_count == 1  # not called again


class TestPdfSearchFTS5:
    """Tests for FTS5-upgraded pdf_search tool."""

    def test_search_empty_query_returns_error(self, sample_pdf, isolated_server):
        """Empty string query returns error dict without opening the PDF."""
        result = pdf_search(sample_pdf, "")

        assert "error" in result
        assert "query" in result
        assert result["query"] == ""
        assert "matches" not in result

    def test_search_whitespace_only_query_returns_error(
        self, sample_pdf, isolated_server
    ):
        """Whitespace-only query is rejected as empty."""
        result = pdf_search(sample_pdf, "   ")

        assert "error" in result

    def test_search_result_has_score_field(self, sample_pdf, isolated_server):
        """Each match dict includes a 'score' field."""
        result = pdf_search(sample_pdf, "page")

        assert "matches" in result
        if result["matches"]:
            match = result["matches"][0]
            assert "score" in match
            assert isinstance(match["score"], float)
            assert match["score"] >= 0.0

    def test_search_result_has_search_mode_field(self, sample_pdf, isolated_server):
        """Response includes 'search_mode' string field."""
        result = pdf_search(sample_pdf, "page")

        assert "search_mode" in result
        assert result["search_mode"] in ("keyword", "hybrid", "semantic")

    def test_search_uses_fts_after_read_pages(self, sample_pdf, isolated_server):
        """After pdf_read_pages populates page text cache, pdf_search uses FTS index."""
        cache_instance, _ = isolated_server
        if not cache_instance.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        pdf_read_pages(sample_pdf, "1-5")
        result = pdf_search(sample_pdf, "content")

        assert result["search_mode"] in ("keyword", "hybrid")

    def test_search_fallback_when_not_indexed(self, sample_pdf, isolated_server):
        """Without prior pdf_read_pages, pdf_search still returns results via scan."""
        result = pdf_search(sample_pdf, "page")

        assert "matches" in result
        assert result["total_matches"] >= 1
        assert "search_mode" in result

    def test_search_indexes_pages_during_scan(self, sample_pdf, isolated_server):
        """After pdf_search completes a scan, FTS index is populated for that file."""
        cache_instance, _ = isolated_server
        if not cache_instance.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        pdf_search(sample_pdf, "page")

        indexed, total = cache_instance.get_fts_index_coverage(sample_pdf)
        assert indexed == total
        assert total == 5  # sample_pdf has 5 pages

    def test_search_second_call_uses_fts(self, sample_pdf, isolated_server):
        """Second pdf_search after first scan returns keyword or hybrid mode."""
        cache_instance, _ = isolated_server
        if not cache_instance.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        pdf_search(sample_pdf, "page")  # First call — builds index
        result2 = pdf_search(sample_pdf, "content")  # Second call — should use FTS

        assert result2["search_mode"] in ("keyword", "hybrid")

    def test_search_page_match_counts_returned(self, sample_pdf, isolated_server):
        """Response has 'page_match_counts' dict, not 'pages_with_matches' list."""
        result = pdf_search(sample_pdf, "page")

        assert "page_match_counts" in result
        assert "pages_with_matches" not in result
        assert isinstance(result["page_match_counts"], dict)

    def test_search_page_match_counts_keys_are_page_numbers(
        self, sample_pdf, isolated_server
    ):
        """page_match_counts keys are strings of 1-indexed page numbers."""
        result = pdf_search(sample_pdf, "page")

        for key in result["page_match_counts"]:
            assert isinstance(key, str)
            assert int(key) >= 1

    def test_search_total_matches_accurate_no_early_exit(
        self, sample_pdf, isolated_server
    ):
        """total_matches reflects all pages, not truncated by max_results."""
        result_limited = pdf_search(sample_pdf, "page", max_results=1)
        result_full = pdf_search(sample_pdf, "page", max_results=100)

        assert result_limited["total_matches"] == result_full["total_matches"]
        assert len(result_limited["matches"]) == 1
        assert len(result_full["matches"]) >= 5

    def test_search_stemming_via_fts(self, isolated_server, tmp_path):
        """FTS5 stemming: query 'search' finds pages with 'searching'."""
        import pymupdf

        cache_instance, _ = isolated_server
        if not cache_instance.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        pdf_path = str(tmp_path / "stemming_test.pdf")
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((50, 50), "We are searching for the document")
        doc.save(pdf_path)
        doc.close()

        pdf_read_pages(pdf_path, "1")
        result = pdf_search(pdf_path, "search")

        assert result["total_matches"] >= 1, (
            "Porter stemmer should match 'searching'; also 'search' is a literal "
            "substring of 'searching' ensuring total_matches > 0"
        )
        assert result["search_mode"] in ("keyword", "hybrid")
        assert len(result["matches"]) >= 1

    def test_search_no_matches_empty_page_match_counts(
        self, sample_pdf, isolated_server
    ):
        """No keyword matches: total_matches=0, page_match_counts={}, matches=[]."""
        result = pdf_search(sample_pdf, "xyznonexistent", mode="keyword")

        assert result["total_matches"] == 0
        assert result["page_match_counts"] == {}
        assert result["matches"] == []

    def test_search_max_results_clamped_to_100(self, sample_pdf, isolated_server):
        """max_results=999999 is clamped to 100."""
        result = pdf_search(sample_pdf, "page", max_results=999999)
        assert len(result["matches"]) <= 100

    def test_search_content_warning_present(self, sample_pdf, isolated_server):
        """Response always includes content_warning."""
        result = pdf_search(sample_pdf, "page")
        assert "content_warning" in result

    def test_search_query_in_response(self, sample_pdf, isolated_server):
        """Response echoes back the query string."""
        result = pdf_search(sample_pdf, "some text")
        assert result["query"] == "some text"

    def test_search_searched_pages_equals_document_length(
        self, sample_pdf, isolated_server
    ):
        """searched_pages reflects total page count of document."""
        result = pdf_search(sample_pdf, "page")
        assert result["searched_pages"] == 5  # sample_pdf has 5 pages

    def test_search_returns_matches_when_fts_unavailable(self, tmp_path):
        """F3: pdf_search returns non-empty matches even when fts_available=False."""
        import pymupdf
        import pdf_mcp.server as server_module
        from pdf_mcp.cache import PDFCache

        pdf_path = str(tmp_path / "fts_off_test.pdf")
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((50, 50), "The quick brown fox jumps over the lazy dog")
        doc.save(pdf_path)
        doc.close()

        no_fts_cache = PDFCache(cache_dir=tmp_path / "cache_no_fts", ttl_hours=1)
        no_fts_cache.fts_available = False

        original_cache = server_module.cache
        server_module.cache = no_fts_cache
        try:
            result = pdf_search(pdf_path, "fox")
        finally:
            server_module.cache = original_cache

        assert result["search_mode"] in ("keyword", "hybrid")
        assert result["total_matches"] >= 1
        assert len(result["matches"]) >= 1
        assert result["matches"][0]["excerpt"]


class TestPythonSearch:
    """Tests for _python_search word-boundary snapping and ellipsis logic."""

    def test_word_boundary_and_ellipsis(self):
        """Match in long text triggers word-boundary snapping and ellipsis."""
        prefix = "word " * 20  # 100 chars, many spaces for rfind
        suffix = "word " * 20
        text = prefix + "TARGET" + suffix
        matches, _ = _python_search(
            {0: text}, "TARGET", max_results=5, context_chars=20
        )
        assert len(matches) == 1
        excerpt = matches[0]["excerpt"]
        assert excerpt.startswith("...")  # ellipsis prepended (ctx_start > 0)
        assert excerpt.endswith("...")  # ellipsis appended (ctx_end < len(text))


class TestSearchScanCacheHit:
    """Test that the scan path reuses already-cached page text (server.py:528)."""

    def test_scan_uses_cached_text_for_partially_indexed_file(
        self, sample_pdf, isolated_server
    ):
        """Scan path hits the cached-text branch when FTS is only partially indexed."""
        cache_instance, _ = isolated_server
        if not cache_instance.fts_available:
            pytest.skip("FTS5 not available in this SQLite build")

        # Pre-cache page 0 → FTS has 1 of 5 pages (partial coverage → scan path taken)
        cache_instance.save_page_text(sample_pdf, 0, "pre-cached page zero content")

        result = pdf_search(sample_pdf, "page")

        assert "matches" in result
        # After scan, all pages should be indexed
        indexed, total = cache_instance.get_fts_index_coverage(sample_pdf)
        assert indexed == total
