"""pdf_cache_stats, pdf_cache_clear and server_info."""

import copy
import sqlite3
from pathlib import Path
from typing import Any
from .. import __version__
from .. import corpus
from .. import portable_tesseract
from .. import updates
from ..extractor import find_tesseract
from ..parallel import resolve_workers
from .. import _core
from .._core import _MAX_PARALLEL_WORKERS, _detect_features, _tool_description, mcp

# Probed once at startup; stable for the process lifetime.
_SERVER_FEATURES = _detect_features()


# ============================================================================
# Tool 6: pdf_cache_stats - Get cache statistics
# ============================================================================


@mcp.tool(
    description=_tool_description(
        "Cache diagnostics: file counts, sizes, and the local cache"
        " directories pdf-mcp is using. Intended for debugging the local"
        " install — the directory paths in the response are local"
        " filesystem paths (single-user STDIO deployment) and should"
        " not be forwarded to remote agents."
    )
)
def pdf_cache_stats() -> dict[str, Any]:
    """
    Get PDF cache statistics.

    Returns:
        - total_files: Number of cached PDF files
        - total_pages: Number of cached pages
        - total_images: Number of cached images
        - cache_size_mb: Total cache size in MB
        - url_cache: Statistics about downloaded URL cache
        - images_dir: Local directory where extracted page images are
          cached. Reconstructs absolute paths for the opaque `image_id`
          values returned by `pdf_read_pages`.
        - renders_dir: Local directory where rendered page PNGs are
          cached. Same role for `render_id` values.
    """
    stats = _core.cache.get_stats()
    url_stats = _core.url_fetcher.get_cache_stats()

    return {
        **stats,
        "embedding_model": _core.pdf_config.embedding_model,
        "url_cache": url_stats,
        "images_dir": str(_core.cache.images_dir),
        "renders_dir": str(_core.cache.renders_dir),
    }


# ============================================================================
# Tool: server_info - Setup-time server introspection
# ============================================================================

_GLOB_METACHARACTERS = ("*", "?", "[")


def _document_roots(patterns: tuple[str, ...]) -> list[str]:
    """
    Reduce [paths] allow globs to directories a caller can pass through.

    A glob is not an argument: `pdf_corpus_overview("/data/pdfs/**")`
    resolves nothing, so reporting the raw patterns alone would leave the
    caller to parse them. For each pattern, keep the longest leading run of
    segments that contains no glob metacharacter, and resolve that to a
    directory:

    - it is a directory (`/data/pdfs/**` -> `/data/pdfs`, and
      `~/Documents/*.pdf` -> `~/Documents`, since the globbed segment ends
      the literal run): report it;
    - it is an existing FILE (an exact-file allow rule): report its parent,
      so the caller still learns where to look;
    - it does not exist: drop it.

    The parent fallback is deliberately limited to the file case. Applying it
    to a missing path would turn `/data/pdfs/gone/**` into `/data/pdfs`,
    advertising a root wider than the rule that produced it. Under-reporting
    is safe here (the caller can still pass any path it already knows, and
    check_path remains the authority); over-reporting points an agent at
    files the allow list will refuse.

    Dropping stale entries makes the result a floor rather than a mirror of
    the config, which is why server_info reports allow_patterns alongside it.
    """
    roots: set[str] = set()
    for pattern in patterns:
        expanded = Path(pattern).expanduser()
        literal_parts: list[str] = []
        for part in expanded.parts:
            if any(meta in part for meta in _GLOB_METACHARACTERS):
                break
            literal_parts.append(part)
        if not literal_parts:
            continue
        candidate = Path(*literal_parts)
        try:
            if candidate.is_dir():
                roots.add(str(candidate.resolve()))
            elif candidate.is_file() and candidate.parent.is_dir():
                roots.add(str(candidate.parent.resolve()))
        except OSError:
            # Unreadable or malformed path: treat as absent, same as a stale
            # entry. Introspection must never raise.
            continue
    return sorted(roots)


def _live_features() -> dict[str, Any]:
    """Startup feature probe with the OCR flag re-checked per call.

    Claude Desktop keeps servers for the app's lifetime and Tesseract can be
    installed meanwhile; OCR re-resolves the binary per call, so the flag
    must too.
    """
    features = copy.deepcopy(_SERVER_FEATURES)
    source = _ocr_source()
    features["extraction"]["ocr"]["available"] = source != "none"
    features["extraction"]["ocr"]["source"] = source
    return features


def _ocr_source() -> str:
    """system | portable | on_first_use (a bundle install that will fetch
    the portable Tesseract on the first OCR call) | none."""
    exe = find_tesseract()
    if exe is not None:
        return "portable" if exe == portable_tesseract.installed_binary() else "system"
    if _core._OCR_AUTO_INSTALL and portable_tesseract.platform_key() is not None:
        return "on_first_use"
    return "none"


