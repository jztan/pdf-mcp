"""The test session must never open the developer's real cache.

pdf_mcp.server builds its module-level PDFCache at import time, from
PDF_MCP_CACHE_DIR or else ~/.cache/pdf-mcp. conftest imports the server,
so without an override every test run opened the real cache, and an
_EXTRACTION_VERSION bump on a branch ran its upgrade there: the first
test process to import the new code dropped the developer's cached text,
embeddings and indexes, and stamped the new version so a later real
upgrade would skip its purge (2026-09-14).
"""

from pathlib import Path

import pdf_mcp.server as server_module


def test_server_cache_is_not_the_users_real_cache():
    real = (Path.home() / ".cache" / "pdf-mcp").resolve()
    assert Path(server_module.cache.cache_dir).resolve() != real


def test_url_downloads_are_not_under_the_users_real_cache():
    real = (Path.home() / ".cache" / "pdf-mcp").resolve()
    downloads = Path(server_module.url_fetcher.cache_dir).resolve()
    assert real not in downloads.parents and downloads != real
