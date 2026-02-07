"""
pdf-mcp: MCP Server for PDF Processing

A production-ready MCP server for PDF processing with SQLite caching.
Provides tools for reading, searching, and extracting content from PDF files.

Usage:
    python -m pdf_mcp.server
"""

import os
from pathlib import Path
from typing import Any

import httpx
import pymupdf
from mcp.server.fastmcp import FastMCP

from .cache import PDFCache
from .extractor import (
    estimate_tokens,
    extract_images_from_page,
    extract_metadata,
    extract_text_from_page,
    extract_text_with_coordinates,
    extract_toc,
    parse_page_range,
)
from .url_fetcher import URLFetcher

# Initialize MCP server
mcp = FastMCP(
    name="pdf-mcp",
    instructions="Production-ready PDF processing server with caching. Use pdf_info first to understand document structure, then use other tools to read content."
)

# Initialize cache and URL fetcher
cache = PDFCache(ttl_hours=24)
url_fetcher = URLFetcher()


def _resolve_path(source: str) -> str:
    """
    Resolve source to local file path.
    
    Handles:
    - Local paths (absolute and relative)
    - URLs (downloads to local cache)
    """
    if url_fetcher.is_url(source):
        try:
            local_path = url_fetcher.fetch(source)
            return str(local_path)
        except httpx.HTTPStatusError as e:
            raise ConnectionError(
                f"Failed to download PDF from URL: HTTP {e.response.status_code}. "
                f"URL: {source}. Try a direct download link that doesn't redirect."
            ) from e
        except httpx.HTTPError as e:
            raise ConnectionError(
                f"Failed to download PDF from URL: {type(e).__name__}. "
                f"URL: {source}. Check that the URL is accessible and points to a valid PDF."
            ) from e
        except ValueError as e:
            raise ValueError(
                f"URL does not point to a valid PDF file: {source}. {e}"
            ) from e
    
    # Local path - resolve to absolute
    path = Path(source)
    if not path.is_absolute():
        path = Path.cwd() / path
    
    if not path.exists():
        raise FileNotFoundError(f"PDF file not found: {source}")
    
    return str(path.resolve())


# ============================================================================
# Tool 1: pdf_info - Get document information
# ============================================================================

@mcp.tool()
def pdf_info(path: str) -> dict[str, Any]:
    """
    Get PDF document information including metadata, page count, and table of contents.
    
    **Always call this first** to understand the document structure before reading content.
    Results are cached for faster subsequent access.
    
    Args:
        path: Path to PDF file (absolute, relative, or URL)
    
    Returns:
        Document info including:
        - page_count: Total number of pages
        - metadata: Author, title, creation date, etc.
        - toc: Table of contents (if available)
        - file_size_mb: File size in megabytes
        - estimated_tokens: Rough estimate of total tokens
        - from_cache: Whether result was served from cache
    """
    local_path = _resolve_path(path)
    
    # Try cache first
    cached = cache.get_metadata(local_path)
    if cached:
        return {
            **cached,
            "from_cache": True,
            "estimated_tokens": cached["page_count"] * 800,  # Rough estimate
            "file_size_mb": round(cached["file_size"] / (1024 * 1024), 2),
        }
    
    # Parse PDF
    doc = pymupdf.open(local_path)
    
    try:
        page_count = len(doc)
        metadata = extract_metadata(doc)
        toc = extract_toc(doc)
        file_size = os.path.getsize(local_path)
        
        # Cache the results
        cache.save_metadata(local_path, page_count, metadata, toc)
        
        return {
            "file_path": local_path,
            "page_count": page_count,
            "metadata": metadata,
            "toc": toc,
            "file_size_bytes": file_size,
            "file_size_mb": round(file_size / (1024 * 1024), 2),
            "estimated_tokens": page_count * 800,
            "from_cache": False,
        }
    finally:
        doc.close()


# ============================================================================
# Tool 2: pdf_read_pages - Read specific pages
# ============================================================================

