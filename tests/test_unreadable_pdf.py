"""A file that PDFium cannot open is an inline error, not a failed tool call.

The inline contract lets an agent read the reason and move on. A raised
PdfiumError reached the client as a protocol-level tool error carrying raw
engine text, on every single-document tool.
"""

import asyncio
from unittest.mock import patch

import pytest
from fastmcp import Client

from pdf_mcp import server
from pdf_mcp.server import (
    pdf_extract_chart,
    pdf_get_toc,
    pdf_info,
    pdf_read_all,
    pdf_read_pages,
    pdf_render_pages,
    pdf_search,
)
from pdf_mcp.backend import bytesopen
from pdf_mcp.url_fetcher import PDFValidationError, _validate_pdf_content

CALLS = {
    "pdf_info": lambda p: pdf_info(p),
    "pdf_get_toc": lambda p: pdf_get_toc(p),
    "pdf_read_pages": lambda p: pdf_read_pages(p, pages="1"),
    "pdf_read_all": lambda p: pdf_read_all(p),
    "pdf_search": lambda p: pdf_search(p, query="voltage"),
    "pdf_render_pages": lambda p: pdf_render_pages(p, pages="1")[0],
    "pdf_extract_chart": lambda p: pdf_extract_chart(p, page=1)[0],
}


@pytest.fixture(params=[b"not a pdf at all", b"%PDF-1.7\nnot really a pdf"])
def bad_pdf(request, tmp_path):
    path = tmp_path / "bad.pdf"
    path.write_bytes(request.param)
    return str(path)


@pytest.mark.parametrize("tool", sorted(CALLS))
def test_every_single_document_tool_answers_inline(tool, bad_pdf, isolated_server):
    result = CALLS[tool](bad_pdf)
    assert result["error_code"] == "pdf_unreadable"
    assert bad_pdf in result["error"]
    assert "PDFium" not in result["error"]
    assert result["hint"]


def test_the_client_sees_a_result_not_a_tool_error(bad_pdf, isolated_server):
    async def call():
        async with Client(server.mcp) as client:
            return await client.call_tool("pdf_info", {"path": bad_pdf})

    result = asyncio.run(call())
    assert not result.is_error
    assert result.structured_content["error_code"] == "pdf_unreadable"


def test_a_truncated_download_is_unreadable(tmp_path, sample_pdf, isolated_server):
    data = open(sample_pdf, "rb").read()
    path = tmp_path / "cut.pdf"
    path.write_bytes(data[: len(data) // 3])
    assert pdf_info(str(path))["error_code"] == "pdf_unreadable"


def test_a_repaired_file_reads_again(tmp_path, sample_pdf, isolated_server):
    path = tmp_path / "doc.pdf"
    path.write_bytes(b"not a pdf")
    assert "error" in pdf_info(str(path))
    path.write_bytes(open(sample_pdf, "rb").read())
    assert "error" not in pdf_info(str(path))


def test_a_missing_file_keeps_its_own_error(tmp_path, isolated_server):
    result = pdf_info(str(tmp_path / "gone.pdf"))
    assert result.get("error_code") != "pdf_unreadable"
    assert "error" in result


def test_a_good_file_opens_once_across_calls(sample_pdf):
    bytesopen.clear_lock_memo()
    assert bytesopen.pdf_open_problem(sample_pdf) is None
    with patch.object(bytesopen.pdfium, "PdfDocument", side_effect=AssertionError):
        assert bytesopen.pdf_open_problem(sample_pdf) is None


def test_an_engine_failure_is_not_called_corrupt():
    with patch(
        "pdf_mcp.backend.bytesopen.open_pdf_bytes",
        side_effect=RuntimeError("engine state"),
    ):
        with pytest.raises(PDFValidationError) as caught:
            _validate_pdf_content(b"%PDF-1.7", "https://x/y.pdf")
    assert "corrupt" not in str(caught.value)
    assert "engine state" in str(caught.value)


def test_a_bad_download_is_still_called_corrupt():
    with pytest.raises(PDFValidationError, match="corrupt"):
        _validate_pdf_content(b"%PDF-1.4 not really a pdf", "https://x/y.pdf")
