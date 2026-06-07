"""
pdf-mcp: Production-ready MCP server for PDF processing.

A Model Context Protocol server that provides tools for reading, searching,
and extracting content from PDF files with SQLite caching for performance.

Install:
    pip install pdf-mcp

Usage with Claude Desktop:
    Add to claude_desktop_config.json:
    {
        "mcpServers": {
            "pdf": {
                "command": "python",
                "args": ["-m", "pdf_mcp.server"]
            }
        }
    }
"""

# Defined before the .server import so server.py can reach it without
# triggering a circular import during package initialisation. FastMCP
# uses this value as serverInfo.version in the MCP initialize handshake.
__version__ = "1.15.0"

from .cache import PDFCache  # noqa: E402

__all__ = ["mcp", "PDFCache", "__version__"]


# PEP 562 module-level __getattr__: expose `mcp` lazily so importing a submodule
# (e.g. a spawned worker importing extractor) does not build FastMCP or construct
# the module-level PDFCache in server.py.
def __getattr__(name: str) -> object:
    if name == "mcp":
        from .server import mcp

        return mcp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
