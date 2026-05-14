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
__version__ = "1.12.1"

from .cache import PDFCache  # noqa: E402
from .server import mcp  # noqa: E402

__all__ = ["mcp", "PDFCache", "__version__"]
