# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
