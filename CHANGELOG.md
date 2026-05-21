# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.13.1] - 2026-05-21
### Changed
- Release automation (`scripts/release.py`) reordered: the GitHub
  release is now created only after the `publish-pypi.yml` workflow
  reports success and the version is live on PyPI, so the `pip install`
  line in the release notes is never a lie. A new preflight step runs
  the same `pip-audit` invocation CI uses, so a vulnerability that
  would block publishing is caught locally before the tag is pushed.
  When the publish step does fail post-tag, the script now prints the
  exact recovery steps (rerun the workflow vs. burn the version)
  instead of exiting silently.
- The `pip-audit` ignore list now lives in a single
  `scripts/audit.sh`, invoked by `ci.yml`,
  `dependency-review.yml`, `publish-pypi.yml`, and the release
  preflight, so the four call sites cannot drift.

### Security
- Bumped `idna` 3.11 → 3.15 to clear CVE-2026-45409.
- Added `PYSEC-2025-183` (pyjwt, transitive via `mcp`, no upstream
  fix yet) to the pip-audit ignore list, alongside the existing
  `CVE-2026-4539` (pygments, dev-only) and `CVE-2026-3219` (pip,
  build-time only) entries.

### BREAKING
- `pdf_read_pages` response shape: per-image dicts now carry `image_id`
  (content-addressed basename) instead of `path` (absolute filesystem
  path), and the per-page `render_path` field is replaced by
  `render_id`. Rationale: API hygiene. The previous `path` field
  embedded the current cache directory, so the value was unstable
  across runs and across `PDF_MCP_CACHE_DIR` changes; the new IDs are
  stable opaque tokens. Callers that need bytes resolve the ID against
  `images_dir` / `renders_dir` from `pdf_cache_stats`, or call
  `pdf_render_pages` (which inlines PNG content blocks for vision
  models). No compatibility shim, since these keys have never
  appeared in a released version.

### Added
- `pdf_cache_stats` response now includes `images_dir` and
  `renders_dir` so callers can resolve the opaque `image_id` /
  `render_id` returned by `pdf_read_pages` to disk paths when they
  need to read bytes directly. The tool description marks
  `pdf_cache_stats` as cache diagnostics.
- `[limits].max_response_bytes` config option (default 200 KB, max 2 MB)
  capping `pdf_read_all` and section-granularity `pdf_search` response
  payloads. New response fields: `truncated`, `truncated_pages`,
  `truncated_bytes`, `bytes_returned`, `bytes_available`, `next_page`
  (on `pdf_read_all`) and `matches_omitted` (on section search).
- Untrusted-content security preamble on every MCP tool that returns
  PDF-derived text/OCR/section content, visible to non-Claude-Code
  clients via the tool `description` field.

### Security
- `url_fetcher` now rejects non-PDF content-types (`text/*`,
  `application/json`, image/audio/video, etc.) before buffering bytes.
- Expanded IPv6 SSRF deny list: `::ffff:0:0/96` (IPv4-mapped),
  `64:ff9b::/96`, `100::/64`, `2001:db8::/32`, `fd00:ec2::254/128`
  (AWS IMDS over IPv6), and `::/128` (unspecified). IPv4-mapped IPv6
  addresses are unwrapped and re-tested against the IPv4 deny list.
- `url_fetcher` now pins the DNS-resolved IP per redirect hop,
  closing the TOCTOU gap between SSRF validation and TCP connect.
- Cache directory is now `chmod 0o700` after creation (defense-in-
  depth). pdf-mcp's supported deployment is single-user, so this
  does not patch an in-scope threat — it tightens permissions to
  match `images/` and `renders/` which were already 0o700, and
  reduces blast radius if the supported model ever expands.

### Changed
- `PDF_MCP_CACHE_DIR` and `PDF_MCP_CACHE_TTL` environment variables
  are now honored at server startup (previously declared in the MCP
  registry manifest but not wired into the Python code). `CACHE_TTL`
  must parse as an integer in `[0, 8760]` hours (up to one year) —
  bad values fail loud at startup rather than silently falling back
  to the default.
