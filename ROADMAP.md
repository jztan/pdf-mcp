# Roadmap

## Released

### v1.0.0 — Initial Release (2025-01-28)
- 8 MCP tools: `pdf_info`, `pdf_read_pages`, `pdf_read_all`, `pdf_search`, `pdf_get_toc`, `pdf_extract_images`, `pdf_cache_stats`, `pdf_cache_clear`
- SQLite-based persistent caching with mtime invalidation
- URL support for remote PDFs
- 18 tests

### v1.1.0 — Coverage & Registry (2025-01-31)
- Codecov integration and coverage badge
- MCP Registry support with `server.json`

### v1.1.2 — Bug Fixes (2026-02-07)
- Fixed `pdf_cache_clear` returning `-1` instead of actual cleared count
- Actionable error messages for URL fetch failures

### v1.2.0 — Security Hardening (2026-02-24)
- SSRF prevention: block private/reserved IPs with per-hop redirect validation
- Prompt injection mitigation: `content_warning` on all tool responses
- Input clamping: `max_pages`, `max_results`, `context_chars`
- 100 MB download size limit
- Secure file permissions on cache directory and downloaded files

### v1.3.0 — Reliability & Tooling (2026-03-08)
- Fix PDF validation bypass for `.pdf` URLs with non-PDF Content-Type
- Migrate from `mcp` SDK (FastMCP v2) to standalone `fastmcp` v3
- Switch code quality tooling to `flake8` + `black`
- Remove redundant `doc.extract_image()` double-decode in image extraction

### v1.4.0 — Image Cache Overhaul (2026-03-14)
- **BREAKING**: `pdf_extract_images` removed — images now always included per-page in `pdf_read_pages`
- **BREAKING**: `include_images` parameter removed from `pdf_read_pages`
- Images saved as PNG files to `~/.cache/pdf-mcp/images/` instead of inline base64
- `total_images` field added to `pdf_read_pages` response

### v1.5.0 — Table Extraction (2026-03-21)
- Table extraction via PyMuPDF `find_tables()` with visible-line detection
- `tables` and `table_count` always included per-page in `pdf_read_pages`
- `total_tables` field added to `pdf_read_pages` response
- SQLite `page_tables` cache with empty-list sentinel for tableless pages
- Suppress PyMuPDF/SWIG `DeprecationWarning` noise on server start/shutdown

### v1.6.0 — Smarter Search (FTS5) & Supply-Chain Hardening ← current
- `pdf_search` upgraded to SQLite FTS5 with Porter stemming and BM25 relevance ranking
- Results ordered by relevance score, not page number
- First search builds the index; subsequent searches are O(log N) instead of O(N) page scan
- Response schema: `page_match_counts`, per-match `score`, `index_used` flag, accurate `total_matches`
- Graceful fallback to Python scan on SQLite builds without FTS5
- `fts_indexed_pages` field added to `pdf_cache_stats` response
- No new dependencies
- GitHub Actions pinned to exact commit SHAs (prevents tag-hijacking)
- `pip-audit` runs on every CI build and dependency-changing PRs
- `uv.lock` committed for reproducible builds
- Least-privilege `permissions` blocks on all workflows
- `black` upgraded to 26.3.1 (CVE-2026-32274 fix)

---

## Planned

### v1.7.0 — Semantic Search

New `pdf_semantic_search` tool that finds pages by meaning, not keywords. "Revenue growth" finds pages about "sales increase" and "financial performance." Powered by local embeddings with no external API. Optional dependency.

---

## Future / Under Consideration

- Search across all cached PDFs in a single query
- Bring-your-own embedding model for domain-specific or multilingual PDFs
