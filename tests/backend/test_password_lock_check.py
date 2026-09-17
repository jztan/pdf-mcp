"""is_password_locked: tell a PDF that needs an open password from any other."""

import os
from unittest.mock import patch

import pypdfium2 as pdfium
import pytest

from pdf_mcp.backend import bytesopen
from pdf_mcp.backend.bytesopen import clear_lock_memo, is_password_locked


@pytest.fixture(autouse=True)
def _fresh_memo():
    clear_lock_memo()
    yield
    clear_lock_memo()


def test_locked_file_is_locked(locked_pdf):
    assert is_password_locked(str(locked_pdf)) is True


def test_owner_only_file_is_not_locked(owner_only_pdf):
    assert is_password_locked(str(owner_only_pdf)) is False


def test_plain_file_is_not_locked(plain_statement_pdf):
    assert is_password_locked(str(plain_statement_pdf)) is False


def test_corrupt_file_is_not_locked(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.7\nnot really a pdf")
    assert is_password_locked(str(bad)) is False


def test_missing_file_is_not_locked(tmp_path):
    assert is_password_locked(str(tmp_path / "nope.pdf")) is False


def test_not_locked_verdict_is_memoised(owner_only_pdf):
    assert is_password_locked(str(owner_only_pdf)) is False
    with patch.object(bytesopen.pdfium, "PdfDocument") as opener:
        assert is_password_locked(str(owner_only_pdf)) is False
    opener.assert_not_called()


def test_locked_verdict_is_rechecked(locked_pdf):
    assert is_password_locked(str(locked_pdf)) is True
    with patch.object(
        bytesopen.pdfium,
        "PdfDocument",
        side_effect=pdfium.PdfiumError("Incorrect password error"),
    ) as opener:
        assert is_password_locked(str(locked_pdf)) is True
    opener.assert_called_once()


def test_corrupt_verdict_is_not_memoised(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.7\nnot really a pdf")
    is_password_locked(str(bad))
    with patch.object(bytesopen.pdfium, "PdfDocument") as opener:
        is_password_locked(str(bad))
    opener.assert_called_once()


def test_mtime_change_invalidates_memo(owner_only_pdf):
    is_password_locked(str(owner_only_pdf))
    st = owner_only_pdf.stat()
    os.utime(owner_only_pdf, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    with patch.object(bytesopen.pdfium, "PdfDocument") as opener:
        is_password_locked(str(owner_only_pdf))
    opener.assert_called_once()


def test_memo_is_bounded(tmp_path, monkeypatch):
    import pymupdf

    monkeypatch.setattr(bytesopen, "_LOCK_MEMO_MAX", 2)
    for i in range(3):
        path = tmp_path / f"doc{i}.pdf"
        doc = pymupdf.open()
        doc.new_page()
        doc.save(str(path))
        doc.close()
        assert is_password_locked(str(path)) is False
    assert len(bytesopen._NOT_LOCKED_MEMO) == 2
