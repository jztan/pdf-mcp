# tests/test_server.py
"""Tests for MCP server tools."""

import os
import tempfile
from typing import Any

import numpy as np
import pytest

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
    pdf_render_pages,
)
from pdf_mcp.url_fetcher import URLFetcher
from pdf_mcp.parallel import PageError


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

        def encode(texts, model_name="BAAI/bge-small-en-v1.5"):
            result = np.zeros((len(texts), dim), dtype=np.float32)
            for i in range(len(texts)):
                result[i, i % dim] = 1.0
            return result

        def encode_query(text, model_name="BAAI/bge-small-en-v1.5"):
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

    def test_semantic_mode_includes_count_fields(self, sample_pdf, isolated_server):
        """mode='semantic' response includes total_matches matching len(matches)."""
        encode, encode_query = self._make_encode()

        with (
            patch("pdf_mcp.embedder.check_available"),
            patch("pdf_mcp.embedder.encode", encode),
            patch("pdf_mcp.embedder.encode_query", encode_query),
        ):
            result = pdf_search(sample_pdf, "test", mode="semantic")

        assert "total_matches" in result
        assert "page_match_counts" in result
        assert result["total_matches"] == len(result["matches"])

    def test_semantic_mode_low_confidence_flagged_when_score_below_threshold(
        self, sample_pdf, isolated_server
    ):
        """Each semantic match carries low_confidence; response carries the
        threshold and a roll-up flag. With a deterministic encode that yields
        cosine ~0 we expect all_results_low_confidence=True."""
        encode, encode_query = self._make_encode()

        with (
            patch("pdf_mcp.embedder.check_available"),
            patch("pdf_mcp.embedder.encode", encode),
            patch("pdf_mcp.embedder.encode_query", encode_query),
        ):
            result = pdf_search(sample_pdf, "unrelated", mode="semantic")

        assert "confidence_threshold" in result
        assert "all_results_low_confidence" in result
        for m in result["matches"]:
            assert "low_confidence" in m
            assert isinstance(m["low_confidence"], bool)
            assert m["low_confidence"] is (m["score"] < result["confidence_threshold"])

    def test_hybrid_mode_low_confidence_flag(self, sample_pdf, isolated_server):
        """Hybrid match without keyword hit and with semantic cosine below
        the threshold is flagged low_confidence; pages with keyword hits
        are not flagged (literal terms appear)."""
        encode, encode_query = self._make_encode()

        with (
            patch("pdf_mcp.embedder.check_available"),
            patch("pdf_mcp.embedder.encode", encode),
            patch("pdf_mcp.embedder.encode_query", encode_query),
        ):
            # "page" appears literally in the corpus → keyword hits → confident
            kw_result = pdf_search(sample_pdf, "page", mode="auto")
            # Unrelated query → no keyword hits and tiny cosine scores
            none_result = pdf_search(sample_pdf, "unrelated", mode="auto")

        for m in kw_result["matches"]:
            assert "low_confidence" in m
            assert "semantic_score" in m
        # The "unrelated" query should have at least one low-confidence match
        # and the top-level rollup should reflect that
        assert "all_results_low_confidence" in none_result
        assert any(m["low_confidence"] for m in none_result["matches"])
        # Returned, not dropped — agent decides what to do
        assert len(none_result["matches"]) > 0

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

    def test_auto_mode_no_fastembed_returns_keyword(self, sample_pdf, isolated_server):
        """mode='auto' without fastembed falls back to search_mode='keyword'."""
        with patch(
            "pdf_mcp.embedder.check_available",
            side_effect=ImportError("fastembed not installed"),
        ):
            result = pdf_search(sample_pdf, "page", mode="auto")

        assert result.get("search_mode") == "keyword"
        assert "total_matches" in result
        assert "page_match_counts" in result

    def test_auto_mode_falls_back_on_encode_failure(self, sample_pdf, isolated_server):
        """mode='auto' degrades to keyword when embedder.encode() raises
        (model load failure, network outage, etc.)."""
        with (
            patch("pdf_mcp.embedder.check_available"),
            patch(
                "pdf_mcp.embedder.encode",
                side_effect=ValueError("Could not load model BAAI/bge-small-en-v1.5"),
            ),
        ):
            result = pdf_search(sample_pdf, "page", mode="auto")

        assert result.get("search_mode") == "keyword"
        assert result.get("semantic_unavailable") is True
        assert "semantic_unavailable_reason" in result
        assert "Could not load model" in result["semantic_unavailable_reason"]

    def test_auto_mode_falls_back_on_encode_query_failure(
        self, sample_pdf, isolated_server
    ):
        """mode='auto' degrades to keyword when encode_query() raises after
        page embeddings are already cached."""
        encode, _ = self._make_encode()
        with (
            patch("pdf_mcp.embedder.check_available"),
            patch("pdf_mcp.embedder.encode", encode),
            patch(
                "pdf_mcp.embedder.encode_query",
                side_effect=ValueError("Could not load model"),
            ),
        ):
            result = pdf_search(sample_pdf, "page", mode="auto")

        assert result.get("search_mode") == "keyword"
        assert result.get("semantic_unavailable") is True

    def test_auto_mode_no_fastembed_omits_unavailable_flag(
        self, sample_pdf, isolated_server
    ):
        """ImportError fallback (fastembed missing) does not set
        semantic_unavailable — that flag is for installed-but-broken cases."""
        with patch(
            "pdf_mcp.embedder.check_available",
            side_effect=ImportError("fastembed not installed"),
        ):
            result = pdf_search(sample_pdf, "page", mode="auto")

        assert result.get("search_mode") == "keyword"
        assert "semantic_unavailable" not in result

    def test_auto_mode_with_fastembed_returns_hybrid(self, sample_pdf, isolated_server):
        """mode='auto' with fastembed available returns search_mode='hybrid'."""
        encode, encode_query = self._make_encode()

        with (
            patch("pdf_mcp.embedder.check_available"),
            patch("pdf_mcp.embedder.encode", encode),
            patch("pdf_mcp.embedder.encode_query", encode_query),
        ):
            result = pdf_search(sample_pdf, "page", mode="auto")

        assert result.get("search_mode") == "hybrid"

    def test_hybrid_total_matches_equals_len_matches(self, sample_pdf, isolated_server):
        """Hybrid mode: total_matches equals len(matches) after RRF fusion."""
        encode, encode_query = self._make_encode()

        with (
            patch("pdf_mcp.embedder.check_available"),
            patch("pdf_mcp.embedder.encode", encode),
            patch("pdf_mcp.embedder.encode_query", encode_query),
        ):
            result = pdf_search(sample_pdf, "page", mode="auto")

        assert "total_matches" in result
        assert "page_match_counts" in result
        assert result["total_matches"] == len(result["matches"])

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

    def test_hybrid_semantic_only_pages_appear(self, isolated_server, tmp_path):
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

        def encode(texts, model_name="BAAI/bge-small-en-v1.5"):
            # Page 0 (banana page): unit vec at dim 1
            # Page 1 (filler page): unit vec at dim 0
            result = np.zeros((len(texts), dim), dtype=np.float32)
            result[0, 1] = 1.0
            if len(texts) > 1:
                result[1, 0] = 1.0
            return result

        def encode_query(text, model_name="BAAI/bge-small-en-v1.5"):
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

        def encode(texts, model_name="BAAI/bge-small-en-v1.5"):
            result = np.zeros((len(texts), dim), dtype=np.float32)
            result[0, 1] = 1.0  # page 0: dim 1
            if len(texts) > 1:
                result[1, 0] = 1.0  # page 1: dim 0 → wins query
            return result

        def encode_query(text, model_name="BAAI/bge-small-en-v1.5"):
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
            pdf_search(sample_pdf, "page", mode="auto")  # first call — encodes
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

    def test_total_matches_equals_len_matches_property(
        self, sample_pdf, isolated_server
    ):
        """Property: total_matches == len(matches) across every mode and
        every query, including multi-word tokenised queries that the
        1.12.0 LLM evaluation surfaced as the schema regression.

        This is the codified-in-CI version of the cross-mode matrix in
        the evaluation reports. If a future change reintroduces a
        meaning-mismatch on total_matches, this test fails the build.
        """
        encode, encode_query = self._make_encode()
        queries = [
            "page",
            "pgvector latency",
            "this string definitely does not appear",
            "xyznonexistent",
        ]
        modes = ["keyword", "semantic", "auto"]

        with (
            patch("pdf_mcp.embedder.check_available"),
            patch("pdf_mcp.embedder.encode", encode),
            patch("pdf_mcp.embedder.encode_query", encode_query),
        ):
            for mode in modes:
                for query in queries:
                    result = pdf_search(sample_pdf, query, mode=mode)
                    assert (
                        "matches" in result
                    ), f"mode={mode} query={query!r}: missing matches"
                    assert (
                        "total_matches" in result
                    ), f"mode={mode} query={query!r}: missing total_matches"
                    assert result["total_matches"] == len(result["matches"]), (
                        f"mode={mode} query={query!r}: "
                        f"total_matches={result['total_matches']} vs "
                        f"len(matches)={len(result['matches'])}"
                    )


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

    def test_invalid_path_returns_inline_error(self, isolated_server):
        """Invalid path returns inline error dict (no raise)."""
        result = pdf_info("/nonexistent/path.pdf")
        assert "error" in result
        assert "PDF file not found" in result["error"]
        assert "hint" in result

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
        """Pages with images surface an opaque image_id, never the disk path."""
        result = pdf_read_pages(sample_pdf_with_images, "1")
        page = result["pages"][0]
        assert page["image_count"] > 0
        img = page["images"][0]
        # Wire-format invariant: image_id is the basename, no absolute path
        # crosses the wire.
        assert "image_id" in img
        assert "path" not in img
        assert "/" not in img["image_id"] and "\\" not in img["image_id"]
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

    def test_invalid_path_returns_inline_error(self, isolated_server):
        """Invalid path returns inline error dict (no raise)."""
        result = pdf_read_all("/nonexistent/path.pdf")
        assert "error" in result
        assert "PDF file not found" in result["error"]
        assert "hint" in result

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
        """Invalid path returns inline error dict (no raise)."""
        result = pdf_get_toc("/nonexistent/path.pdf")
        assert "error" in result
        assert "PDF file not found" in result["error"]
        assert "hint" in result


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
        """Non-PDF file extensions return inline error dict."""
        import tempfile
        import os

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"not a pdf")
            txt_path = f.name

        try:
            result = pdf_info(txt_path)
            assert "error" in result
            assert "Only PDF files are supported" in result["error"]
            assert "hint" in result
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
    """Tests for _resolve_path helper — inline-error contract.

    _resolve_path returns (path, None) on success or
    (None, {"error", "hint"}) on failure. It does not raise for
    user-recoverable failures.
    """

    def test_relative_path_resolved(self, sample_pdf, isolated_server):
        """Relative path is resolved to an absolute string with no error."""
        rel_path = os.path.relpath(sample_pdf)
        local_path, err = _resolve_path(rel_path)
        assert err is None
        assert local_path is not None
        assert os.path.isabs(local_path)

    def test_url_http_status_error_inline(self, isolated_server):
        """HTTPStatusError from URL fetch returns inline error dict."""
        mock_response = Mock()
        mock_response.status_code = 404
        error = httpx.HTTPStatusError(
            "Not Found", request=Mock(), response=mock_response
        )
        with patch.object(URLFetcher, "is_url", return_value=True):
            with patch.object(URLFetcher, "fetch", side_effect=error):
                local_path, err = _resolve_path("https://example.com/missing.pdf")
        assert local_path is None
        assert err is not None
        assert "HTTP 404" in err["error"]
        assert "redirect" in err["hint"].lower()

    def test_url_http_error_inline(self, isolated_server):
        """Generic HTTPError from URL fetch returns inline error dict."""
        error = httpx.ConnectError("Connection refused")
        with patch.object(URLFetcher, "is_url", return_value=True):
            with patch.object(URLFetcher, "fetch", side_effect=error):
                local_path, err = _resolve_path("https://example.com/unreachable.pdf")
        assert local_path is None
        assert err is not None
        assert "ConnectError" in err["error"]
        assert "accessible" in err["hint"].lower()

    def test_url_value_error_inline(self, isolated_server):
        """ValueError from URL fetch surfaces fetcher message verbatim.

        Fetcher composes self-describing errors (SSRF deny list,
        HTTPS-only, content-type mismatch). _resolve_path preserves them.
        """
        error = ValueError("URL does not appear to be a PDF")
        with patch.object(URLFetcher, "is_url", return_value=True):
            with patch.object(URLFetcher, "fetch", side_effect=error):
                local_path, err = _resolve_path("https://example.com/fake.pdf")
        assert local_path is None
        assert err is not None
        assert err["error"] == "URL does not appear to be a PDF"
        assert "https://" in err["hint"]

    @pytest.mark.parametrize(
        "fetcher_msg,hint_keyword",
        [
            ("Only HTTPS URLs are supported (got: http)", "https://"),
            (
                "URL host resolves to a blocked IP on the SSRF deny list",
                "SSRF deny list",
            ),
            ("URL host denied by config: evil.example.com", "[urls]"),
            ("URL host not in allowed list: foo.example.com", "[urls]"),
            ("URL content-type 'text/html' is not a PDF", "content-type"),
            ("URL does not appear to be a PDF", "%PDF"),
            ("PDF file too large: 999 bytes", "size limit"),
            ("PDF download exceeded maximum size", "size limit"),
            ("Too many redirects (max 5)", "redirects"),
            ("DNS resolution failed for foo: ...", "resolve"),
            ("Could not extract hostname from URL: ...", "resolve"),
        ],
    )
    def test_url_value_error_per_cause_hint(
        self, isolated_server, fetcher_msg, hint_keyword
    ):
        """Each fetcher ValueError variant maps to a per-cause hint."""
        with patch.object(URLFetcher, "is_url", return_value=True):
            with patch.object(URLFetcher, "fetch", side_effect=ValueError(fetcher_msg)):
                local_path, err = _resolve_path("https://example.com/x.pdf")
        assert local_path is None
        assert err is not None
        assert err["error"] == fetcher_msg
        assert hint_keyword.lower() in err["hint"].lower()

    def test_bad_extension_inline(self, isolated_server, tmp_path):
        """Non-.pdf extension returns inline error dict."""
        not_pdf = tmp_path / "notes.txt"
        not_pdf.write_text("hi")
        local_path, err = _resolve_path(str(not_pdf))
        assert local_path is None
        assert err is not None
        assert "Only PDF files are supported" in err["error"]
        assert ".pdf" in err["hint"]

    def test_not_found_inline(self, isolated_server, tmp_path):
        """Missing file returns inline error dict."""
        missing = tmp_path / "does_not_exist.pdf"
        local_path, err = _resolve_path(str(missing))
        assert local_path is None
        assert err is not None
        assert "PDF file not found" in err["error"]
        assert "exists" in err["hint"]


