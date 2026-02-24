# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
