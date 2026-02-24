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

from .cache import PDFCache
from .server import mcp

__version__ = "1.2.0"
__all__ = ["mcp", "PDFCache", "__version__"]