class TestResolvePathInlineParity:
    """Every tool that calls _resolve_path returns the same inline
    {error, hint} shape on path/URL failure.

    Satisfies the ROADMAP deliverable "shape tests across every
    affected tool" by exercising one local (not-found) failure and
    one URL (HTTPStatusError) failure per tool.
    """

    # (tool, extra kwargs needed on top of `path`)
    TOOLS: list[tuple[Any, dict[str, Any]]] = [
        (pdf_info, {}),
        (pdf_read_pages, {"pages": "1"}),
        (pdf_read_all, {}),
        (pdf_search, {"query": "anything"}),
        (pdf_get_toc, {}),
        (pdf_render_pages, {"pages": "1"}),
    ]

    @staticmethod
    def _extract_err(result: Any) -> dict[str, Any]:
        """pdf_render_pages wraps its error in a single-element list."""
        if isinstance(result, list):
            assert len(result) >= 1
            return result[0]
        assert isinstance(result, dict)
        return result

    @pytest.mark.parametrize(
        "tool,extra",
        TOOLS,
        ids=[t.__name__ for t, _ in TOOLS],
    )
    def test_not_found_returns_inline_error(
        self, isolated_server, tmp_path, tool, extra
    ):
        missing = tmp_path / "missing.pdf"
        result = tool(path=str(missing), **extra)
        err = self._extract_err(result)
        assert "error" in err, f"{tool.__name__} did not return inline error"
        assert "PDF file not found" in err["error"]
        assert "hint" in err
        assert "exists" in err["hint"].lower()

    @pytest.mark.parametrize(
        "tool,extra",
        TOOLS,
        ids=[t.__name__ for t, _ in TOOLS],
    )
    def test_url_http_status_returns_inline_error(self, isolated_server, tool, extra):
        mock_response = Mock()
        mock_response.status_code = 503
        error = httpx.HTTPStatusError(
            "Service Unavailable", request=Mock(), response=mock_response
        )
        with patch.object(URLFetcher, "is_url", return_value=True):
            with patch.object(URLFetcher, "fetch", side_effect=error):
                result = tool(path="https://example.com/x.pdf", **extra)
        err = self._extract_err(result)
        assert "error" in err
        assert "HTTP 503" in err["error"]
        assert "hint" in err


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
        result = pdf_search(
            long_text_pdf, "UNIQUETARGET", context_chars=50, excerpt_style="snippet"
        )

        assert result["total_matches"] >= 1
        match = result["matches"][0]
        excerpt = match["excerpt"]
        # Match is in the middle of long text, so excerpt should have ellipsis
        assert "..." in excerpt
        assert "UNIQUETARGET" in excerpt


