"""The server answers both MCP protocol eras with the same tools and results.

Claude Desktop and other handshake clients use `initialize`; 2026-07-28 clients
discover the server and carry no session. Neither may see different tools or a
different result shape.
"""

import asyncio

import pytest
from fastmcp import Client
from mcp.types import ImageContent

from pdf_mcp import server

_LEGACY, _MODERN = "legacy", "auto"


def _session(mode, sample_pdf):
    async def run():
        async with Client(server.mcp, mode=mode) as client:
            is_modern = client.session.discover_result is not None
            tools = await client.list_tools()
            info = await client.call_tool("pdf_info", {"path": sample_pdf})
            missing = await client.call_tool("pdf_info", {"path": "/no/such.pdf"})
            render = await client.call_tool(
                "pdf_render_pages", {"path": sample_pdf, "pages": "1"}
            )
            return is_modern, tools, info, missing, render

    return asyncio.run(run())


@pytest.mark.parametrize(("mode", "expect_modern"), [(_LEGACY, False), (_MODERN, True)])
def test_both_eras_negotiate_and_serve_tools(
    mode, expect_modern, isolated_server, sample_pdf
):
    is_modern, tools, info, missing, render = _session(mode, sample_pdf)

    assert is_modern is expect_modern
    assert len(tools) == 13
    assert info.structured_content["page_count"] >= 1
    assert not info.is_error
    # The inline error contract: status OK with `error` in the payload.
    assert "error" in missing.structured_content
    assert not missing.is_error
    assert any(isinstance(block, ImageContent) for block in render.content)


def test_both_eras_list_identical_tools(isolated_server, sample_pdf):
    _, legacy, *_ = _session(_LEGACY, sample_pdf)
    _, modern, *_ = _session(_MODERN, sample_pdf)

    def shape(tools):
        return [(t.name, t.description, t.input_schema) for t in tools]

    assert shape(legacy) == shape(modern)
