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


def test_server_advertises_pdf_mcp_version_in_handshake():
    """The FastMCP server must surface pdf-mcp's __version__ (not the
    FastMCP framework version) so MCP clients can tell pdf-mcp releases
    apart via the `initialize` handshake's `serverInfo.version` field.

    Regression for: when no explicit `version=` is passed to FastMCP(...),
    clients see FastMCP's own framework version (e.g. "3.2.4") instead of
    pdf-mcp's.
    """
    from pdf_mcp import __version__

    assert mcp.version == __version__, (
        f"FastMCP server.version is {mcp.version!r}, expected {__version__!r}. "
        f"Did the FastMCP() constructor lose its `version=` kwarg?"
    )


class TestSearchGuidanceInDescriptions:
    """The tool description is what an agent reads BEFORE calling, so the two
    query behaviours it cannot guess must be stated there: keyword terms are
    AND-matched (with an OR retry), and one ranked list cannot carry a
    question that spans several documents. Measured on a 24-filing corpus:
    a single corpus call left 'compare A with B' questions answerable only
    in part, while the same questions answered cleanly when asked once per
    document."""

    def test_both_search_tools_state_the_and_matching_and_or_retry(self):
        tools = _registered_tools()
        for name in ("pdf_search", "pdf_corpus_search"):
            desc = getattr(tools[name], "description", "") or ""
            lowered = desc.lower()
            assert "and-matched" in lowered, f"{name} omits AND-matching"
            assert "or-joined" in lowered, f"{name} omits the OR retry"

    def test_corpus_search_teaches_per_document_decomposition(self):
        desc = (
            getattr(_registered_tools()["pdf_corpus_search"], "description", "") or ""
        )
        lowered = " ".join(desc.lower().split())
        assert (
            "cannot carry every document's answer" in lowered
        ), "corpus tool omits why one ranked list fails multi-document questions"
        assert (
            "doc_match_counts" in lowered
        ), "corpus tool omits the signal that names documents missing from matches"

    def test_corpus_search_instructs_exhaustive_fanout_for_multi_doc(self):
        """The doc_match_counts guidance must carry the A/B-validated
        fan-out instruction (2026-07-29, spread_fanout_verdict.md):
        're-ask EVERY document listed' moved caller fan-out from a
        median of 2 documents to 5 and multi-document answer coverage
        from 56% to 69%, and was the only text of three tested that
        moved behavior without erasing the gain. The wording is
        LOCKED to the tested text; a paraphrase is a different,
        unvalidated intervention.
        """
        desc = (
            getattr(_registered_tools()["pdf_corpus_search"], "description", "") or ""
        )
        lowered = " ".join(desc.lower().split())
        assert "re-ask every document listed here" in lowered
        assert "recovers only about half of a multi-document answer" in lowered
        assert "single-document question, follow up on the best match only" in lowered