class TestReadPagesCachedImages:
    """Tests for cached image retrieval in pdf_read_pages."""

    def test_images_served_from_cache(self, sample_pdf_with_images, isolated_server):
        """Second call returns cached images; image_id resolves to the disk PNG."""
        import pdf_mcp.server as srv

        result1 = pdf_read_pages(sample_pdf_with_images, "1")
        imgs1 = result1["pages"][0]["images"]

        result2 = pdf_read_pages(sample_pdf_with_images, "1")
        imgs2 = result2["pages"][0]["images"]

        assert len(imgs1) > 0
        assert len(imgs2) == len(imgs1)
        for img in imgs2:
            assert "image_id" in img
            assert "path" not in img
            assert "data" not in img
            assert (srv.cache.images_dir / img["image_id"]).exists()


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
        """Each image dict has expected keys; no absolute disk path crosses the wire."""
        result = pdf_read_pages(sample_pdf_with_images, "1")
        page = result["pages"][0]
        assert page["image_count"] > 0
        img = page["images"][0]
        assert "index" in img
        assert "width" in img
        assert "height" in img
        assert "format" in img
        assert "image_id" in img
        assert "path" not in img  # absolute path no longer on the wire
        # Basename only — no slash, no parent dirs, no home prefix.
        assert "/" not in img["image_id"]
        assert "\\" not in img["image_id"]
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
        import pdf_mcp.server as srv

        result1 = pdf_read_pages(sample_pdf_with_images, "1")
        for img in result1["pages"][0]["images"]:
            (srv.cache.images_dir / img["image_id"]).unlink()

        result2 = pdf_read_pages(sample_pdf_with_images, "1")
        assert result2["pages"][0]["image_count"] == result1["pages"][0]["image_count"]
        for img in result2["pages"][0]["images"]:
            assert (srv.cache.images_dir / img["image_id"]).exists()

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
        import pdf_mcp.server as srv

        result = pdf_read_pages(sample_pdf_with_images, "1")
        paths = [
            srv.cache.images_dir / img["image_id"]
            for img in result["pages"][0]["images"]
        ]
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

    def test_search_total_matches_equals_len_matches_across_max_results(
        self, sample_pdf, isolated_server
    ):
        """total_matches equals len(matches) for every max_results.

        The pre-1.13 contract was total_matches = total occurrences across
        the document (intentionally independent of max_results). That was
        the source of the LLM-visible schema disagreement: total_matches
        could exceed len(matches) without any signal that the rest had
        been truncated. The schema-parity contract now makes total_matches
        always equal len(matches); the doc-wide signal lives in
        page_match_counts.
        """
        result_limited = pdf_search(sample_pdf, "page", max_results=1)
        result_full = pdf_search(sample_pdf, "page", max_results=100)

        assert result_limited["total_matches"] == len(result_limited["matches"])
        assert result_full["total_matches"] == len(result_full["matches"])
        assert result_limited["total_matches"] == 1
        assert result_full["total_matches"] >= 5

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


class TestPdfInfoTextCoverage:
    """Tests for text_coverage field in pdf_info."""

    def test_text_coverage_present(self, sample_pdf, isolated_server):
        """pdf_info response includes a summary-only text_coverage by default."""
        result = pdf_info(sample_pdf)
        assert "text_coverage" in result
        cov = result["text_coverage"]
        assert isinstance(cov, dict)
        assert "summary" in cov
        assert cov["detail_included"] is False
        # Per-page arrays omitted by default (keeps payload bounded on big PDFs)
        assert "text_chars_per_page" not in cov
        assert "raster_images_per_page" not in cov

    def test_text_coverage_detail_includes_per_page_arrays(
        self, sample_pdf, isolated_server
    ):
        """detail=True returns the parallel per-page arrays."""
        result = pdf_info(sample_pdf, detail=True)
        cov = result["text_coverage"]
        assert cov["detail_included"] is True
        # sample_pdf has 5 pages
        assert len(cov["text_chars_per_page"]) == 5
        assert len(cov["raster_images_per_page"]) == 5
        assert all(isinstance(c, int) for c in cov["text_chars_per_page"])
        assert all(isinstance(r, int) for r in cov["raster_images_per_page"])

    def test_text_coverage_text_pages_have_chars(self, sample_pdf, isolated_server):
        """Pages with text have text_chars > 0 across the array."""
        result = pdf_info(sample_pdf, detail=True)
        chars = result["text_coverage"]["text_chars_per_page"]
        assert all(c > 0 for c in chars)

    def test_text_coverage_image_only_pages(self, sample_pdf_scanned, isolated_server):
        """Image-only pages: text_chars == 0, raster > 0; summary reflects them."""
        result = pdf_info(sample_pdf_scanned, detail=True)
        cov = result["text_coverage"]
        assert cov["text_chars_per_page"][0] == 0
        assert cov["raster_images_per_page"][0] > 0
        assert cov["summary"]["pages_with_only_images"] >= 1
        # OCR candidate listing surfaces this page (1-indexed)
        assert 1 in cov["summary"]["ocr_candidate_pages"]

    def test_text_coverage_summary_counts(self, sample_pdf, isolated_server):
        """Summary rollups equal direct counts over the parallel arrays."""
        result = pdf_info(sample_pdf, detail=True)
        cov = result["text_coverage"]
        chars = cov["text_chars_per_page"]
        raster = cov["raster_images_per_page"]
        assert cov["summary"]["pages_with_text"] == sum(1 for c in chars if c > 0)
        assert cov["summary"]["total_text_chars"] == sum(chars)
        assert cov["summary"]["pages_with_raster_images"] == sum(
            1 for r in raster if r > 0
        )

    def test_text_coverage_summary_independent_of_detail(
        self, sample_pdf, isolated_server
    ):
        """The summary section is identical whether detail is requested or not."""
        default_summary = pdf_info(sample_pdf)["text_coverage"]["summary"]
        detailed_summary = pdf_info(sample_pdf, detail=True)["text_coverage"]["summary"]
        assert default_summary == detailed_summary

    def test_text_coverage_cached_on_second_call(self, sample_pdf, isolated_server):
        """Second pdf_info call returns same coverage from cache."""
        r1 = pdf_info(sample_pdf, detail=True)
        r2 = pdf_info(sample_pdf, detail=True)
        assert r2["from_cache"] is True
        assert r2["text_coverage"] == r1["text_coverage"]

    def test_text_coverage_lazy_backfill(self, sample_pdf, isolated_server):
        """Existing cached row with no coverage gets backfilled on next pdf_info."""
        import pdf_mcp.server as srv

        # Manually save metadata without coverage to simulate pre-v1.9.0 cache
        srv.cache.save_metadata(sample_pdf, 5, {}, [], text_coverage=None)
        result = pdf_info(sample_pdf, detail=True)
        cov = result["text_coverage"]
        assert cov is not None
        assert len(cov["text_chars_per_page"]) == 5

    def test_pdf_info_cold_500_page_under_2s(self, isolated_server, tmp_path):
        """Cold pdf_info on a 500-page PDF completes under 2 seconds."""
        import time
        import pymupdf as _pymupdf

        pdf_path = str(tmp_path / "big.pdf")
        doc = _pymupdf.open()
        for i in range(500):
            page = doc.new_page()
            page.insert_text((50, 50), f"Page {i + 1} content here.")
        doc.save(pdf_path)
        doc.close()

        start = time.monotonic()
        pdf_info(pdf_path)
        elapsed = time.monotonic() - start
        assert elapsed < 2.0, f"pdf_info took {elapsed:.2f}s on 500-page PDF"