- `pdf_read_all` now accepts `start_page: int` (default `1`) and
  echoes the post-clamp value in the response. The pre-existing
  `next_page` field in the response is now consumable: pass it back
  as `start_page` to resume the read on a clean page boundary.
  Previously `next_page` named a continuation cursor the tool had
  no parameter to accept, forcing callers to fall back to
  `pdf_read_pages` for the resume. A regression test enforces the
  invariant that iterating `start_page=next_page` covers every page
  exactly once.
- The MCP `initialize` handshake now reports pdf-mcp's `__version__`
  as `serverInfo.version`. Previously the field carried FastMCP's
  framework version (e.g. `3.2.4`) because no explicit `version=`
  was passed to `FastMCP(...)`, so MCP clients could not tell
  pdf-mcp releases apart from the handshake alone.
- SSRF rejection now surfaces a self-describing error
  ("URL host resolves to a blocked IP on the SSRF deny list (loopback /
  RFC 1918 / link-local / IMDS / IPv6 ULA): …") instead of the previous
  generic "URL does not point to a valid PDF file" wrapper, so security
  blocks are no longer indistinguishable from format problems or
  filesystem 404s.
- `URLFetcher.is_url` now recognises `http://` URLs as well as
  `https://`, routing them through the validator so callers get a clear
  "Only HTTPS URLs are supported" error rather than the misleading
  "PDF file not found" path-resolution error.
- `pdf_search` section-mode docstring clarifies that `matches_omitted`
  counts byte-cap drops only — drops caused by a low `max_results` are
  not counted there (re-query with a higher `max_results` to see them).
- `pdf_info` docstring clarifies that the `toc` field is gated by
  `toc_entry_count <= 50`, independent of the `detail` flag (which only
  controls per-page `text_coverage` arrays).
- `pdf_search` `@mcp.tool` description corrected from "keyword,
  semantic, or hybrid (RRF) modes" to "keyword, semantic, or auto
  (hybrid RRF) modes" — the public mode name is `auto`, `hybrid` is
  rejected. The runtime always accepted only `auto/keyword/semantic`;
  the description was wrong, so a caller reading the tool description
  would try `mode="hybrid"` and get an inline error.
- `pdf_search` and `pdf_info` tool descriptions now carry the
  `matches_omitted` byte-cap-only semantics and the `toc` ≤50 gating
  note. Previously these clarifications lived only in function
  docstrings, which FastMCP does not surface as `description=` on the
  wire — so LLM callers couldn't see them.

### Documentation
- Clarified `[limits].max_response_bytes` docstring: the cap bounds
  the text content field (`full_text` on `pdf_read_all`; section
  titles + overhead on section-mode `pdf_search`), not the wire-
  level MCP TextContent envelope. The envelope adds ~300–500 bytes
  of other response fields and JSON framing on top of the cap.

## [1.12.1] - 2026-05-12
### Fixed
- `pdf_search` `total_matches` in keyword mode could disagree with `len(matches)` after the 1.12.0 tokenisation fix — multi-word queries like `pgvector latency` returned 4 matches with `total_matches: 0` because the literal phrase didn't appear anywhere even though both tokens did. `total_matches` now equals `len(matches)` in every mode, and `get_fts_page_counts` counts token occurrences (not literal-phrase) so `page_match_counts` keeps its per-page intensity signal in keyword mode.
- Heuristic section detector emitted body paragraphs that started with a heading-shaped prefix (e.g. "Section 2: This paragraph discusses ...") as the section title, because the regex fired on the prefix even when the rest of the line was prose. A stricter `_looks_like_clean_heading` shape check (≤120 chars, no mid-string `. ` or `; `) now runs after the scored signals; candidates that fail it still produce a section boundary but with `title: None`.
- `pdf_search` section-mode previously inferred `title_source` from cached PDF metadata at response time, which meant a section search called before `pdf_info` populated the metadata cache reported `title_source: "heading_detected"` for every match — even when `derive_sections` actually took the TOC path. `title_source` is now set at detection time on the `Section` dataclass and persisted on the FTS row, so the field is correct regardless of call order.

