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

### v1.11.0 — Bring-Your-Own Embedding Model (BYOM)
- `[embedding] model = "..."` in `~/.config/pdf-mcp/config.toml` — swap the embedding model per-install without touching code
- Default remains `BAAI/bge-small-en-v1.5`; missing key is fully backward-compatible
- `embedder.py` singleton is now model-aware: reloads `TextEmbedding` automatically when the configured model changes mid-process
- `check_available(model_name)` validates the model name against fastembed's local supported-model list before any PDF work; error message includes the full list of valid names
- `page_embeddings` cache gains a `model` column; stale rows from a prior model are evicted automatically on the next search — no manual cache clear needed
- `model` field added to semantic and hybrid `pdf_search` responses
- `embedding_model` field added to `pdf_cache_stats` response
- New `docs/embedding-models.md` — MTEB retrieval benchmark comparison across 9 fastembed models (fast English, high-quality English, long-context, multilingual) with size, license, and a selection guide
- 13 new tests across `test_pdf_reader.py`, `test_server.py`, `test_embedder.py`, `test_cache.py`

### v1.12.0 — LLM-evaluation fixes round 1 (2026-05-12)
- `pdf_search` hybrid mode used to ship a stale, pre-fusion `total_matches` (and `page_match_counts`) alongside the post-RRF matches array, producing self-contradicting payloads like `matches=[5 items], total_matches=0`. Both fields are now recomputed from the fused result set. Semantic mode also gains `total_matches`/`page_match_counts` so the schema is consistent across all three modes.
- Tokenised `_escape_fts5_query`: keyword queries like `"pgvector latency"` no longer require adjacency. Tokenise on whitespace, strip FTS5 reserved chars, join with implicit AND. BM25 still ranks.
- Auto-mode no longer crashes with `ToolError` when fastembed is installed but the embedding model can't load (offline machine, HF outage). Degrades to keyword with `semantic_unavailable=true` and a reason string.
- Heuristic section detector rejects line candidates longer than 200 chars to suppress body paragraphs that happen to start with a heading-shaped prefix.
- **BREAKING**: `pdf_info.text_coverage` shape changed from `list[{page, text_chars, raster_images}]` to a compact dict. Default response now contains only a constant-size `summary` (page-count rollups + truncated OCR candidate list); a 3000-page PDF no longer ships ~6000 ints just for coverage. Pass `pdf_info(path, detail=True)` to opt into the per-page parallel arrays.
- Per-match `low_confidence` + response-level `all_low_confidence` / `confidence_threshold` on semantic-mode `pdf_search` results.
- Browser demo (`pages/index.html`) and README updated for the new `text_coverage` shape; demo footer bumped to v0.3.

### v1.12.1 — LLM-evaluation fixes round 2
- `pdf_search.total_matches` could disagree with `len(matches)` in keyword mode after the 1.12.0 tokenisation fix — multi-word queries like `pgvector latency` returned 4 matches with `total_matches: 0` because the literal phrase didn't appear anywhere even though both tokens did. `total_matches` now equals `len(matches)` in every mode, and `get_fts_page_counts` counts token occurrences so `page_match_counts` keeps its per-page intensity signal for keyword mode. A property test (`test_total_matches_equals_len_matches_property`) asserts the invariant across modes × queries in CI.
- Hybrid-mode `low_confidence` flag — true only when there's no keyword hit on the page AND the underlying semantic cosine is below `confidence_threshold`. Pages with literal-term hits stay confident regardless of cosine. Each hybrid match also exposes its `semantic_score` so the agent can see the raw cosine alongside the RRF score it's ranking on.
- Semantic-mode `all_low_confidence` renamed to `all_results_low_confidence` for parity with the new hybrid-mode rollup.
- Section-title honesty: heuristic candidates that pass the scored threshold but fail a stricter `_looks_like_clean_heading` shape check (≤120 chars, no mid-string `. ` or `; `) now emit a section boundary with `title=None`. Each section carries a `title_source` field (`"toc"` | `"heading_detected"` | `null`) set at detection time on the `Section` dataclass and persisted on a new `title_source UNINDEXED` column of `pdf_section_fts`. Schema migration drops and recreates the pre-1.12.1 section FTS table; sections re-index lazily on the next section-mode call per PDF.

---

## Planned

---

## Investigated / Rejected

Items that were actively prototyped or benchmarked and then deliberately closed.