class TestPdfReadPagesRender:
    """Tests for render_dpi parameter on pdf_read_pages."""

    def test_render_dpi_adds_render_id(self, sample_pdf, isolated_server):
        """render_dpi set -> each page dict has opaque render_id (basename only)."""
        import pdf_mcp.server as srv

        result = pdf_read_pages(sample_pdf, "1", render_dpi=72)
        page = result["pages"][0]
        assert "render_id" in page
        assert "render_path" not in page  # absolute path no longer on the wire
        assert "/" not in page["render_id"]
        assert "\\" not in page["render_id"]
        assert (srv.cache.renders_dir / page["render_id"]).exists()

    def test_render_dpi_adds_render_size_bytes(self, sample_pdf, isolated_server):
        """render_dpi set -> each page dict has render_size_bytes > 0."""
        result = pdf_read_pages(sample_pdf, "1", render_dpi=72)
        assert result["pages"][0]["render_size_bytes"] > 0

    def test_render_id_resolves_under_renders_dir(self, sample_pdf, isolated_server):
        """Rendered PNG (resolved via renders_dir) lives under renders_dir."""
        import pdf_mcp.server as srv

        result = pdf_read_pages(sample_pdf, "1", render_dpi=72)
        render_path = srv.cache.renders_dir / result["pages"][0]["render_id"]
        assert render_path.exists()
        assert srv.cache.renders_dir in render_path.parents

    def test_render_dpi_response_includes_dpi_fields(self, sample_pdf, isolated_server):
        """Response includes render_dpi_used and render_dpi_requested."""
        result = pdf_read_pages(sample_pdf, "1", render_dpi=200)
        assert result["render_dpi_used"] == 200
        assert result["render_dpi_requested"] == 200

    def test_render_dpi_clamped_high(self, sample_pdf, isolated_server):
        """render_dpi above 400 is clamped to 400."""
        result = pdf_read_pages(sample_pdf, "1", render_dpi=1000)
        assert result["render_dpi_used"] == 400
        assert result["render_dpi_requested"] == 1000

    def test_render_dpi_clamped_low(self, sample_pdf, isolated_server):
        """render_dpi below 72 is clamped to 72."""
        result = pdf_read_pages(sample_pdf, "1", render_dpi=10)
        assert result["render_dpi_used"] == 72

    def test_render_dpi_cache_hit(self, sample_pdf, isolated_server):
        """Second call with same render_dpi hits the cache (no re-render)."""
        from unittest.mock import patch

        pdf_read_pages(sample_pdf, "1", render_dpi=72)  # first call — renders
        with patch("pdf_mcp.server.render_page_as_png") as mock_render:
            pdf_read_pages(sample_pdf, "1", render_dpi=72)  # cache hit
            mock_render.assert_not_called()

    def test_render_dpi_not_set_no_render_id(self, sample_pdf, isolated_server):
        """Without render_dpi, pages have no render_id key."""
        result = pdf_read_pages(sample_pdf, "1")
        page = result["pages"][0]
        assert "render_id" not in page
        assert "render_path" not in page  # legacy absolute-path key also absent
        assert "render_dpi_used" not in result

    def test_cache_clear_removes_render_png(self, sample_pdf, isolated_server):
        """pdf_cache_clear removes PNGs created by pdf_read_pages render_dpi."""
        import pdf_mcp.server as srv

        result = pdf_read_pages(sample_pdf, "1", render_dpi=72)
        png_path = srv.cache.renders_dir / result["pages"][0]["render_id"]
        assert png_path.exists()
        pdf_cache_clear(expired_only=False)
        assert not png_path.exists()

    def test_no_absolute_paths_in_response(
        self, sample_pdf_with_images, isolated_server
    ):
        """Wire-format invariant: pdf_read_pages response carries no
        absolute filesystem paths. The image/render IDs are content-
        addressed basenames; absolute paths are unstable across runs
        and across PDF_MCP_CACHE_DIR changes, so they shouldn't be
        part of the public response shape."""
        import json

        result = pdf_read_pages(sample_pdf_with_images, "1", render_dpi=72)
        serialised = json.dumps(result)
        # No POSIX absolute paths.
        assert "/Users/" not in serialised
        assert "/home/" not in serialised
        assert "/tmp/" not in serialised
        assert "/var/" not in serialised
        # No Windows-style absolute paths.
        assert ":\\\\" not in serialised

    def test_bidirectional_cache_read_then_render_tool(
        self, sample_pdf, isolated_server
    ):
        """pdf_read_pages(render_dpi=72) populates cache; pdf_render_pages is a hit."""
        from unittest.mock import patch

        pdf_read_pages(sample_pdf, "1", render_dpi=72)
        # pdf_render_pages at same DPI should hit the cache
        with patch("pdf_mcp.server.render_page_as_png") as mock_render:
            from pdf_mcp.server import pdf_render_pages

            pdf_render_pages(sample_pdf, "1", dpi=72)
            mock_render.assert_not_called()