### Added
- `pdf_search` hybrid-mode matches now carry per-match `low_confidence` (true when there's no keyword hit on the page AND the underlying semantic cosine is below `confidence_threshold` — pages with literal-term hits stay confident regardless of cosine) plus `semantic_score`, mirroring the semantic-mode flag added in 1.12.0. Response-level `all_results_low_confidence` and `confidence_threshold` are present in both modes. Matches are NOT dropped when low-confidence — agents decide whether to surface "couldn't find it but here's the closest" vs "couldn't find it."
- `pdf_search` section-mode matches now carry a `title_source` field: `"toc"`, `"heading_detected"`, or `null`. Sections with `title_source: null` also have `title: null` so agents can show the page range without rendering a synthesised label.
- Property test `test_total_matches_equals_len_matches_property` asserts the invariant `len(matches) == total_matches` across all modes × queries (including multi-word tokenised queries), so a future regression fails CI.

### Changed
- Semantic-mode `all_low_confidence` renamed to `all_results_low_confidence` for parity with the new hybrid-mode field.
- New `title_source UNINDEXED` column on `pdf_section_fts`. Pre-1.12.1 section indexes are dropped and recreated on first launch (FTS5 does not support `ALTER ADD COLUMN`); sections re-index lazily on the next section-mode call per PDF.

### Docs
- Browser demo (`pages/index.html`, served at `pdf-mcp.jztan.com`): search mock now tokenises queries (whitespace AND), counts token occurrences for `page_match_counts`, and sets `total_matches = matches.length` to mirror the server's 1.12.1 keyword path. Demo footer bumped to `v0.4`.

## [1.12.0] - 2026-05-12
### Fixed
- `pdf_search` hybrid mode used to return a stale, pre-fusion `total_matches` (and `page_match_counts`) alongside the post-RRF matches array, producing self-contradicting payloads like `matches=[5 items], total_matches=0`. Both fields are now recomputed from the fused result set. Semantic mode now includes `total_matches`/`page_match_counts` so the schema is consistent across all three modes.
- `pdf_search` keyword mode was effectively phrase-only because `_escape_fts5_query` wrapped the entire query in double-quotes. Multi-word queries like `"pgvector latency"` returned zero matches when the words appeared on the same page but non-contiguously. Queries are now tokenised; pages must contain all tokens (implicit FTS5 AND) and BM25 still ranks by combined frequency.
- `pdf_search` auto mode crashed with a `ToolError` when fastembed was installed but the embedding model could not be loaded (offline machine, HF outage, etc.). It now degrades to keyword and surfaces `semantic_unavailable=true` plus a `semantic_unavailable_reason` string.
- Heuristic section detector emitted body-paragraph snippets as section titles when a line started with a heading-shaped prefix (e.g. "Section 2: This paragraph discusses ..."). Lines longer than 200 chars are now rejected as heading candidates so no spurious sections are produced.

### Changed
- **BREAKING**: `pdf_info.text_coverage` shape changed from `list[{page, text_chars, raster_images}]` to a compact dict. By default it now contains only a constant-size `summary` (page-count rollups + truncated OCR candidate list) so payload size stays bounded regardless of page count — a 3000-page PDF no longer ships ~6000 ints just for coverage. Pass `pdf_info(path, detail=True)` to opt into the per-page parallel arrays `text_chars_per_page` and `raster_images_per_page`.

### Added
- Per-match `low_confidence` flag plus response-level `all_low_confidence` and `confidence_threshold` on `pdf_search` semantic-mode responses, so agents can decide whether to trust top-k semantic results below the cosine threshold.

### Docs
- README: updated the `pdf_info` description to reflect the new `text_coverage` shape (summary by default, per-page arrays under `detail=True`).
- Browser demo (`pages/index.html`, served at `pdf-mcp.jztan.com`): mock response and coverage visualizations migrated to the new compact `text_coverage` shape; demo footer bumped to `v0.3`.

## [1.11.0] - 2026-05-09

### Added
- **Bring Your Own Model (BYOM)** — embedding model is now configurable via the `[embedding] model = "..."` setting. Four models validated: `BAAI/bge-small-en-v1.5` (default), `BAAI/bge-large-en-v1.5`, `mixedbread-ai/mxbai-embed-large-v1`, and `nomic-ai/nomic-embed-text-v1.5`. See `docs/embedding-models.md` for MTEB scores and trade-offs.
- `model_name` threaded through `pdf_search` and `pdf_cache_stats` responses so agents can verify which embedding model produced a given result.
- `model` column on `page_embeddings` cache table — switching models evicts stale rows automatically (no manual cache clear needed).
- Embedding-model benchmark script (`scripts/bench_embedding_models.py`) with MRR + latency gate, summary tables, and markdown export. Used to validate the four supported models before shipping.
- `embedding_model` property on `Config` (renamed `MODEL_NAME` → `DEFAULT_MODEL`).

### Fixed
- `embedder` batch_size lowered to 8 (fastembed default is 256) to prevent OOM and hang on long-context models like `nomic-embed-text-v1.5` (8192-token window) when processing 75-page PDFs. First capped at 16 (68772e1), then lowered to 8 after 16 still hung nomic (17db1ef).

### Changed
- Bumped `pip` to 26.1.1 and `python-multipart` to 0.0.27 (transitive dep updates).

## [1.10.0] - 2026-05-03
### Added
- `pdf_search` gains `granularity="section"` parameter — returns matching *sections* instead of pages, ranked by BM25 over section text. Section boundaries come from the PDF's TOC when present (~95% of academic PDFs); otherwise from a new 7-signal heuristic detector that combines font-face delta, bold detection, vertical whitespace gap, top-of-page position, heading regex, capitalization, and line length. Default `granularity="page"` preserves existing behaviour byte-for-byte. Response shape: `{"sections": [{"section_id", "title", "start_page", "end_page", "score"}], "search_mode": "section", "total_sections": int}`.
- `pdf_mcp.section_detector` module — new public module exposing `Section` dataclass, `detect_boundaries(pdf_path)` (heuristic detector), `extract_toc_sections(doc)` (TOC-derived), and `derive_sections(pdf_path)` (TOC-first dispatcher with heuristic fallback). Validated on three real arxiv PDFs: F1 0.80–0.94 detector quality; page-mode agents save 1–9 `pdf_read_pages` tool calls per query on multi-page sections (9.46 average on a 75-page paper).
- `pdf_section_fts` virtual table in SQLite cache, parallel to `pdf_search_fts`. Section indexing is lazy: populated on the first `pdf_search(granularity="section")` call per PDF, reused across subsequent calls.
- Cache methods: `index_sections(path, sections)`, `search_section_fts(path, query, max_results)`, `get_section_fts_coverage(path)`.

## [1.9.0] - 2026-04-19
### Added
- `pdf_render_pages(path, pages, dpi=200)` — new MCP tool that renders PDF pages as PNG images returned inline as MCP image content blocks; intended for vision-capable models that need to *see* page content (diagrams, handwriting, scanned pages). First element of the response is always a JSON summary block; subsequent elements are one image per page. DPI clamped to `[72, 400]`; up to 5 pages inline per call (`truncated_render: true` when truncated). Does not run OCR — tools are orthogonal.
- `pdf_read_pages` gains `ocr=True` / `ocr_lang="eng"` parameters — runs Tesseract OCR on pages that have no extractable text; OCR'd text is written to cache with `source='ocr'` and becomes instantly searchable via `pdf_search`. Requires system Tesseract (`brew install tesseract` / `apt install tesseract-ocr`). Pre-flight check returns a clean error with install hint if Tesseract is missing. Capped at 20 pages per call (`truncated_ocr: true` when truncated).
- `pdf_read_pages` gains `render_dpi` parameter — attaches a `render_path` (PNG on disk) alongside extracted text for each page; uses the same `page_renders` cache as `pdf_render_pages` (bidirectional: either tool populates the cache, either reads it).
- `pdf_info` gains `text_coverage` field — per-page `{page, text_chars, raster_images}` list letting agents identify OCR candidates without reading page content. Computed in the same parse pass as metadata; zero additional cost on cached calls. Pre-v1.9.0 cached rows are backfilled lazily on the next `pdf_info` call without requiring a cache clear.
- `pdf_search` matches now include a `source` field (`"extracted"` or `"ocr"`) so agents can tell when a match came from OCR text (lower confidence, worth cross-checking with `pdf_render_pages`).
- `page_renders` table in SQLite cache — stores rendered PNGs keyed on `(file_path, page_num, dpi)` with mtime-based invalidation and orphan guard (old PNG unlinked on path change). `renders_dir` (`~/.cache/pdf-mcp/renders/`) kept separate from `images_dir`.
- `source` column on `page_text` — `'extracted'` (default) or `'ocr'`; schema migration is additive (existing rows get `'extracted'` via `DEFAULT`).
- OCR requires only system Tesseract — no additional Python packages (`brew install tesseract` / `apt install tesseract-ocr` / [Windows installer](https://github.com/UB-Mannheim/tesseract/wiki)).

### Fixed
- `pdf_render_pages` raised `Output validation error: outputSchema defined but no structured output returned` in FastMCP 3.x when deployed to Claude Desktop. FastMCP infers an `outputSchema` from `list[Any]` and then rejects `ImageContent` blocks as non-serializable JSON. Fixed by setting `output_schema=None` on the decorator to opt out of schema generation for tools that return mixed content types.

### Changed
- `pdf_cache_stats` now includes `total_renders` count and adds render PNG sizes to `cache_size_bytes`.
- Cache housekeeping (`_invalidate_file`, `clear_expired`, `clear_all`) extended to delete render PNGs and `page_renders` rows.

### Tests
- 87 new unit tests across `test_pdf_reader.py` and `test_server.py` covering: `page_renders` cache CRUD + housekeeping, `source` column on `page_text`, `text_coverage_json` on `pdf_metadata` (including lazy backfill), `render_page_as_png` extractor (dimensions, permissions, orphan guard), `check_tesseract_available` (mock subprocess), `pdf_info` text_coverage shape + caching + 500-page performance bound (<2 s), `pdf_read_pages` render path (cache hit, DPI clamp, bidirectional), `pdf_render_pages` (summary block, image blocks, truncation, orthogonality with OCR, error in summary), `pdf_read_pages` OCR path (Tesseract pre-flight, cache hit, empty result, native-text skip, `MAX_OCR_PAGES_LIMIT`, lang forwarding, FTS integration), `pdf_search` source field (all return paths).
- New `tests/test_integration.py` — end-to-end tests using a synthetically scanned PDF (Pillow-rendered text embedded as raster): scan detection runs unconditionally; OCR extraction, FTS searchability, and render validity tests skip gracefully when Tesseract is not installed.

### Security
- URL fetching is now HTTPS-only — `http://` URLs are rejected with an actionable error message. Previously both schemes were accepted.
- SSRF private-IP check replaced with an explicit, auditable CIDR block list (`127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.0.0/16`, `0.0.0.0/8`, `::1/128`, `fc00::/7`, `fe80::/10`) checked with IP-version-aware matching. Previously used Python's `is_private`/`is_loopback` properties whose semantics shifted between Python versions.
- New optional access-control config at `~/.config/pdf-mcp/config.toml` — `[paths]` allow/deny rules for local file sources and `[urls]` allow/deny rules for URL hosts. Rules use shell-glob patterns; deny wins on conflict; `~` is expanded; path matching operates on the real (symlink-resolved) path to prevent traversal bypasses. The SSRF floor (HTTPS-only + CIDR blocks) is always enforced regardless of config. Missing config = permissive; malformed config = server refuses to start (never silently falls back to permissive).
- CI now installs via `uv sync --frozen` (replacing `pip install -e .[dev]`) so the committed `uv.lock` is enforced on every build — dependency drift fails loudly.

---

## [1.8.0] - 2026-04-16
### Changed
- `pdf_search` gains a `mode` parameter: `"keyword"` (default, existing behaviour), `"semantic"` (embedding-based), or `"auto"` (hybrid RRF — runs both and fuses results)
- In hybrid mode (`mode="auto"`), keyword and semantic results are fused via Reciprocal Rank Fusion (RRF, k=60); `search_mode` field in the response reflects which path ran (`"keyword"`, `"semantic"`, or `"hybrid"`)
- `index_used` boolean replaced by `search_mode` string across all `pdf_search` response shapes
- Semantic-only and hybrid responses omit `total_matches`/`page_match_counts` (FTS5 counts are not meaningful for embedding-ranked results)
- `pdf_semantic_search` tool removed — all search modes are now available through `pdf_search`; tool count is 7

### Added
- `_rrf_fuse()` helper in `server.py` — merges two ranked lists using RRF scoring; covered by unit tests
- Hybrid mode falls back to keyword-only when `fastembed` is not installed, with `search_mode: "keyword"` in the response
- `scripts/benchmark_rrf.py` — agentic benchmark verifying hybrid search quality on real public PDFs (arXiv 1706.03762, 2005.14165), organized into 3 task groups mirroring how AI agents use search: Q&A (metric: MRR; scenarios 1a precise factual, 1b conceptual, 1c mixed/router-trap), Context Building (metric: Recall@K; scenarios 2a clustered pages, 2b scattered pages / true fusion), and Navigation (metric: Recall@1; scenarios 3a exact section heading, 3b cross-reference by concept); every scenario includes a router comparison column; latency measured per task group (3 warm-cache runs, median); k-sensitivity sweep on scenario 1b (k=10,30,60,120); outputs JSON + plain-text reports to `benchmark_results/`
- `benchmark_data/ground_truth.json` — manually annotated ground truth (PDF URLs, queries, relevant page sets) for all 7 benchmark scenarios; stable across runs

### Tests
- 204 new server tests covering `mode="keyword"`, `mode="semantic"`, `mode="auto"` paths, RRF fusion correctness, fastembed-absent fallback, and `_rrf_fuse()` unit tests
- `pdf_semantic_search` test class removed (functionality now tested via `pdf_search` mode tests)

---

## [1.7.0] - 2026-04-05
### Changed
- `pdf_info` no longer returns the full `toc` array when a document has more than 50 TOC entries (~1000 token budget); instead returns `toc_entry_count` and `toc_truncated: true` — call `pdf_get_toc` to retrieve the full outline. PDFs with ≤50 entries continue to include `toc` inline. This prevents PowerPoint-exported PDFs (one bookmark per slide) from producing 10k+ token responses.

### Added
- `pdf_semantic_search(path, query, top_k=5)` — new MCP tool that finds the most relevant PDF pages by meaning, not keywords; searching "revenue growth" matches pages about "sales increase" or "financial performance"
- Embeddings generated locally using `BAAI/bge-small-en-v1.5` (384-dim, ONNX Runtime via `fastembed`) — no external API or GPU required
- Embeddings cached in SQLite as raw `float32` BLOBs; first call for a document indexes all pages (e.g. ~291 ms for a 200-page PDF); subsequent queries rank in under 5 ms
- Response includes `results` (page, score, snippet), `total_pages_searched`, `embedding_cache_hits`, `embedding_cache_misses`, and `model` fields
- `embedding_pages` field in `pdf_cache_stats` response
- `[semantic]` optional install extra: `pip install 'pdf-mcp[semantic]'`; server starts and all existing tools work without it
- Clear `ImportError` with install hint returned from `pdf_semantic_search` when `fastembed` is not installed

### Fixed
- Automatic SQLite schema migration on startup: stale `page_tables`, `pdf_metadata`, and `page_text` tables (created by versions before v1.5.0) are silently dropped and recreated, preventing `no such column: data` errors for users upgrading from v1.4.0 or earlier
- `page_embeddings` is protected from unnecessary drops during migration — only dropped if its own schema is broken, preserving cached embeddings across upgrades

### Changed
- Cache invalidation, `clear_all()`, and `clear_expired()` now also remove stale `page_embeddings` rows

### Tests
- 22 new tests: 4 unit tests for `embedder.py` (all mocked — no model download), 8 cache tests (`TestPageEmbeddingsTable`, `TestPageEmbeddingsCRUD`, `TestPageEmbeddingsLifecycle`), 11 server integration tests (`TestPdfSemanticSearch`) covering cache miss/hit lifecycle, empty-page exclusion, score ordering, `top_k` clamping, and missing-dependency error path
- 7 new migration tests: `TestGetColumns` (2) verifying the `_get_columns` helper; `TestSchemaMigration` (5) covering stale-schema drop-and-recreate for `page_tables`, `pdf_metadata`, `page_text`, and both paths of the `page_embeddings` guard (preserve valid, recreate broken)

## [1.6.0] - 2026-03-27
### Security
- Pin all GitHub Actions to exact commit SHAs to prevent tag-hijacking supply-chain attacks
- Add `pip audit` step to CI and publish workflows to catch known CVEs on every build
- Add `dependency-review.yml` workflow to block high-severity dependencies and denied licenses on PRs
- Add `permissions: contents: read` to all workflows (least-privilege)
- Add `pip-audit` to dev dependencies
- Commit `uv.lock` for reproducible builds (removed from `.gitignore`)

### Changed
- `pdf_search` now uses a SQLite FTS5 full-text index with Porter stemming and BM25 relevance ranking
- First search builds the FTS5 index (same cost as before); every subsequent search is O(log N) instead of O(N) page scan
- Results are ordered by BM25 relevance score rather than page number
- Response schema: `page_match_counts` replaces `pages_with_matches`; each match now includes a `score` field; new `index_used` flag; `total_matches` is always accurate (no early-exit truncation)
- Graceful fallback to Python scan when FTS5 is unavailable (older SQLite builds)

### Added
- `fts_indexed_pages` field in `pdf_cache_stats` response
- Empty/whitespace query validation in `pdf_search` (returns error before opening PDF)

### Tests
- 35 new cache unit tests (`TestFTS5Cache`) covering FTS5 index population, deduplication, invalidation, and fallback behaviour
- 18 new server integration tests (`TestPdfSearchFTS5`) covering fully-indexed, cold/partial, and Python-fallback search paths
- `scripts/compare_search.py`: standalone comparison report proving BM25 ranking, Porter stemming, and ≥3× performance improvement over Python scan

## [1.5.0] - 2026-03-21
### Added
- `pdf_read_pages` now always includes per-page `tables` and `table_count` fields in each page dict, mirroring the existing `images`/`image_count` pattern
- New `total_tables` field in `pdf_read_pages` response (sum of `table_count` across all pages)
- Table extraction uses PyMuPDF's `find_tables()` with visible-line detection; pages without detectable borders return `tables: []`
- Table cache layer in SQLite (`page_tables` table) with the same mtime-based invalidation as text and image caches; empty-list sentinel prevents redundant re-extraction on tableless pages

### Fixed
- Suppress PyMuPDF/SWIG `DeprecationWarning` (`builtin type swigvarlink has no __module__ attribute`) that leaked noisy output to MCP clients on every server start and shutdown

## [1.4.0] - 2026-03-14
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