- **Hybrid (BM25 + semantic) section search** (2026-05-04) — Built a full Phase-1 validation pipeline on `feature/hybrid-section-validation` (15 commits, 550 tests), then ran a literature review + 45-query confirmation calibration across the three arxiv benchmark PDFs. Found: (a) hybrid RRF gives only ~5% lift over BM25 on scientific papers (BEIR SciFact), well below the spec's 0.10 MRR gate threshold; (b) severe **lexical regression** at section grain (0.93 → 0.63 MRR, −33%) because RRF dilutes BM25's clean rank-1 signal on title-style queries; (c) the existing v1.8.0 page-grain hybrid is **3× better** on paraphrase queries (0.19 vs 0.07). No query class where `hybrid-section` wins. SOTA scientific-paper QA systems (e.g., PaperQA2) use semantic + LLM rerank, not BM25/dense fusion.
- **Default embedding model benchmark** (2026-05-09) — Live MRR + latency benchmark of 4 fast English fastembed models (`bge-small`, `arctic-embed-s`, `bge-base`, `arctic-embed-m`) on the 7-scenario arxiv ground truth via the new `scripts/benchmark_embedding_models.py`. Gate: MRR lift ≥ 0.05 AND p50 latency ≤ 1.5× baseline. Result: `bge-small-en-v1.5` wins by 0.116 MRR over the best challenger (0.806 vs 0.690); no challenger met the lift threshold. arctic-embed-m collapsed to MRR 0.029, likely a missing query/passage prefix protocol fastembed doesn't apply. Default kept; full numbers in `docs/embedding-models.md`.
---

## Future / Under Consideration

- **Calibrate the semantic confidence threshold** (from 1.12.0 LLM re-evaluation) — current `_SEMANTIC_CONFIDENCE_THRESHOLD = 0.5` is a guess. Re-eval reported gibberish query `"xyzzy plover frobnicate"` scoring **0.5425** against a vector-database paper with `BAAI/bge-small-en-v1.5` — above the floor, so `low_confidence=False` despite being topically unrelated. bge-small gives moderate cosine to any short input. Need an empirical calibration pass: assemble a small set of (corpus, gibberish-query, real-query) tuples; measure cosine score distributions; pick a threshold that puts the gibberish below and the real queries above (likely 0.6–0.65, possibly per-model). Document the calibration in `docs/embedding-models.md` next to the existing MTEB comparison so future model swaps know whether to retune. Optional follow-up: a per-corpus calibration mode that observes empirical score distributions on the user's own PDF and self-adjusts.
- **Unify `_resolve_path` errors with the inline-error contract** (from 1.11.0 LLM evaluation) — `_resolve_path` raises `ToolError` on bad paths/URLs while page-spec errors return inline `{"error": ..., "hint": ...}`. Agents end up needing both recovery paths. Unify on the inline-error pattern so all path/url validation failures return a payload with `status=OK` and an `error` field, mirroring the page-spec contract. Touches every tool that calls `_resolve_path`, so plan as a coordinated refactor with shape tests.
- **Two-column / complex layout reading order** (from 1.11.0 LLM evaluation) — PyMuPDF's `get_text("text", sort=True)` does not understand column structure, so academic-paper two-column layouts come back with paragraphs interleaved across columns. Investigate `pymupdf-pro`'s layout analysis (or alternatives like GROBID/Marker/Surya) as an optional `pdf-mcp[layout]` extra. Overlaps with the existing escalation item below.
- **`pdf_render_pages` page labels** — each `ImageContent` block currently has no page annotation; if `render_failed_pages` fires, surviving images could be misaligned. FastMCP `Image` supports an `annotations` field — embed `{"page": N}` in each block so agents can correlate images to pages regardless of failures.
- **Heuristic detector escalation for low-quality PDFs** — for PDFs where the 7-signal detector underperforms (e.g., scanned PDFs after OCR, layout-irregular preprints), explore CRF-based or transformer-based layout detection (GROBID, Marker, Surya). Heavier dependency footprint; would be an optional `pdf-mcp[layout]` extra.
- **Agent-task evaluation for section vs page search** — the existing benchmark measures retrieval characteristics. A downstream eval (LLM grading on Q&A tasks, or agent-task completion benchmarks) would measure whether agents *answer better questions* with section-granularity, not just whether retrieval recalls more content.