class TestPdfRenderPages:
    """Tests for pdf_render_pages tool."""

    def test_returns_list(self, sample_pdf, isolated_server):
        """pdf_render_pages returns a list (not a dict)."""
        result = pdf_render_pages(sample_pdf, "1", dpi=72)
        assert isinstance(result, list)

    def test_first_element_is_summary_dict(self, sample_pdf, isolated_server):
        """First list element is a dict with pages_rendered and dpi_used."""
        result = pdf_render_pages(sample_pdf, "1", dpi=72)
        summary = result[0]
        assert isinstance(summary, dict)
        assert "pages_rendered" in summary
        assert "dpi_used" in summary
        assert 1 in summary["pages_rendered"]

    def test_subsequent_elements_are_images(self, sample_pdf, isolated_server):
        """Elements after the summary are MCP ImageContent blocks."""
        from mcp.types import ImageContent

        result = pdf_render_pages(sample_pdf, "1", dpi=72)
        assert len(result) == 2  # summary + 1 image
        assert isinstance(result[1], ImageContent)

    def test_dpi_clamped(self, sample_pdf, isolated_server):
        """DPI above 400 is clamped; dpi_requested vs dpi_used differ."""
        result = pdf_render_pages(sample_pdf, "1", dpi=1000)
        summary = result[0]
        assert summary["dpi_used"] == 400
        assert summary["dpi_requested"] == 1000

    def test_max_inline_pages_truncation(self, sample_pdf, isolated_server):
        """Requesting more than MAX_RENDER_INLINE_PAGES returns truncated_render."""
        import pdf_mcp.server as srv
        from mcp.types import ImageContent

        original = srv.MAX_RENDER_INLINE_PAGES
        srv.MAX_RENDER_INLINE_PAGES = 2
        try:
            result = pdf_render_pages(sample_pdf, "1-5", dpi=72)
        finally:
            srv.MAX_RENDER_INLINE_PAGES = original
        image_count = sum(1 for x in result if isinstance(x, ImageContent))
        assert image_count == 2
        assert result[0].get("truncated_render") is True

    def test_does_not_have_ocr_parameter(self):
        """pdf_render_pages does not accept ocr parameter — tools are orthogonal."""
        import inspect

        sig = inspect.signature(pdf_render_pages)
        assert "ocr" not in sig.parameters

    def test_bidirectional_cache_render_tool_then_read_pages(
        self, sample_pdf, isolated_server
    ):
        """pdf_render_pages populates cache; pdf_read_pages(render_dpi=72) is a hit."""
        from unittest.mock import patch

        pdf_render_pages(sample_pdf, "1", dpi=72)
        with patch("pdf_mcp.server.render_page_as_png") as mock_render:
            pdf_read_pages(sample_pdf, "1", render_dpi=72)
            mock_render.assert_not_called()

    def test_rendering_does_not_run_ocr(self, sample_pdf_scanned, isolated_server):
        """Calling pdf_render_pages does not make pages searchable via pdf_search."""
        pdf_render_pages(sample_pdf_scanned, "1", dpi=72)
        # sample_pdf_scanned has no extractable text
        result = pdf_search(sample_pdf_scanned, "the", mode="keyword")
        assert result["total_matches"] == 0

    def test_docstring_mentions_vision_models(self):
        """Tool docstring explicitly mentions vision models."""
        assert "vision" in pdf_render_pages.__doc__.lower()

    def test_invalid_pages_returns_error_in_summary(self, sample_pdf, isolated_server):
        """Out-of-range pages returns error in summary dict; no images appended."""
        result = pdf_render_pages(sample_pdf, "100", dpi=72)
        assert result[0]["error"]
        assert len(result) == 1  # images list is empty in error case

    def test_image_blocks_carry_page_in_meta(self, sample_pdf, isolated_server):
        """Each image content block carries its page number in MCP _meta."""
        result = pdf_render_pages(sample_pdf, "1-2", dpi=72)
        blocks = result[1:]
        assert len(blocks) == 2
        assert blocks[0].meta == {"page": 1}
        assert blocks[1].meta == {"page": 2}

    def test_pages_rendered_aligns_with_image_blocks(self, sample_pdf, isolated_server):
        """Lockstep invariant: pages_rendered[i] == _meta['page'] of result[i+1]."""
        result = pdf_render_pages(sample_pdf, "1-3", dpi=72)
        summary = result[0]
        blocks = result[1:]
        assert len(blocks) == len(summary["pages_rendered"])
        for i, block in enumerate(blocks):
            assert block.meta["page"] == summary["pages_rendered"][i]

    def test_invariant_holds_when_one_page_read_fails(
        self, sample_pdf, isolated_server
    ):
        """OSError on page 2 must not misalign pages_rendered or image _meta."""
        from pathlib import Path
        from unittest.mock import patch

        real_read_bytes = Path.read_bytes
        call_count = {"n": 0}

        def fake_read_bytes(self):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise OSError("simulated disk failure")
            return real_read_bytes(self)

        with patch.object(Path, "read_bytes", fake_read_bytes):
            result = pdf_render_pages(sample_pdf, "1-3", dpi=72)

        summary = result[0]
        blocks = result[1:]
        assert summary["pages_rendered"] == [1, 3]
        assert summary["render_failed_pages"] == [2]
        assert len(blocks) == 2
        assert blocks[0].meta["page"] == 1
        assert blocks[1].meta["page"] == 3


class TestPdfReadPagesOcr:
    """Tests for ocr and ocr_lang parameters on pdf_read_pages."""

    def test_ocr_error_when_tesseract_missing(self, sample_pdf, isolated_server):
        """ocr=True returns error dict when Tesseract not installed."""
        from unittest.mock import patch

        with patch(
            "pdf_mcp.server.check_tesseract_available",
            side_effect=RuntimeError("Tesseract not found."),
        ):
            result = pdf_read_pages(sample_pdf, "1", ocr=True)
        assert "error" in result
        assert "install_hint" in result

    def test_ocr_error_before_path_resolution(self, isolated_server):
        """Tesseract check fires before path resolution (no FileNotFoundError)."""
        from unittest.mock import patch

        with patch(
            "pdf_mcp.server.check_tesseract_available",
            side_effect=RuntimeError("Tesseract not found."),
        ):
            result = pdf_read_pages("/nonexistent/file.pdf", "1", ocr=True)
        assert "error" in result
        assert "install_hint" in result

    def test_ocr_false_no_source_in_page(self, sample_pdf, isolated_server):
        """Without ocr=True, page dicts have no 'source' key."""
        result = pdf_read_pages(sample_pdf, "1")
        assert "source" not in result["pages"][0]

    def test_ocr_true_page_has_source(self, sample_pdf, isolated_server, monkeypatch):
        """ocr=True adds 'source' to each page dict."""
        from unittest.mock import patch

        monkeypatch.setenv("PDF_MCP_MAX_WORKERS", "1")
        with patch("pdf_mcp.server.check_tesseract_available"):
            with patch(
                "pdf_mcp.server._ocr_page_worker",
                side_effect=lambda args: (args[1], "ocr text here"),
            ):
                result = pdf_read_pages(sample_pdf, "1", ocr=True)
        assert "source" in result["pages"][0]

    def test_ocr_true_writes_source_ocr_to_cache(
        self, sample_pdf, isolated_server, monkeypatch
    ):
        """OCR result is stored with source='ocr' in cache."""
        import pdf_mcp.server as srv
        from unittest.mock import patch

        monkeypatch.setenv("PDF_MCP_MAX_WORKERS", "1")
        with patch("pdf_mcp.server.check_tesseract_available"):
            with patch(
                "pdf_mcp.server._ocr_page_worker",
                side_effect=lambda args: (args[1], "hello from ocr"),
            ):
                pdf_read_pages(sample_pdf, "1", ocr=True)
        source = srv.cache.get_page_source(sample_pdf, 0)
        assert source == "ocr"

    def test_ocr_cache_hit_does_not_re_ocr(
        self, sample_pdf, isolated_server, monkeypatch
    ):
        """Second ocr=True call does not re-run OCR if source='ocr' cached."""
        from unittest.mock import patch, MagicMock

        monkeypatch.setenv("PDF_MCP_MAX_WORKERS", "1")
        mock_worker = MagicMock(side_effect=lambda args: (args[1], "ocr result"))
        with patch("pdf_mcp.server.check_tesseract_available"):
            with patch("pdf_mcp.server._ocr_page_worker", mock_worker):
                pdf_read_pages(sample_pdf, "1", ocr=True)
                call_count_first = mock_worker.call_count
                pdf_read_pages(sample_pdf, "1", ocr=True)
                call_count_second = mock_worker.call_count
        assert call_count_second == call_count_first  # not called again

    def test_ocr_empty_result_cached_and_not_retriggered(
        self, sample_pdf, isolated_server, monkeypatch
    ):
        """Empty OCR result is cached as source='ocr'; subsequent call skips re-OCR."""
        import pdf_mcp.server as srv
        from unittest.mock import patch, MagicMock

        monkeypatch.setenv("PDF_MCP_MAX_WORKERS", "1")
        mock_worker = MagicMock(side_effect=lambda args: (args[1], ""))
        with patch("pdf_mcp.server.check_tesseract_available"):
            with patch("pdf_mcp.server._ocr_page_worker", mock_worker):
                pdf_read_pages(sample_pdf, "1", ocr=True)
                assert srv.cache.get_page_source(sample_pdf, 0) == "ocr"
                pdf_read_pages(sample_pdf, "1", ocr=True)
        assert mock_worker.call_count == 1  # not called a second time

    def test_ocr_skip_page_with_native_text(
        self, sample_pdf, isolated_server, monkeypatch
    ):
        """Page with source='extracted' and non-empty text is not re-OCR'd."""
        import pdf_mcp.server as srv
        from unittest.mock import patch, MagicMock

        monkeypatch.setenv("PDF_MCP_MAX_WORKERS", "1")
        srv.cache.save_page_text(sample_pdf, 0, "native text here", source="extracted")
        mock_worker = MagicMock(
            side_effect=lambda args: (args[1], "should not be called")
        )
        with patch("pdf_mcp.server.check_tesseract_available"):
            with patch("pdf_mcp.server._ocr_page_worker", mock_worker):
                pdf_read_pages(sample_pdf, "1", ocr=True)
        mock_worker.assert_not_called()

    def test_ocr_max_pages_limit_truncation(
        self, sample_pdf, isolated_server, monkeypatch
    ):
        """Requesting more than MAX_OCR_PAGES_LIMIT pages sets truncated_ocr."""
        from unittest.mock import patch
        import pdf_mcp.server as srv

        monkeypatch.setenv("PDF_MCP_MAX_WORKERS", "1")  # force sequential (no pickle)
        original = srv.MAX_OCR_PAGES_LIMIT
        srv.MAX_OCR_PAGES_LIMIT = 2
        try:
            with patch("pdf_mcp.server.check_tesseract_available"):
                with patch(
                    "pdf_mcp.server._ocr_page_worker",
                    side_effect=lambda args: (args[1], "text"),
                ):
                    result = pdf_read_pages(sample_pdf, "1-5", ocr=True)
        finally:
            srv.MAX_OCR_PAGES_LIMIT = original
        assert result.get("truncated_ocr") is True
        assert len(result["pages"]) == 2

    def test_ocr_lang_passed_to_ocr_page(self, sample_pdf, isolated_server):
        """ocr_lang parameter is forwarded to _ocr_page_worker args."""
        from unittest.mock import patch

        captured = []

        def mock_worker(args):
            captured.append(args)
            return (args[1], "text")

        with patch("pdf_mcp.server.check_tesseract_available"):
            with patch("pdf_mcp.server._ocr_page_worker", mock_worker):
                pdf_read_pages(sample_pdf, "1", ocr=True, ocr_lang="fra")
        assert len(captured) == 1
        # args = (local_path, page_num, ocr_lang, dpi)
        assert captured[0][2] == "fra"

    def test_ocr_text_searchable_via_pdf_search(
        self, sample_pdf_scanned, isolated_server
    ):
        """OCR'd text is found by pdf_search after pdf_read_pages(ocr=True)."""
        from unittest.mock import patch

        with patch("pdf_mcp.server.check_tesseract_available"):
            with patch(
                "pdf_mcp.server._ocr_page_worker",
                side_effect=lambda args: (args[1], "the quick brown fox"),
            ):
                pdf_read_pages(sample_pdf_scanned, "1", ocr=True)
        result = pdf_search(sample_pdf_scanned, "fox", mode="keyword")
        assert result["total_matches"] >= 1


