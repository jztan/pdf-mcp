"""The one-time update notice rides on the first dict-shaped tool result."""

import asyncio
import json

from fastmcp import Client

from pdf_mcp import server, updates


def _call(tool, args):
    async def run():
        async with Client(server.mcp) as client:
            return await client.call_tool(tool, args)

    return asyncio.run(run())


def test_notice_on_first_result_only(monkeypatch, isolated_server, sample_pdf):
    monkeypatch.setattr(server, "_pending_notice", "pdf-mcp 999.0.0 is available")
    first = _call("pdf_info", {"path": sample_pdf})
    second = _call("pdf_info", {"path": sample_pdf})
    assert first.structured_content["notice"] == "pdf-mcp 999.0.0 is available"
    assert "notice" not in second.structured_content


def test_no_notice_when_nothing_pending(monkeypatch, isolated_server, sample_pdf):
    monkeypatch.setattr(server, "_pending_notice", "")
    result = _call("pdf_info", {"path": sample_pdf})
    assert "notice" not in result.structured_content


def test_pending_notice_built_only_when_check_on(tmp_path):
    (tmp_path / updates.CACHE_FILENAME).write_text(
        json.dumps({"latest": "999.0.0", "checked_at": 0.0})
    )
    assert server._initial_notice(False, tmp_path) == ""
    assert "999.0.0" in server._initial_notice(True, tmp_path)


def test_notice_reaches_the_json_text_block_too(
    monkeypatch, isolated_server, sample_pdf
):
    """Clients that show the model the text block, not structuredContent,
    must see the notice as well."""
    monkeypatch.setattr(server, "_pending_notice", "pdf-mcp 999.0.0 is available")
    first = _call("pdf_info", {"path": sample_pdf})
    assert json.loads(first.content[0].text)["notice"] == (
        "pdf-mcp 999.0.0 is available"
    )


def test_list_results_never_carry_the_notice(monkeypatch, isolated_server, sample_pdf):
    """pdf_render_pages returns a list; the notice waits for a dict result."""
    monkeypatch.setattr(server, "_pending_notice", "pdf-mcp 999.0.0 is available")
    _call("pdf_render_pages", {"path": sample_pdf, "pages": "1", "dpi": 50})
    assert server._pending_notice == "pdf-mcp 999.0.0 is available"
