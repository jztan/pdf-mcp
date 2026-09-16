"""Per-process memo of the corpus warm verdict. Every pdf_corpus_search call
re-verifies that each document is fully warm, and that verification
re-chunks every page to check the stored unit layout: on a 100-document
corpus it was the largest per-query cost (profile 2026-09-17). A positive
verdict is memoised on (path, mtime, cache db, model, extraction version);
negatives are never memoised, so a warm that completes later is seen."""

import os

import pytest


class _Cache:
    """Minimal cache double: fully warm text, embeddings as configured."""

    def __init__(self, tmp_path, pages=2, emb_units=1):
        self.db_path = tmp_path / "cache.db"
        self.pages = pages
        self.emb_units = emb_units
        self.calls = {"get_metadata": 0}

    def get_metadata(self, path):
        self.calls["get_metadata"] += 1
        return {"page_count": self.pages, "text_coverage": {}}

    def get_pages_text(self, path, page_nums):
        return {p: "short page" for p in page_nums}

    def get_page_embeddings(self, path, page_nums, model):
        if self.emb_units == 0:
            return {}
        return {p: [b"x"] * self.emb_units for p in page_nums}


@pytest.fixture
def pdf(tmp_path):
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-1.4 stub")
    return str(p)


def test_positive_verdict_is_memoised(pdf, tmp_path):
    from pdf_mcp import corpus

    corpus.clear_warm_memo()
    c = _Cache(tmp_path)
    assert corpus._cached_pages(pdf, c, True, "m") == 2
    assert corpus._cached_pages(pdf, c, True, "m") == 2
    assert c.calls["get_metadata"] == 1


def test_negative_verdict_is_not_memoised(pdf, tmp_path):
    from pdf_mcp import corpus

    corpus.clear_warm_memo()
    c = _Cache(tmp_path, emb_units=0)
    assert corpus._cached_pages(pdf, c, True, "m") is None
    assert corpus._cached_pages(pdf, c, True, "m") is None
    assert c.calls["get_metadata"] == 2
    c.emb_units = 1  # warm completed in the meantime
    assert corpus._cached_pages(pdf, c, True, "m") == 2


def test_mtime_change_invalidates(pdf, tmp_path):
    from pdf_mcp import corpus

    corpus.clear_warm_memo()
    c = _Cache(tmp_path)
    assert corpus._cached_pages(pdf, c, True, "m") == 2
    os.utime(pdf, (1, 1))
    assert corpus._cached_pages(pdf, c, True, "m") == 2
    assert c.calls["get_metadata"] == 2


def test_key_includes_model_embeddings_flag_and_db(pdf, tmp_path):
    from pdf_mcp import corpus

    corpus.clear_warm_memo()
    c = _Cache(tmp_path)
    corpus._cached_pages(pdf, c, True, "m")
    corpus._cached_pages(pdf, c, True, "other-model")
    corpus._cached_pages(pdf, c, False, "m")
    other = _Cache(tmp_path / "other")
    (tmp_path / "other").mkdir()
    corpus._cached_pages(pdf, other, True, "m")
    assert c.calls["get_metadata"] == 3 and other.calls["get_metadata"] == 1


def test_forget_path_and_clear(pdf, tmp_path):
    from pdf_mcp import corpus

    corpus.clear_warm_memo()
    c = _Cache(tmp_path)
    corpus._cached_pages(pdf, c, True, "m")
    corpus.forget_warm_verdict(pdf)
    corpus._cached_pages(pdf, c, True, "m")
    assert c.calls["get_metadata"] == 2
    corpus.clear_warm_memo()
    corpus._cached_pages(pdf, c, True, "m")
    assert c.calls["get_metadata"] == 3


def test_missing_file_is_never_memoised(tmp_path):
    from pdf_mcp import corpus

    corpus.clear_warm_memo()
    c = _Cache(tmp_path)
    gone = str(tmp_path / "gone.pdf")
    assert corpus._cached_pages(gone, c, True, "m") == 2  # cache still answers
    assert corpus._cached_pages(gone, c, True, "m") == 2
    assert c.calls["get_metadata"] == 2
