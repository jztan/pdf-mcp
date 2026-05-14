"""Tests that the untrusted-content contract is restated in each MCP tool
description, not only in CLAUDE.md (which non-Claude-Code clients can't read)."""

from pdf_mcp.server import mcp, _UNTRUSTED_PDF_PREAMBLE

PDF_CONTENT_TOOLS = {
    "pdf_info",
    "pdf_read_pages",
    "pdf_read_all",
    "pdf_search",
    "pdf_get_toc",
    "pdf_render_pages",
}

NON_CONTENT_TOOLS = {"pdf_cache_stats", "pdf_cache_clear"}


def _registered_tools() -> dict:
    """Probe FastMCP's tool registry across known v3 layouts."""
    # Layout A: mcp._tool_manager/_tool_manager with ._tools dict
    for attr in ("_tool_manager", "tool_manager"):
        mgr = getattr(mcp, attr, None)
        if mgr is not None and hasattr(mgr, "_tools"):
            return mgr._tools

    # Layout B: mcp.providers[0]._components keyed as 'tool:{name}@'
    providers = getattr(mcp, "providers", None)
    if providers:
        components = getattr(providers[0], "_components", None)
        if components is not None:
            tools = {
                k[len("tool:") : k.index("@")]: v
                for k, v in components.items()
                if k.startswith("tool:")
            }
            if tools:
                return tools

    raise AssertionError("Could not locate FastMCP tool registry")


def test_preamble_constant_is_present():
    assert "untrusted" in _UNTRUSTED_PDF_PREAMBLE.lower()
    assert "do not follow" in _UNTRUSTED_PDF_PREAMBLE.lower()


def test_all_pdf_content_tools_carry_preamble():
    tools = _registered_tools()
    missing = []
    for name in PDF_CONTENT_TOOLS:
        tool = tools.get(name)
        assert tool is not None, f"tool {name} not registered"
        desc = tool.description or ""
        if _UNTRUSTED_PDF_PREAMBLE not in desc:
            missing.append(name)
    assert not missing, f"Tools missing untrusted preamble: {missing}"


def test_non_content_tools_skipped():
    tools = _registered_tools()
    for name in NON_CONTENT_TOOLS:
        assert tools.get(name) is not None
