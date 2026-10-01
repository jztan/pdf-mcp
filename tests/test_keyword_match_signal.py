"""`keyword_match` on every keyword-mode search response.

A keyword query of three or more words that no page fully matches is
retried with its terms OR-joined. Before this field, nothing in the
response said so: "Microsoft cloud revenue zyzzyvaword" returned hits that
looked exactly like hits for a query every page term matched. Every
keyword-mode response from `pdf_search` and `pdf_corpus_search` now says
'full', 'partial' or 'none'. Ranking is unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from pdf_mcp.server import pdf_corpus_search, pdf_corpus_warm, pdf_search

FILLER = "The committee reviewed the annual schedule and adjourned early."
PAGES = [
    "Glacier calving rates and glacier calving fronts were measured. " + FILLER,
    FILLER + " Ice shelf thinning was noted in the appendix.",
    FILLER,
]
PARTIAL_QUERY = "glacier calving zyzzyvaword"


def _write_pdf(path: Path, pages: list[str]) -> str:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(50, 50, 550, 800), text)
    doc.save(path)
    doc.close()
    return str(path.resolve())


@pytest.fixture
def fts_server(isolated_server):
    cache, _ = isolated_server
    if not cache.fts_available:
        pytest.skip("FTS5 not available in this SQLite build")
    return cache


@pytest.fixture
def one_pdf(tmp_path, fts_server):
    return _write_pdf(tmp_path / "glacier.pdf", PAGES)


@pytest.fixture
def corpus(tmp_path, fts_server):
    d = tmp_path / "corpus"
    d.mkdir()
    files = [
        _write_pdf(d / "a.pdf", PAGES),
        _write_pdf(d / "b.pdf", [FILLER + " Shelf ice notes.", FILLER]),
    ]
    pdf_corpus_warm(files, embeddings=False)
    return fts_server, d, files


def _no_private_keys(matches: list[dict]) -> None:
    for m in matches:
        assert not [k for k in m if k.startswith("_")], m


class TestPdfSearchKeywordMatch:
    def test_full_when_every_term_matched(self, one_pdf):
        res = pdf_search(one_pdf, "glacier calving", mode="keyword")
        assert res["matches"], res
        assert res["keyword_match"] == "full"

    def test_partial_when_the_or_retry_produced_the_hits(self, one_pdf):
        res = pdf_search(one_pdf, PARTIAL_QUERY, mode="keyword")
        assert res["matches"], "precondition: the OR retry finds the page"
        assert res["keyword_match"] == "partial"
        _no_private_keys(res["matches"])

    def test_partial_in_every_excerpt_style(self, one_pdf):
        for style in ("snippet", "paragraph", "window"):
            res = pdf_search(
                one_pdf, PARTIAL_QUERY, mode="keyword", excerpt_style=style
            )
            assert res["keyword_match"] == "partial", style
            _no_private_keys(res["matches"])

    def test_none_when_nothing_matched(self, one_pdf):
        res = pdf_search(one_pdf, "zyzzyvaword", mode="keyword")
        assert res["matches"] == []
        assert res["keyword_match"] == "none"

    def test_two_term_query_never_relaxes(self, one_pdf):
        """Two terms is a deliberate conjunction: no OR retry, so 'none'."""
        res = pdf_search(one_pdf, "glacier zyzzyvaword", mode="keyword")
        assert res["matches"] == []
        assert res["keyword_match"] == "none"

    def test_cold_document_reports_partial(self, tmp_path, fts_server):
        """First call on an uncached document takes the extract-then-index
        branch, not the pre-indexed one."""
        path = _write_pdf(tmp_path / "cold.pdf", PAGES)
        res = pdf_search(path, PARTIAL_QUERY, mode="keyword")
        assert res["keyword_match"] == "partial"
        again = pdf_search(path, PARTIAL_QUERY, mode="keyword")
        assert again["keyword_match"] == "partial"

    def test_auto_mode_degraded_to_keyword_carries_it(self, one_pdf, monkeypatch):
        from pdf_mcp import embedder

        def _missing(_model: str) -> None:
            raise ImportError("fastembed is not installed")

        monkeypatch.setattr(embedder, "check_available", _missing)
        res = pdf_search(one_pdf, PARTIAL_QUERY, mode="auto")
        assert res["search_mode"] == "keyword"
        assert res["semantic_unavailable"] is True
        assert res["keyword_match"] == "partial"
        _no_private_keys(res["matches"])

    def test_python_fallback_without_fts5(self, one_pdf, monkeypatch):
        """No FTS5: the Python matcher is AND-only, so 'full' or 'none'."""
        from pdf_mcp import _core

        monkeypatch.setattr(_core.cache, "fts_available", False)
        assert (
            pdf_search(one_pdf, "glacier calving", mode="keyword")["keyword_match"]
            == "full"
        )
        assert (
            pdf_search(one_pdf, PARTIAL_QUERY, mode="keyword")["keyword_match"]
            == "none"
        )


class TestCorpusSearchKeywordMatch:
    def test_full(self, corpus):
        _cache, d, _files = corpus
        res = pdf_corpus_search(str(d), "glacier calving", mode="keyword")
        assert res["matches"], res
        assert res["keyword_match"] == "full"

    def test_partial(self, corpus):
        _cache, d, _files = corpus
        res = pdf_corpus_search(str(d), PARTIAL_QUERY, mode="keyword")
        assert res["matches"], res
        assert res["keyword_match"] == "partial"
        _no_private_keys(res["matches"])

    def test_none(self, corpus):
        _cache, d, _files = corpus
        res = pdf_corpus_search(str(d), "zyzzyvaword", mode="keyword")
        assert res["matches"] == []
        assert res["keyword_match"] == "none"

    def test_partial_on_the_per_document_path(self, corpus):
        """A document missing from the shared index sends corpus search to
        the per-document arm, whose relaxed retry must report 'partial'
        too."""
        cache, d, files = corpus
        with cache._connect() as conn:
            conn.execute("DELETE FROM pdf_search_fts WHERE file_path = ?", (files[1],))
        res = pdf_corpus_search(str(d), PARTIAL_QUERY, mode="keyword")
        assert res["matches"], res
        assert res["keyword_match"] == "partial"
        _no_private_keys(res["matches"])

        full = pdf_corpus_search(str(d), "glacier calving", mode="keyword")
        assert full["keyword_match"] == "full"