@mcp.tool()
def pdf_read_pages(
    path: str,
    pages: str,
    include_images: bool = False
) -> dict[str, Any]:
    """
    Read text content from specific pages of a PDF.
    
    Use page ranges to control how much content is loaded.
    For large documents, read in chunks (e.g., "1-20", then "21-40").
    
    Args:
        path: Path to PDF file (absolute, relative, or URL)
        pages: Page specification:
            - "1-10": Pages 1 through 10
            - "1,5,10": Pages 1, 5, and 10
            - "1-5,10,15-20": Combination of ranges and individual pages
        include_images: If True, extract images as base64 (increases response size)
    
    Returns:
        - pages: List of {page, text} objects
        - total_chars: Total characters extracted
        - estimated_tokens: Estimated token count
        - cache_hits: Number of pages served from cache
        - images: List of images (if include_images=True)
    """
    local_path = _resolve_path(path)
    
    doc = pymupdf.open(local_path)
    
    try:
        page_nums = parse_page_range(pages, len(doc))
        
        if not page_nums:
            return {
                "error": f"No valid pages in range '{pages}'. Document has {len(doc)} pages.",
                "page_count": len(doc),
            }
        
        # Try to get cached text for all pages at once
        cached_texts = cache.get_pages_text(local_path, page_nums)
        
        results = []
        images = []
        cache_hits = 0
        total_chars = 0
        
        for page_num in page_nums:
            # Check cache
            if page_num in cached_texts:
                text = cached_texts[page_num]
                cache_hits += 1
            else:
                # Extract text
                page = doc[page_num]
                text = extract_text_from_page(page, sort_by_position=True)
                
                # Cache it
                cache.save_page_text(local_path, page_num, text)
            
            total_chars += len(text)
            results.append({
                "page": page_num + 1,  # 1-indexed for output
                "text": text,
                "chars": len(text),
            })
            
            # Extract images if requested
            if include_images:
                cached_images = cache.get_page_images(local_path, page_num)
                if cached_images:
                    images.extend(cached_images)
                else:
                    page_images = extract_images_from_page(doc, page_num)
                    if page_images:
                        cache.save_page_images(local_path, page_num, page_images)
                        images.extend(page_images)
        
        response = {
            "pages": results,
            "total_chars": total_chars,
            "estimated_tokens": estimate_tokens("".join(r["text"] for r in results)),
            "cache_hits": cache_hits,
            "cache_misses": len(page_nums) - cache_hits,
        }
        
        if include_images:
            response["images"] = images
            response["image_count"] = len(images)
        
        return response
        
    finally:
        doc.close()


# ============================================================================
# Tool 3: pdf_read_all - Read entire document (for small PDFs)
# ============================================================================

@mcp.tool()
def pdf_read_all(
    path: str,
    max_pages: int = 50
) -> dict[str, Any]:
    """
    Read the entire PDF document.
    
    **Warning**: Only use for small documents. For large documents, use pdf_read_pages
    with specific page ranges.
    
    Args:
        path: Path to PDF file (absolute, relative, or URL)
        max_pages: Maximum pages to read (safety limit, default 50)
    
    Returns:
        - full_text: Complete document text
        - page_count: Number of pages read
        - truncated: Whether document was truncated due to max_pages
        - estimated_tokens: Estimated token count
    """
    local_path = _resolve_path(path)
    
    doc = pymupdf.open(local_path)
    
    try:
        total_pages = len(doc)
        pages_to_read = min(total_pages, max_pages)
        truncated = total_pages > max_pages
        
        # Get cached texts
        page_nums = list(range(pages_to_read))
        cached_texts = cache.get_pages_text(local_path, page_nums)
        
        texts = []
        new_texts = {}
        
        for page_num in page_nums:
            if page_num in cached_texts:
                texts.append(cached_texts[page_num])
            else:
                page = doc[page_num]
                text = extract_text_from_page(page, sort_by_position=True)
                texts.append(text)
                new_texts[page_num] = text
        
        # Cache new texts
        if new_texts:
            cache.save_pages_text(local_path, new_texts)
        
        full_text = "\n\n".join(texts)
        
        return {
            "full_text": full_text,
            "page_count": pages_to_read,
            "total_pages": total_pages,
            "truncated": truncated,
            "total_chars": len(full_text),
            "estimated_tokens": estimate_tokens(full_text),
        }
        
    finally:
        doc.close()


# ============================================================================
# Tool 4: pdf_search - Search within PDF
# ============================================================================

