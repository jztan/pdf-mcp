"""PDFs that need an open password return a stable password_required result."""

import shutil
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from pdf_mcp import _core
from pdf_mcp.backend.bytesopen import clear_lock_memo
from pdf_mcp.server import (
    pdf_corpus_overview,
    pdf_corpus_search,
    pdf_corpus_warm,
    pdf_extract_chart,
    pdf_get_toc,
    pdf_info,
    pdf_read_all,
    pdf_read_pages,
    pdf_render_pages,
    pdf_search,
)


@pytest.fixture(autouse=True)
def _fresh_memo():
    clear_lock_memo()
    yield
    clear_lock_memo()


SINGLE_FILE_CALLS = {
    "pdf_info": lambda p: pdf_info(p),
    "pdf_get_toc": lambda p: pdf_get_toc(p),
    "pdf_read_pages": lambda p: pdf_read_pages(p, pages="1"),
    "pdf_read_all": lambda p: pdf_read_all(p),
    "pdf_search": lambda p: pdf_search(p, query="late fee", mode="keyword"),
    "pdf_render_pages": lambda p: pdf_render_pages(p, pages="1"),
    "pdf_extract_chart": lambda p: pdf_extract_chart(p, page=1),
}
LIST_TOOLS = {"pdf_render_pages", "pdf_extract_chart"}


def _unwrap(tool, result):
    if tool in LIST_TOOLS:
        assert isinstance(result, list) and len(result) == 1
        return result[0]
    assert isinstance(result, dict)
    return result


@pytest.mark.parametrize("tool", sorted(SINGLE_FILE_CALLS))
def test_single_file_tools_return_password_required(tool, locked_pdf, isolated_server):
    result = _unwrap(tool, SINGLE_FILE_CALLS[tool](str(locked_pdf)))
    assert result["error_code"] == "password_required"
    assert result["error"] == f"PDF is password-protected: {locked_pdf}"
    assert "qpdf --decrypt" in result["hint"]
    assert "PDFium" not in result["error"]


def test_search_error_keeps_query(locked_pdf, isolated_server):
    result = pdf_search(str(locked_pdf), query="late fee", mode="keyword")
    assert result["error_code"] == "password_required"
    assert result["query"] == "late fee"


def test_locked_calls_write_nothing_to_cache(locked_pdf, isolated_server):
    cache, _ = isolated_server
    for call in SINGLE_FILE_CALLS.values():
        call(str(locked_pdf))
    with sqlite3.connect(cache.db_path) as conn:
        for table in ("pdf_metadata", "page_text"):
            (count,) = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE file_path = ?",
                (str(locked_pdf.resolve()),),
            ).fetchone()
            assert count == 0, table


def test_url_source_returns_password_required(locked_pdf, isolated_server):
    url = "https://example.com/statement.pdf"
    with patch.object(_core.url_fetcher, "fetch", return_value=locked_pdf):
        result = pdf_info(url)
    assert result["error_code"] == "password_required"
    assert result["error"] == f"PDF is password-protected: {url}"


def test_owner_only_pdf_still_reads_and_caches(owner_only_pdf, isolated_server):
    first = pdf_info(str(owner_only_pdf))
    assert "error" not in first
    assert first["from_cache"] is False
    assert pdf_info(str(owner_only_pdf))["from_cache"] is True
    pages = pdf_read_pages(str(owner_only_pdf), pages="1")
    assert "late fee" in pages["pages"][0]["text"]


def test_corrupt_pdf_is_not_reported_as_locked(tmp_path, isolated_server):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.7\nnot really a pdf")
    result = pdf_extract_chart(str(bad), page=1)[0]
    assert "error" in result
    assert "error_code" not in result


@pytest.fixture
def mixed_corpus(tmp_path, locked_pdf, plain_statement_pdf):
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    shutil.copy(locked_pdf, corpus_dir / "locked.pdf")
    shutil.copy(plain_statement_pdf, corpus_dir / "plain.pdf")
    return corpus_dir


def _skip_reasons(result):
    return {Path(entry["path"]).name: entry["reason"] for entry in result["skipped"]}


CORPUS_CALLS = {
    "pdf_corpus_warm": lambda d: pdf_corpus_warm(str(d)),
    "pdf_corpus_overview": lambda d: pdf_corpus_overview(str(d)),
    "pdf_corpus_search": lambda d: pdf_corpus_search(
        str(d), query="late fee", mode="keyword"
    ),
}


@pytest.mark.parametrize("tool", sorted(CORPUS_CALLS))
def test_corpus_tools_skip_locked_files(tool, mixed_corpus, isolated_server):
    result = CORPUS_CALLS[tool](mixed_corpus)
    assert "error" not in result
    assert _skip_reasons(result) == {"locked.pdf": "password_required"}


def test_corpus_warm_processes_the_unlocked_file(mixed_corpus, isolated_server):
    result = pdf_corpus_warm(str(mixed_corpus))
    assert result["corpus_size"] == 1
    assert result["unprocessed"] == []


def test_corpus_of_only_locked_files(tmp_path, locked_pdf, isolated_server):
    corpus_dir = tmp_path / "locked_only"
    corpus_dir.mkdir()
    shutil.copy(locked_pdf, corpus_dir / "a.pdf")
    result = pdf_corpus_warm(str(corpus_dir))
    assert result["error"] == "No PDF files found in corpus"
    assert _skip_reasons(result) == {"a.pdf": "password_required"}
