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

### v1.6.0 — Smarter Search (FTS5) & Supply-Chain Hardening
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

### v1.7.0 — Semantic Search
- New `pdf_semantic_search` tool: finds pages by meaning, not keywords ("revenue growth" matches "sales increase", "financial performance")
- Powered by `BAAI/bge-small-en-v1.5` (384-dim) via `fastembed` — fully local, no external API
- Optional `[semantic]` install extra (`fastembed>=0.7`, `numpy>=1.24`); all existing tools work without it
- SQLite `page_embeddings` table caches raw float32 BLOBs; first search embeds all pages, subsequent queries rank in <5 ms
- Response includes per-result `score` (cosine similarity), `snippet`, `embedding_cache_hits`/`misses`, and `model` fields
- `embedding_pages` field added to `pdf_cache_stats` response
- Graceful error with install hint when `fastembed` is not installed
- Automatic schema migration: `page_embeddings` table preserved across upgrades

### v1.8.0 — Hybrid RRF Search
- `pdf_search` gains `mode` parameter: `"keyword"`, `"semantic"`, `"auto"` (hybrid RRF default)
- Reciprocal Rank Fusion (k=60) merges BM25 and semantic results into a single ranked list
- `search_mode` field in response indicates which path ran; `pdf_semantic_search` removed
- Hybrid falls back to keyword-only when `fastembed` is not installed

### v1.9.0 — OCR & Page Rendering
- New `pdf_render_pages` tool: renders pages as PNG images for vision-capable models (8 tools total)
- `pdf_read_pages` gains `ocr=True`/`ocr_lang` for Tesseract OCR on scanned pages; OCR'd text automatically searchable via `pdf_search`
- `pdf_read_pages` gains `render_dpi` to attach rendered PNG path alongside text (shared cache with `pdf_render_pages`)
- `pdf_info` gains `text_coverage`: per-page `{text_chars, raster_images}` for OCR candidate detection
- `pdf_search` matches gain `source` field (`"extracted"` or `"ocr"`)
- SQLite `page_renders` table with dedicated `renders_dir`; bidirectional cache sharing between render tools
- `source` column on `page_text`; lazy backfill for pre-v1.9.0 cached rows

### v1.10.0 — Section-Granularity Search (2026-05-04)
- `pdf_search` gains `granularity` parameter: `"page"` (default, backward compatible) or `"section"` (returns complete sections containing each match, ranked by BM25 over section text)
- New `pdf_mcp.section_detector` module — public `Section` dataclass and `detect_boundaries(pdf_path)` / `extract_toc_sections(doc)` / `derive_sections(pdf_path)` API
- 7-signal heuristic detector combines font-face delta, bold detection (via flag bit OR font-name marker like `.B`/`Bold`), vertical whitespace gap, top-of-page position, numbered/keyword heading regex, Title Case / ALL CAPS, and short-line cues. Threshold-4 weighted score; multi-line headings (number on one line, title on next) are merged via a post-pass
- TOC-first dispatcher: uses `doc.get_toc()` when present (authoritative for ~95% of academic PDFs); heuristic detector is the fallback for TOC-less PDFs
- New `pdf_section_fts` SQLite FTS5 virtual table (parallel to `pdf_search_fts` for pages); section index lazily populated on the first section-mode call per PDF, then reused
- New cache methods: `index_sections`, `search_section_fts`, `get_section_fts_coverage`
- **Validated on three real arxiv PDFs** (GNN review, LLM survey, GPT-3): heuristic detector F1 0.80–0.94 across PDFs with ≤0.20 spread (passes the kill-switch gate); page-mode agents save **1.32–9.46 extra `pdf_read_pages` calls per query** depending on document structure (9.46 average on the 75-page GPT-3 paper, where 0% of sections fit in a single page)
- New benchmark harness: `scripts/benchmark_sections.py` (with `--detector-source=toc|heuristic` and `--toc-flatten=all|leaves` flags for reproducible alternative views)
- 60+ new tests across `tests/test_section_detector.py`, `tests/test_cache.py`, `tests/test_server.py` (521 total, up from 489)

---

## Planned

(none currently scheduled)

---

## Future / Under Consideration

- **`pdf_render_pages` page labels** — each `ImageContent` block currently has no page annotation; if `render_failed_pages` fires, surviving images could be misaligned. FastMCP `Image` supports an `annotations` field — embed `{"page": N}` in each block so agents can correlate images to pages regardless of failures.
- **Bring-your-own embedding model (BYOM)** — allow users to swap out `BAAI/bge-small-en-v1.5` for any `fastembed`-compatible model via config, for multilingual or domain-specific use cases.
- ~~**Hybrid (BM25 + semantic) section search**~~ — **rejected after Phase 1 validation (2026-05-04).** Built the validation pipeline on `feature/hybrid-section-validation` (15 commits, 550 tests), then ran a literature review + 45-query confirmation calibration. Found: (a) on scientific papers, hybrid RRF gives only ~5% lift over BM25 (BEIR SciFact), well below the spec's 0.10 MRR gate threshold; (b) hybrid causes severe **lexical regression** at section grain (0.93 → 0.63 MRR, a 33% relative drop) because RRF dilutes BM25's clean rank-1 signal on title-style queries; (c) for paraphrase queries, the existing v1.8.0 `pdf_search(mode="auto")` page-grain hybrid is **3× better** than a hypothetical section-grain hybrid would be (0.187 vs 0.067). Conclusion: there is no query class where `hybrid-section` is the best cell, and SOTA scientific-paper QA systems (e.g., PaperQA2) have moved past BM25/dense fusion entirely toward LLM reranking. The validation infrastructure (cache helpers + benchmark framework) is being retained on `feature/hybrid-section-validation` for future use against alternative Phase-2 candidates. Full evidence in [docs/superpowers/specs/2026-05-04-hybrid-section-search-validation-design.md §10](docs/superpowers/specs/2026-05-04-hybrid-section-search-validation-design.md).
- **Heuristic detector escalation for low-quality PDFs** — for PDFs where the 7-signal detector underperforms (e.g., scanned PDFs after OCR, layout-irregular preprints), explore CRF-based or transformer-based layout detection (GROBID, Marker, Surya). Heavier dependency footprint; would be an optional `pdf-mcp[layout]` extra.
- **Agent-task evaluation for section vs page search** — the existing benchmark measures retrieval characteristics. A downstream eval (LLM grading on Q&A tasks, or agent-task completion benchmarks) would measure whether agents *answer better questions* with section-granularity, not just whether retrieval recalls more content.