class TestPdfSearchSource:
    """Tests for source field on pdf_search matches (v1.10.0)."""

    def test_search_match_has_source_field(self, sample_pdf, isolated_server):
        """Each search match includes a 'source' field."""
        result = pdf_search(sample_pdf, "page", mode="keyword")
        assert len(result["matches"]) > 0
        for match in result["matches"]:
            assert "source" in match

    def test_search_match_source_extracted_for_native_text(
        self, sample_pdf, isolated_server
    ):
        """Matches from native extraction have source='extracted'."""
        pdf_read_pages(sample_pdf, "1-5")  # populates page_text with source='extracted'
        result = pdf_search(sample_pdf, "page", mode="keyword")
        for match in result["matches"]:
            assert match["source"] == "extracted"

    def test_search_match_source_ocr_for_ocr_text(
        self, sample_pdf_scanned, isolated_server
    ):
        """Matches from OCR'd pages have source='ocr'."""
        import pdf_mcp.server as srv

        srv.cache.save_page_text(
            sample_pdf_scanned, 0, "ocr content here", source="ocr"
        )
        result = pdf_search(sample_pdf_scanned, "ocr", mode="keyword")
        assert len(result["matches"]) > 0
        assert result["matches"][0]["source"] == "ocr"


class TestPdfSearchGranularityValidation:
    """Granularity parameter validation. Section-mode dispatch is tested
    in a later task (P-C2)."""

    def test_default_granularity_preserves_page_behaviour(
        self, sample_pdf, isolated_server
    ):
        # Calling without `granularity` should behave exactly as before:
        # returns page-mode shape with `matches`, `search_mode`, etc.
        result = pdf_search(sample_pdf, "Sample")
        assert "matches" in result
        assert "search_mode" in result
        # Should NOT contain section-mode keys
        assert "sections" not in result

    def test_invalid_granularity_returns_error(self, sample_pdf, isolated_server):
        result = pdf_search(sample_pdf, "Sample", granularity="bogus")
        assert "error" in result
        assert "granularity" in result["error"].lower()

    def test_explicit_granularity_page_works(self, sample_pdf, isolated_server):
        # Explicit granularity="page" should work identically to default
        result = pdf_search(sample_pdf, "Sample", granularity="page")
        assert "matches" in result
        assert "sections" not in result


class TestPdfSearchSectionMode:
    """Section-mode dispatch: TOC-first index, BM25 ranking."""

    def test_returns_sections_shape(
        self, isolated_server, sample_pdf_with_toc_sections
    ):
        result = pdf_search(
            sample_pdf_with_toc_sections, "graph attention", granularity="section"
        )
        assert "sections" in result
        assert result["search_mode"] == "section"
        # No page-mode keys leak through
        assert "matches" not in result

    def test_returns_ranked_sections(
        self, isolated_server, sample_pdf_with_toc_sections
    ):
        result = pdf_search(
            sample_pdf_with_toc_sections, "graph attention", granularity="section"
        )
        sections = result["sections"]
        assert len(sections) >= 1
        # "Methods" body has the strongest match for "graph attention"
        assert sections[0]["title"] == "Methods"
        # Each section dict has the expected keys
        sec = sections[0]
        for key in ("section_id", "title", "start_page", "end_page", "score"):
            assert key in sec

    def test_no_keyword_match_returns_empty_sections(
        self, isolated_server, sample_pdf_with_toc_sections
    ):
        result = pdf_search(
            sample_pdf_with_toc_sections,
            "zebra octopus xylophone",
            granularity="section",
        )
        assert result["sections"] == []
        assert result["search_mode"] == "section"

    def test_title_source_is_toc_when_pdf_has_toc(
        self, isolated_server, sample_pdf_with_toc_sections
    ):
        """title_source == "toc" for sections derived from PyMuPDF's TOC.

        Regression: an earlier implementation derived title_source from
        the cached pdf_metadata, which meant a pdf_search call BEFORE
        pdf_info populated the metadata cache would report
        title_source="heading_detected" on every match — even when
        derive_sections actually took the TOC path. The fix records
        title_source on the Section dataclass at detection time and
        persists it on the FTS row, so the field is correct regardless
        of call order.
        """
        # Call section search FIRST — pdf_info has not run yet, so the
        # metadata cache is empty for this path. The fix should still
        # report "toc" because derive_sections takes the TOC path.
        result = pdf_search(
            sample_pdf_with_toc_sections, "graph", granularity="section"
        )
        assert result["sections"], "fixture should produce matches"
        for sec in result["sections"]:
            assert "title_source" in sec
            assert sec["title_source"] == "toc"
            assert sec["title"] is not None

    def test_total_sections_reflects_indexed_count(
        self, isolated_server, sample_pdf_with_toc_sections
    ):
        result = pdf_search(
            sample_pdf_with_toc_sections, "graph", granularity="section"
        )
        # The fixture has 5 TOC entries -> 5 sections indexed
        assert result["total_sections"] == 5

    def test_caches_after_first_call(
        self, isolated_server, sample_pdf_with_toc_sections
    ):
        # First call populates the cache; second call should reuse it.
        # Both should return the same shape.
        r1 = pdf_search(sample_pdf_with_toc_sections, "graph", granularity="section")
        r2 = pdf_search(sample_pdf_with_toc_sections, "graph", granularity="section")
        assert r1["total_sections"] == r2["total_sections"]
        assert [s["title"] for s in r1["sections"]] == [
            s["title"] for s in r2["sections"]
        ]


