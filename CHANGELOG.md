# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]
### Changed
- `pdf_read_pages` now saves images as PNG files to `~/.cache/pdf-mcp/images/` and returns file paths instead of inline base64 data
- Image cache entries store file paths in SQLite instead of base64 blobs, significantly reducing database size
- Cache `get_stats()` reports combined SQLite + image directory size
- `pdf_read_pages` now always includes per-page `images` and `image_count` fields in each page dict
- New `total_images` field in `pdf_read_pages` response
- `pdf_read_all` docstring updated to direct users to `pdf_read_pages` for images

### Removed
- **BREAKING**: `pdf_extract_images` tool removed — use `pdf_read_pages` (images are now always included per-page)
- **BREAKING**: `include_images` parameter removed from `pdf_read_pages` — images are always returned

### Fixed
- Image files are now properly cleaned up on cache clear, expiration, and invalidation
- Expired cache entries are automatically pruned on server startup

### Tests
- Increase test coverage from 96% to 99% (184 tests)
- Add sentinel caching edge-case tests (DB migration, FileNotFoundError handling)
- Add extractor tests for RGBA format, unknown format, and save failure paths
- Add url_fetcher tests for cache hit, clear, SSRF, streaming size limit, and redirect edge cases
- Add server tests for `MAX_PAGES_LIMIT` truncation and `pdf_read_all` cache hit
- Add `parse_page_range` trailing comma test

## [1.3.0] - 2026-03-08
### Fixed
- PDF validation bypass: `.pdf` URL extension no longer skips magic-bytes (`%PDF`) verification when Content-Type is non-PDF

### Tests
- Add regression tests for `.pdf` URL returning HTML content (direct and via redirect)
- Add positive tests for valid PDFs served with incorrect or missing Content-Type headers
- Add `_resolve_path` tests for URL error handling (HTTPStatusError, HTTPError, ValueError) and relative path resolution
- Add search excerpt test for word-boundary adjustment and ellipsis truncation
- Add test for cached image retrieval in `pdf_read_pages`

### Changed
- Migrate from `mcp` SDK (FastMCP v2) to standalone `fastmcp` v3 package (`fastmcp>=3.0.0`)
- Switch code quality tooling from `ruff` to `flake8` + `black` (line-length 88)
- Remove unused `extract_text_with_coordinates` import from `server.py`
- Remove unused local variables in `extractor.py` image extraction

### Performance
- Remove redundant `doc.extract_image()` call in `extract_images_from_page()` that decoded every image twice; `Pixmap` constructor handles errors via existing try/except

### Tests
- Replace weak `TestExtractImagesColorFormats` with comprehensive `TestExtractImagesFromPage` covering output structure, CMYK→RGB conversion, error handling with logging, and multi-image indexing

## [1.2.0] - 2026-02-24
### Added
- SSRF prevention: block private/reserved IPs, localhost, and link-local addresses in URL fetcher with DNS resolution validation
- Prompt injection mitigation: `content_warning` fields on all tool responses returning untrusted PDF content
- Input validation: clamp `max_pages` (500), `max_results` (100), `context_chars` (2000), `max_images` (50) to prevent resource exhaustion
- Download size limit: 100MB max enforced via streaming downloads
- `.pdf` extension validation on local file paths
- Secure file permissions: `0o700` on cache directory, `0o600` on downloaded files

### Fixed
- SSRF TOCTOU vulnerability: redirects are now validated per-hop before connecting, preventing redirects to private/internal IPs
- `file_size_bytes` missing from cached `pdf_info` responses (schema mismatch between cached and uncached)
- sqlite3 `DeprecationWarning` on Python 3.12+ in `cache.clear_expired()` datetime handling
- Overly broad `except Exception` in image extraction narrowed to specific exception types with logging
- Local file path disclosure removed from `pdf_info` responses and error messages

### Changed
- URL cache filenames now use SHA-256 instead of MD5
- HTTP downloads use streaming with manual redirect handling instead of buffered response

## [1.1.2] - 2026-02-07

### Fixed
- `pdf_cache_clear` now returns actual cleared file count instead of `-1` sentinel value
- URL fetch errors now return clear, actionable error messages for LLMs instead of raw httpx exceptions
- Release script now bumps `__init__.py` version alongside other version files

## [1.1.1] - 2025-02-01

### Added
- MCP Registry support with `server.json` configuration
- Registry ownership declaration in README

## [1.1.0] - 2025-01-31

### Added
- Codecov integration for test coverage reporting
- Coverage badge in README

### Changed
- Publish workflow now runs tests with coverage
- Added pytest-cov to dev dependencies

## [1.0.0] - 2025-01-28

### Added
- Initial release
- 8 MCP tools for PDF processing:
  - `pdf_info` - Get document metadata, page count, TOC
  - `pdf_read_pages` - Read specific pages with caching
  - `pdf_read_all` - Read entire document (small PDFs)
  - `pdf_search` - Full-text search within PDF
  - `pdf_get_toc` - Get table of contents
  - `pdf_extract_images` - Extract images as base64
  - `pdf_cache_stats` - View cache statistics
  - `pdf_cache_clear` - Clear cache entries
- SQLite-based persistent caching
- URL support for remote PDFs
- Automatic cache invalidation on file changes
- Comprehensive test suite (18 tests)
