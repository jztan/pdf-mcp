"""
pdf-mcp: MCP Server for PDF Processing

A production-ready MCP server for PDF processing with SQLite caching.
Provides tools for reading, searching, and extracting content from PDF files.

Usage:
    python -m pdf_mcp.server
"""

import os
from typing import Any
from . import __version__
from . import updates
from . import _core
from ._core import mcp
from .tools.info import pdf_info, pdf_get_toc  # noqa: F401
from .tools.read import pdf_read_pages, pdf_read_all  # noqa: F401
from .tools.search import pdf_search  # noqa: F401
from .tools.corpus_tools import (
    pdf_corpus_warm,
    pdf_corpus_overview,
    pdf_corpus_search,
)  # noqa: F401
from .tools.admin import pdf_cache_stats, server_info, pdf_cache_clear  # noqa: F401
from .tools.render import pdf_render_pages  # noqa: F401
from .tools.chart import pdf_extract_chart  # noqa: F401

__all__ = [
    "main",
    "main_http",
    "mcp",
    "pdf_cache_clear",
    "pdf_cache_stats",
    "pdf_corpus_overview",
    "pdf_corpus_search",
    "pdf_corpus_warm",
    "pdf_extract_chart",
    "pdf_get_toc",
    "pdf_info",
    "pdf_read_all",
    "pdf_read_pages",
    "pdf_render_pages",
    "pdf_search",
    "server_info",
]


# ============================================================================
# Main entry point
# ============================================================================


from fastmcp.server.middleware import Middleware, MiddlewareContext  # noqa: E402
from fastmcp.tools import ToolResult  # noqa: E402
from mcp.types import TextContent  # noqa: E402


class _UpdateNoticeMiddleware(Middleware):
    """Adds the pending update notice to the first dict-shaped tool result.

    Rebuilds the result from the updated dict so the JSON text block (what
    clients such as Claude Desktop show the model) and structuredContent
    both carry it. List-shaped results (pdf_render_pages) are left alone;
    the notice waits for the next dict result.
    """

    async def on_call_tool(  # type: ignore[override]
        self, context: MiddlewareContext, call_next: Any
    ) -> ToolResult:
        result: ToolResult = await call_next(context)
        data = result.structured_content
        single_text = len(result.content) == 1 and isinstance(
            result.content[0], TextContent
        )
        if _core._pending_notice and isinstance(data, dict) and single_text:
            updated = {**data, "notice": _core._pending_notice}
            _core._pending_notice = ""
            return ToolResult(
                content=updated, structured_content=updated, meta=result.meta
            )
        return result


mcp.add_middleware(_UpdateNoticeMiddleware())


def main() -> None:
    """
    Run the MCP server using STDIO transport.

    STDIO is used because:
    - Claude Desktop starts the server with the app and keeps it for the
      app's lifetime
    - Communication happens via stdin/stdout
    - Process exits when the client closes stdin

    That's why we use SQLite caching - it persists between process restarts.
    """
    # Explicitly use STDIO transport (this is the default, but being explicit)
    if _core._UPDATE_CHECK_ENABLED:
        updates.start_background_refresh(_core.cache.cache_dir)
    # show_banner=False: fastmcp's banner also checks PyPI for a newer fastmcp.
    mcp.run(transport="stdio", show_banner=False)


def main_http() -> None:
    """
    Run the MCP server over HTTP transport (remote access).

    Single-tenant by contract: run one instance per user, each with its own
    cache volume and filesystem. The SQLite cache and any warmed corpus are
    shared by every caller of a single process - there is no per-user
    partitioning, session scoping, or tenant key. Isolation comes from
    running separate processes, exactly as the STDIO path gets it from
    process-per-conversation.

    Fails closed: without ``PDF_MCP_AUTH_TOKEN`` the server does not start.
    Binds 127.0.0.1 by default; TLS and public exposure belong to a reverse
    proxy in front of this process, not to the app. Set a ``[paths]`` allow
    list in the config or the tools can read any path the process can.

    That allow list is also the document surface. Paths resolve here, not on
    the caller's machine, so a connected client reads files already under an
    allowed root or ``https://`` URLs this server fetches; MCP gives it no
    way to send one. Operators put documents under an allowed root out of
    band, and ``server_info`` reports those roots under ``documents.roots``
    so a caller can find them. See docs/remote-access.md.

    Env: PDF_MCP_AUTH_TOKEN (required), PDF_MCP_ALLOW_ANY_PATH (unset),
    PDF_MCP_HTTP_HOST (127.0.0.1), PDF_MCP_HTTP_PORT (8000),
    PDF_MCP_HTTP_PATH (/mcp).
    """
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

    token = os.environ.get("PDF_MCP_AUTH_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "PDF_MCP_AUTH_TOKEN is not set. The HTTP transport refuses to "
            "start unauthenticated - set it to a long random secret and "
            "pass it as 'Authorization: Bearer <token>'."
        )

    mcp.auth = StaticTokenVerifier(
        tokens={token: {"client_id": "pdf-mcp", "scopes": []}}
    )

    allow_any = os.environ.get("PDF_MCP_ALLOW_ANY_PATH", "").strip() == "1"
    if not allow_any and not _core.pdf_config.has_path_allowlist:
        raise SystemExit(
            "No [paths] allow list is configured "
            f"({_core.pdf_config.config_path}). The HTTP transport refuses to "
            "start unrestricted: with no allow list, every tool can read "
            "any path this process can reach. Add a [paths] allow list to "
            "that file, or set PDF_MCP_ALLOW_ANY_PATH=1 to override this "
            "deliberately."
        )

    from starlette.requests import Request
    from starlette.responses import JSONResponse

    @mcp.custom_route("/health", methods=["GET"])
    async def _health(request: Request) -> JSONResponse:
        """Unauthenticated liveness probe. Exposes the version and nothing else."""
        return JSONResponse({"status": "ok", "version": __version__})

    host = os.environ.get("PDF_MCP_HTTP_HOST", "127.0.0.1")
    port = int(os.environ.get("PDF_MCP_HTTP_PORT", "8000"))
    path = os.environ.get("PDF_MCP_HTTP_PATH", "/mcp")

    mcp.run(transport="http", host=host, port=port, path=path, show_banner=False)


if __name__ == "__main__":
    main()