class TestSearchResponseModelField:
    """pdf_search response includes model field on semantic/hybrid paths."""

    def _make_encode(self, dim: int = 4):
        def encode(texts, model_name="BAAI/bge-small-en-v1.5"):
            return np.ones((len(texts), dim), dtype=np.float32)

        def encode_query(text, model_name="BAAI/bge-small-en-v1.5"):
            return np.ones(dim, dtype=np.float32)

        return encode, encode_query

    def test_semantic_response_has_model_field(self, sample_pdf, isolated_server):
        """mode='semantic' response includes model field with configured model name."""
        from pdf_mcp.server import pdf_search

        encode, encode_query = self._make_encode()

        with (
            patch("pdf_mcp.embedder.check_available"),
            patch("pdf_mcp.embedder.encode", encode),
            patch("pdf_mcp.embedder.encode_query", encode_query),
        ):
            result = pdf_search(sample_pdf, "test", mode="semantic")

        assert "model" in result
        assert result["model"] == "BAAI/bge-small-en-v1.5"

    def test_hybrid_response_has_model_field(self, sample_pdf, isolated_server):
        """mode='auto' with fastembed returns model field."""
        from pdf_mcp.server import pdf_search

        encode, encode_query = self._make_encode()

        with (
            patch("pdf_mcp.embedder.check_available"),
            patch("pdf_mcp.embedder.encode", encode),
            patch("pdf_mcp.embedder.encode_query", encode_query),
        ):
            result = pdf_search(sample_pdf, "test", mode="auto")

        if result.get("search_mode") == "hybrid":
            assert "model" in result
            assert result["model"] == "BAAI/bge-small-en-v1.5"

    def test_auto_mode_invalid_model_returns_error(self, sample_pdf, isolated_server):
        """mode='auto' with invalid model name propagates ValueError as error."""
        from pdf_mcp.server import pdf_search

        with patch(
            "pdf_mcp.embedder.check_available",
            side_effect=ValueError("Unknown embedding model 'bad-model'"),
        ):
            result = pdf_search(sample_pdf, "page", mode="auto")

        assert "error" in result


class TestCacheStatsEmbeddingModel:
    """pdf_cache_stats includes embedding_model field."""

    def test_cache_stats_has_embedding_model(self, isolated_server):
        """pdf_cache_stats response includes embedding_model key."""
        from pdf_mcp.server import pdf_cache_stats

        result = pdf_cache_stats()

        assert "embedding_model" in result
        assert result["embedding_model"] == "BAAI/bge-small-en-v1.5"