@mcp.tool(
    description=(
        "Report which optional features are installed and what "
        "configuration is active on this pdf-mcp server. Call this first "
        "when about to use semantic search, OCR, or column-aware "
        "extraction — if the feature isn't available, downstream calls "
        "will either fall back silently (column-aware → positional sort) "
        "or fail (semantic mode → error). Also reports `documents`: the "
        "roots this server can open, which is how to find what is "
        "available when connected over HTTP and given no path to start "
        "from — pass a root straight to pdf_corpus_overview. Returns "
        "version, per-feature availability with descriptions, search mode "
        "list, document roots, and active config values. Cheap to call "
        "(no I/O beyond reading process state and stat-ing the configured "
        "roots). Results are stable for the server's lifetime, except that "
        "a root appears once its directory exists on disk and OCR shows as "
        "available once Tesseract is installed."
    )
)
def server_info() -> dict[str, Any]:
    """
    Report installed optional features and active configuration.

    Setup-time server introspection — distinct from pdf_cache_stats, which
    reports runtime cache state. This tool operates on the server itself
    (no PDF argument), which is why it omits the `pdf_` prefix that all
    PDF-operating tools carry.

    Returns:
        - version: pdf-mcp release version.
        - features: {
            extraction: {column_aware, ocr} — each {available, description};
                ocr also has source: "system", "portable", "on_first_use"
                (a bundle install that downloads Tesseract on the first OCR
                call) or "none",
            search: {modes_available, default_mode, embedding_model?}
                (embedding_model present only when semantic search is
                 available),
            corpus: {tools, max_files, budget_seconds_range,
                modes_available} — multi-document tool limits; corpus
                mode availability mirrors single-doc search.
          }
        - documents: {access_mode, roots, allow_patterns, deny_patterns}
            — which PDFs this server is willing to open.
            access_mode is "allowlist" when [paths] allow is configured,
            else "unrestricted". roots holds existing directories ready to
            pass straight to pdf_corpus_overview / pdf_corpus_warm; it is
            derived from allow_patterns and is empty under "unrestricted",
            which means "any path the server process can read", NOT "no
            documents available". allow_patterns / deny_patterns are the
            configured globs verbatim. Paths resolve on the server, so over
            the HTTP transport these are the server's files, not the
            caller's.
        - storage: {sqlite_version, journal_mode, keyword_search_ranked}
            — what the SQLite build actually supports. pdf-mcp declares no
            minimum SQLite version, and two capabilities degrade quietly on
            an old one. keyword_search_ranked=False means FTS5 is missing,
            so mode="keyword" falls back to substring matching with no BM25
            ranking and no stemming; prefer mode="semantic" there.
            journal_mode="delete" means the filesystem refused WAL; cache
            writes are then several times slower on Windows (~57ms per page
            write against ~11ms under WAL).
        - config: {max_workers, max_response_bytes, cache_ttl_hours,
                   cache_dir}. cache_dir is a local filesystem path
                   (single-user STDIO deployment, per the pdf_cache_stats
                   precedent).
        - update: {current, latest, update_available, checked_at,
                   download_url} from the daily update check, or null when
                   the check is off (every pip/uvx install by default).
    """
    # max_workers: resolve the actually-in-effect cap (PDF_MCP_MAX_WORKERS
    # override or the min(cpu_count, cap) default) by reusing resolve_workers
    # rather than re-deriving the logic. A large page count and gate=0 keep
    # those two from binding, leaving only the cpu/cap/env clamp.
    max_workers = resolve_workers(10**6, gate=0, cap=_MAX_PARALLEL_WORKERS)
    allow_patterns = _core.pdf_config.path_allow_patterns
    return {
        "version": __version__,
        "update": updates.update_status(
            __version__, _core.cache.cache_dir, _core._UPDATE_CHECK_ENABLED
        ),
        "features": _live_features(),
        "documents": {
            "access_mode": ("allowlist" if allow_patterns else "unrestricted"),
            "roots": _document_roots(allow_patterns),
            "allow_patterns": list(allow_patterns),
            "deny_patterns": list(_core.pdf_config.path_deny_patterns),
        },
        "storage": {
            "sqlite_version": sqlite3.sqlite_version,
            "journal_mode": _core.cache.journal_mode,
            "keyword_search_ranked": _core.cache.fts_available,
        },
        "config": {
            "max_workers": max_workers,
            "max_response_bytes": _core.pdf_config.max_response_bytes,
            "cache_ttl_hours": _core.cache.ttl_hours,
            "cache_dir": str(_core.cache.cache_dir),
        },
    }


# ============================================================================
# Tool 7: pdf_cache_clear - Clear cache
# ============================================================================


@mcp.tool()
def pdf_cache_clear(expired_only: bool = True) -> dict[str, Any]:
    """
    Clear the PDF cache.

    Args:
        expired_only: If True, only clear expired entries. If False, clear everything.

    Returns:
        - cleared_files: Number of files cleared from metadata cache
        - cleared_urls: Number of downloaded URLs cleared
    """
    if expired_only:
        cleared = _core.cache.clear_expired()
        corpus.clear_warm_memo()
    else:
        cleared = _core.cache.clear_all()
        _core.url_fetcher.clear_cache()
        corpus.clear_warm_memo()

    return {
        "expired_only": expired_only,
        "cleared_files": cleared,
        "message": "Cache cleared successfully",
    }