@mcp.tool()
def pdf_search(
    path: str,
    query: str,
    max_results: int = 10,
    context_chars: int = 200
) -> dict[str, Any]:
    """
    Search for text within a PDF document.
    
    Use this to find relevant pages before reading full content.
    Much more efficient than loading the entire document.
    
    Args:
        path: Path to PDF file (absolute, relative, or URL)
        query: Text to search for (case-insensitive)
        max_results: Maximum number of matches to return (default 10)
        context_chars: Characters of context around each match (default 200)
    
    Returns:
        - matches: List of {page, excerpt, position} objects
        - total_matches: Total number of matches found
        - pages_with_matches: List of page numbers containing matches
    """
    local_path = _resolve_path(path)
    
    doc = pymupdf.open(local_path)
    
    try:
        matches = []
        pages_with_matches = set()
        total_matches = 0
        query_lower = query.lower()
        
        for page_num in range(len(doc)):
            page = doc[page_num]
            
            # Use PyMuPDF's search functionality
            text_instances = page.search_for(query)
            
            if text_instances:
                pages_with_matches.add(page_num + 1)
                total_matches += len(text_instances)
                
                # Get full page text for context extraction
                full_text = page.get_text()
                full_text_lower = full_text.lower()
                
                # Find matches and extract context
                start = 0
                while len(matches) < max_results:
                    pos = full_text_lower.find(query_lower, start)
                    if pos == -1:
                        break
                    
                    # Extract context around match
                    ctx_start = max(0, pos - context_chars // 2)
                    ctx_end = min(len(full_text), pos + len(query) + context_chars // 2)
                    
                    # Adjust to word boundaries
                    if ctx_start > 0:
                        space_pos = full_text.rfind(' ', ctx_start - 50, ctx_start)
                        if space_pos > 0:
                            ctx_start = space_pos + 1
                    
                    if ctx_end < len(full_text):
                        space_pos = full_text.find(' ', ctx_end, ctx_end + 50)
                        if space_pos > 0:
                            ctx_end = space_pos
                    
                    excerpt = full_text[ctx_start:ctx_end]
                    
                    # Add ellipsis if truncated
                    if ctx_start > 0:
                        excerpt = "..." + excerpt
                    if ctx_end < len(full_text):
                        excerpt = excerpt + "..."
                    
                    matches.append({
                        "page": page_num + 1,
                        "excerpt": excerpt.strip(),
                        "position": pos,
                    })
                    
                    start = pos + len(query)
                
                if len(matches) >= max_results:
                    break
        
        return {
            "query": query,
            "matches": matches,
            "total_matches": total_matches,
            "pages_with_matches": sorted(pages_with_matches),
            "searched_pages": len(doc),
        }
        
    finally:
        doc.close()


# ============================================================================
# Tool 5: pdf_get_toc - Get table of contents
# ============================================================================

@mcp.tool()
def pdf_get_toc(path: str) -> dict[str, Any]:
    """
    Get the table of contents (bookmarks/outline) from a PDF.
    
    Useful for understanding document structure and navigating to specific sections.
    
    Args:
        path: Path to PDF file (absolute, relative, or URL)
    
    Returns:
        - toc: List of {level, title, page} entries
        - has_toc: Whether document has a table of contents
        - entry_count: Number of TOC entries
    """
    local_path = _resolve_path(path)
    
    # Try cache first
    cached = cache.get_metadata(local_path)
    if cached and "toc" in cached:
        toc = cached["toc"]
        return {
            "toc": toc,
            "has_toc": len(toc) > 0,
            "entry_count": len(toc),
            "from_cache": True,
        }
    
    doc = pymupdf.open(local_path)
    
    try:
        toc = extract_toc(doc)
        
        return {
            "toc": toc,
            "has_toc": len(toc) > 0,
            "entry_count": len(toc),
            "from_cache": False,
        }
        
    finally:
        doc.close()


# ============================================================================
# Tool 6: pdf_extract_images - Extract images from pages
# ============================================================================

@mcp.tool()
def pdf_extract_images(
    path: str,
    pages: str | None = None,
    max_images: int = 20
) -> dict[str, Any]:
    """
    Extract images from PDF pages as base64-encoded PNG.
    
    Args:
        path: Path to PDF file (absolute, relative, or URL)
        pages: Page specification (default: all pages). Same format as pdf_read_pages.
        max_images: Maximum number of images to extract (default 20, to limit response size)
    
    Returns:
        - images: List of {page, index, width, height, format, data} objects
        - image_count: Number of images extracted
        - truncated: Whether results were truncated due to max_images
    """
    local_path = _resolve_path(path)
    
    doc = pymupdf.open(local_path)
    
    try:
        page_nums = parse_page_range(pages, len(doc))
        
        all_images = []
        
        for page_num in page_nums:
            # Check cache
            cached_images = cache.get_page_images(local_path, page_num)
            
            if cached_images:
                all_images.extend(cached_images)
            else:
                page_images = extract_images_from_page(doc, page_num)
                if page_images:
                    cache.save_page_images(local_path, page_num, page_images)
                    all_images.extend(page_images)
            
            if len(all_images) >= max_images:
                break
        
        truncated = len(all_images) > max_images
        images = all_images[:max_images]
        
        return {
            "images": images,
            "image_count": len(images),
            "total_found": len(all_images),
            "truncated": truncated,
        }
        
    finally:
        doc.close()


# ============================================================================
# Tool 7: pdf_cache_stats - Get cache statistics
# ============================================================================

@mcp.tool()
def pdf_cache_stats() -> dict[str, Any]:
    """
    Get PDF cache statistics and optionally clear expired entries.
    
    Returns:
        - total_files: Number of cached PDF files
        - total_pages: Number of cached pages
        - total_images: Number of cached images
        - cache_size_mb: Total cache size in MB
        - url_cache: Statistics about downloaded URL cache
    """
    stats = cache.get_stats()
    url_stats = url_fetcher.get_cache_stats()
    
    return {
        **stats,
        "url_cache": url_stats,
    }


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
        cleared = cache.clear_expired()
    else:
        cleared = cache.clear_all()
        url_fetcher.clear_cache()
    
    return {
        "expired_only": expired_only,
        "cleared_files": cleared,
        "message": "Cache cleared successfully",
    }


# ============================================================================
# Main entry point
# ============================================================================

def main():
    """
    Run the MCP server using STDIO transport.
    
    STDIO is used because:
    - Claude Desktop spawns a new process per conversation
    - Communication happens via stdin/stdout
    - Process exits after conversation ends
    
    That's why we use SQLite caching - it persists between process restarts.
    """
    # Explicitly use STDIO transport (this is the default, but being explicit)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