class TestExcerptStyle:
    """Tests for excerpt_style parameter in pdf_search."""

    def test_invalid_excerpt_style_returns_error(self, sample_pdf, isolated_server):
        result = pdf_search(sample_pdf, "content", excerpt_style="bogus")
        assert "error" in result
        assert "excerpt_style" in result["error"]

    def test_default_excerpt_style_is_paragraph(self, sample_pdf, isolated_server):
        result = pdf_search(sample_pdf, "content")
        assert result.get("excerpt_style") == "paragraph"

    def test_explicit_snippet_style(self, sample_pdf, isolated_server):
        result = pdf_search(sample_pdf, "content", excerpt_style="snippet")
        assert "error" not in result

    def test_keyword_paragraph_excerpt_contains_query_terms(
        self, sample_pdf, isolated_server
    ):
        """Paragraph excerpt must contain at least one query term."""
        result = pdf_search(
            sample_pdf, "content", mode="keyword", excerpt_style="paragraph"
        )
        assert "error" not in result
        assert result["excerpt_style"] == "paragraph"
        assert len(result["matches"]) > 0
        for m in result["matches"]:
            assert "content" in m["excerpt"].lower()

    def test_paragraph_picks_correct_block_with_repeated_terms(self, isolated_server):
        """Regression: on a page with multiple blocks sharing a term,
        paragraph mode must pick the block with the MOST query-term
        overlap, not the first block on the page."""
        import tempfile
        import pymupdf
        from pathlib import Path

        doc = pymupdf.open()
        page = doc.new_page()
        # Block 0: shares "engineering" but not the distinguishing terms
        page.insert_text((50, 50), "Define the constructs of engineering.")
        # Block 1: the target — has "engineering" AND "best practices"
        page.insert_text(
            (50, 200),
            "Identify best practices for engineering improvement.",
        )
        # Block 2: unrelated
        page.insert_text((50, 350), "Unrelated content about cooking.")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            doc.save(f.name)
            doc.close()
            path = str(Path(f.name).resolve())
            try:
                result = pdf_search(
                    path,
                    "best practices engineering",
                    mode="keyword",
                    excerpt_style="paragraph",
                )
                assert "error" not in result
                assert len(result["matches"]) > 0
                excerpt = result["matches"][0]["excerpt"].lower()
                assert "best practices" in excerpt
            finally:
                os.unlink(path)

    def test_upgrade_deduplicates_same_block(self, isolated_server):
        """_upgrade_excerpts_to_paragraphs collapses matches in the same block."""
        import pymupdf
        from pdf_mcp.server import _upgrade_excerpts_to_paragraphs
        import tempfile

        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((50, 50), "alpha beta gamma delta")
        page.insert_text((50, 200), "epsilon zeta eta theta")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            doc.save(f.name)
            doc.close()
            doc2 = pymupdf.open(f.name)
            # Simulate two matches on page 1 whose snippets land in block 0
            fake_matches = [
                {"page": 1, "excerpt": "alpha beta", "score": 0.9, "position": 0},
                {"page": 1, "excerpt": "beta gamma", "score": 0.8, "position": 6},
            ]
            upgraded = _upgrade_excerpts_to_paragraphs(fake_matches, doc2, "alpha")
            # Both snippets are in block 0 → deduped to one match
            assert len(upgraded) == 1
            assert upgraded[0]["score"] == 0.9  # kept higher score
            doc2.close()
            os.unlink(f.name)

    def test_keyword_explicit_snippet_mode(self, sample_pdf, isolated_server):
        """Explicit snippet mode works and sets excerpt_style='snippet'."""
        result = pdf_search(
            sample_pdf, "content", mode="keyword", excerpt_style="snippet"
        )
        assert "error" not in result
        assert len(result["matches"]) > 0
        assert result.get("excerpt_style") == "snippet"

    @staticmethod
    def _make_encode(dim: int = 384):
        import numpy as np

        def encode(texts, model_name="BAAI/bge-small-en-v1.5"):
            result = np.zeros((len(texts), dim), dtype=np.float32)
            for i in range(len(texts)):
                result[i, i % dim] = 1.0
            return result

        def encode_query(text, model_name="BAAI/bge-small-en-v1.5"):
            v = np.zeros(dim, dtype=np.float32)
            v[0] = 1.0
            return v

        return encode, encode_query

    def test_semantic_paragraph_excerpt_contains_query_terms(
        self, sample_pdf, isolated_server
    ):
        """Semantic paragraph excerpt must contain at least one query term."""
        encode, encode_query = self._make_encode()
        with (
            patch("pdf_mcp.embedder.check_available"),
            patch("pdf_mcp.embedder.encode", encode),
            patch("pdf_mcp.embedder.encode_query", encode_query),
        ):
            result = pdf_search(
                sample_pdf, "content", mode="semantic", excerpt_style="paragraph"
            )
            assert "error" not in result
            assert result.get("excerpt_style") == "paragraph"
            assert len(result["matches"]) > 0
            for m in result["matches"]:
                assert "content" in m["excerpt"].lower()

    def test_hybrid_paragraph_excerpt_contains_query_terms(
        self, sample_pdf, isolated_server
    ):
        """Hybrid paragraph excerpt must contain at least one query term."""
        encode, encode_query = self._make_encode()
        with (
            patch("pdf_mcp.embedder.check_available"),
            patch("pdf_mcp.embedder.encode", encode),
            patch("pdf_mcp.embedder.encode_query", encode_query),
        ):
            result = pdf_search(
                sample_pdf, "content", mode="auto", excerpt_style="paragraph"
            )
            assert "error" not in result
            assert result.get("excerpt_style") == "paragraph"
            for m in result["matches"]:
                assert "content" in m["excerpt"].lower()

    def test_python_fallback_paragraph_mode(self, isolated_server):
        """When FTS5 is unavailable, python fallback also supports paragraph mode."""
        import pymupdf
        from pathlib import Path

        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((50, 50), "The quick brown fox jumps.")
        page.insert_text((50, 200), "Lazy dog sleeps all day.")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            doc.save(f.name)
            doc.close()
            path = str(Path(f.name).resolve())
            try:
                cache, _ = isolated_server
                orig = cache.fts_available
                cache.fts_available = False
                result = pdf_search(
                    path, "fox", mode="keyword", excerpt_style="paragraph"
                )
                cache.fts_available = orig
                assert "error" not in result
                if result["matches"]:
                    assert result.get("excerpt_style") == "paragraph"
            finally:
                os.unlink(path)

    def test_section_granularity_ignores_excerpt_style(
        self, sample_pdf, isolated_server
    ):
        """Section mode ignores excerpt_style — no error, no excerpt_style key."""
        result = pdf_search(
            sample_pdf, "content", granularity="section", excerpt_style="paragraph"
        )
        assert "error" not in result
        assert "excerpt_style" not in result

    def test_auto_keyword_fallback_paragraph_mode(self, sample_pdf, isolated_server):
        """Auto mode falling back to keyword still applies paragraph upgrade."""
        with patch("pdf_mcp.embedder.check_available", side_effect=ImportError):
            result = pdf_search(
                sample_pdf, "content", mode="auto", excerpt_style="paragraph"
            )
        assert "error" not in result
        assert result.get("search_mode") == "keyword"
        assert result.get("excerpt_style") == "paragraph"

    def test_hybrid_keyword_excerpt_anchors_block_selection(self, isolated_server):
        """In hybrid mode, keyword excerpt anchors paragraph to the
        block containing the FTS5 snippet, not the first block with
        the most token overlap."""
        from pdf_mcp.server import _upgrade_excerpts_to_paragraphs
        import tempfile
        import pymupdf

        doc = pymupdf.open()
        page = doc.new_page()
        # Block 0: has "alpha" but not "beta"
        page.insert_text((50, 50), "alpha concepts and constructs overview")
        # Block 1: has "alpha" AND "beta" — the FTS5 snippet came from here
        page.insert_text((50, 200), "alpha beta best practices for improvement")
        # Block 2: unrelated
        page.insert_text((50, 350), "unrelated content about cooking")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            doc.save(f.name)
            doc.close()
            doc2 = pymupdf.open(f.name)
            fake_matches = [
                {"page": 1, "excerpt": "alpha beta", "score": 0.9},
            ]
            upgraded = _upgrade_excerpts_to_paragraphs(
                fake_matches,
                doc2,
                "alpha",
                keyword_excerpts={0: "alpha beta"},
            )
            assert len(upgraded) == 1
            assert "beta" in upgraded[0]["excerpt"].lower()
            doc2.close()
            os.unlink(f.name)

    def test_keyword_excerpt_not_found_falls_back_to_token_overlap(
        self, isolated_server
    ):
        """When the FTS5 snippet doesn't appear verbatim in any block,
        falls back to get_best_paragraph_for_query."""
        from pdf_mcp.server import _upgrade_excerpts_to_paragraphs
        import tempfile
        import pymupdf

        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((50, 50), "alpha gamma delta")
        page.insert_text((50, 200), "epsilon zeta eta")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            doc.save(f.name)
            doc.close()
            doc2 = pymupdf.open(f.name)
            fake_matches = [
                {"page": 1, "excerpt": "snippet", "score": 0.5},
            ]
            # keyword_excerpts has text that doesn't appear in any block
            upgraded = _upgrade_excerpts_to_paragraphs(
                fake_matches,
                doc2,
                "alpha gamma",
                keyword_excerpts={0: "nonexistent snippet text"},
            )
            assert len(upgraded) == 1
            # Falls back to token overlap — picks block with "alpha gamma"
            assert "alpha" in upgraded[0]["excerpt"].lower()
            doc2.close()
            os.unlink(f.name)

    def test_short_block_skipped_in_favor_of_body_paragraph(self, isolated_server):
        """Heading/caption blocks under the minimum-length floor are
        skipped; the picker retries with the floor and finds a
        substantive body block instead."""
        from pdf_mcp.server import _upgrade_excerpts_to_paragraphs
        import tempfile
        import pymupdf

        doc = pymupdf.open()
        page = doc.new_page()
        # Block 0: short heading (< 80 chars) — has "attention"
        page.insert_text((50, 50), "Scaled Dot-Product Attention")
        # Block 1: body paragraph (> 80 chars) — also has "attention"
        page.insert_text(
            (50, 200),
            (
                "The attention mechanism computes a weighted sum of"
                " values based on the compatibility of a query with"
                " the corresponding keys using scaled dot products."
            ),
        )
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            doc.save(f.name)
            doc.close()
            doc2 = pymupdf.open(f.name)
            fake_matches = [
                {"page": 1, "excerpt": "attention", "score": 0.5},
            ]
            upgraded = _upgrade_excerpts_to_paragraphs(fake_matches, doc2, "attention")
            assert len(upgraded) == 1
            excerpt = upgraded[0]["excerpt"]
            # Must pick the body paragraph, not the heading
            assert len(excerpt) > 80
            assert "weighted sum" in excerpt.lower()
            doc2.close()
            os.unlink(f.name)


class TestOcrParallelOrchestration:
    def _two_page_scanned(self, tmp_path):
        import base64
        import pymupdf

        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
            "AAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
        )
        path = str(tmp_path / "scanned2.pdf")
        doc = pymupdf.open()
        for _ in range(2):
            page = doc.new_page()
            page.insert_image(pymupdf.Rect(50, 50, 400, 600), stream=png)
        doc.save(path)
        doc.close()
        return path

    def test_ocr_failure_is_isolated_and_not_cached(
        self, isolated_server, tmp_path, monkeypatch
    ):
        cache_instance, _ = isolated_server
        monkeypatch.setenv("PDF_MCP_MAX_WORKERS", "1")  # force sequential path
        path = self._two_page_scanned(tmp_path)

        import pdf_mcp.server as srv

        monkeypatch.setattr(
            srv,
            "_ocr_page_worker",
            lambda args: (args[1], PageError("RuntimeError('ocr exploded')")),
        )

        result = pdf_read_pages(path, "1-2", ocr=True)
        assert "error" not in result
        sources = [p.get("source") for p in result["pages"]]
        assert sources == ["ocr_failed", "ocr_failed"]
        assert all(p["text"] == "" for p in result["pages"])
        # Failure must NOT be cached -> page source still absent.
        assert cache_instance.get_pages_source(path, [0, 1]) == {}

    def test_ocr_response_shape_unchanged(self, isolated_server, tmp_path, monkeypatch):
        monkeypatch.setenv("PDF_MCP_MAX_WORKERS", "1")
        path = self._two_page_scanned(tmp_path)
        result = pdf_read_pages(path, "1-2", ocr=True)
        assert set(["pages", "total_chars", "cache_hits", "cache_misses"]).issubset(
            result.keys()
        )
        assert len(result["pages"]) == 2
