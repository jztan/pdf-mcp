# Roadmap

## Project Status

- **Current version:** v2.2.1 (released 2026-08-22)
- **MCP Registry:** Published (v2.1.0)
- **Test suite:** 1687 tests in `tests/` across unit, integration, and retrieval-quality benchmarks (1683 fast + 4 `slow`); archived spikes under `scripts/archive/` are not counted and no gate runs them. OCR tests skip cleanly when system Tesseract is absent. CI runs the suite on Linux (Python 3.10 to 3.14) and on Windows (3.10 and 3.13), the latter added with the engine swap; it installs Tesseract so the OCR paths actually run there. The `test_benchmark_*` files are fast unit tests for the benchmark scripts' helpers; billed/multi-minute checks (the LLM-judge coherence eval and the RRF v2 retrieval gate, both `slow`) are excluded from the release gate, which runs `pytest -m "not slow"`.
- **Tools:** 13 released (`pdf_info`, `pdf_read_pages`, `pdf_read_all`, `pdf_search`, `pdf_get_toc`, `pdf_render_pages`, `pdf_extract_chart`, `pdf_corpus_warm`, `pdf_corpus_overview`, `pdf_corpus_search`, `pdf_cache_stats`, `pdf_cache_clear`, `server_info`)
- **Transports:** STDIO (`pdf-mcp`) and single-tenant HTTP (`pdf-mcp-http`) both released; multi-arch Docker images published at `ghcr.io/jztan/pdf-mcp` (linux/amd64 + linux/arm64), tagged per release

---

## Next Release

**Unqueued, and the version is a decision to make rather than a default.** `CHANGELOG.md`'s `[Unreleased]` now holds the PyMuPDF replacement: the runtime engine is a permissive stack (pypdfium2, pdfplumber, pypdf, pytesseract), so the declared MIT licence is true of the whole install for the first time, and `pymupdf4llm` with its Polyform Noncommercial transitive dependency is gone. Alongside it are four user-facing fixes: CJK keyword search finding nothing in a corpus-warmed document, OCR returning nothing on a default Windows Tesseract install, cold `pdf_search` costing 17.5s on Windows against 3.2s on Linux, and zero-height search bboxes for documents using unembedded fonts.

No tool signature or response field changes, and quality was gated against the PyMuPDF baseline on the full benchmark set before the swap. Whether that reads as a minor or a major cut is a judgement about how loudly to signal the engine change to existing users, not something the diff decides. Cut from `develop` via `python scripts/release.py <minor|major>` per [`RELEASE_SOP.md`](../docs_internal/RELEASE_SOP.md).

---

## Tracking MCP 2026-07-28

The MCP spec **shipped GA on 2026-07-28** ([announcement](https://blog.modelcontextprotocol.io/posts/2026-07-28/)). Protocol-level work is now gated only on fastmcp shipping support for it; the goal is a single coordinated protocol release rather than dribbling breaking changes across patches. The v2.x releases are feature work, not this track; the protocol bump lands as its own major, v3.0.

**v3.0 scope (protocol major, gated on fastmcp v4):**

- [ ] **Stateless transport.** Adopt the new request model once fastmcp supports it. The `initialize` handshake and `Mcp-Session-Id` header are removed by the spec; per-request `_meta` replaces them. Now that `pdf-mcp-http` has shipped, the HTTP-routing additions (`Mcp-Method` / `Mcp-Name` headers, header-based load balancing) are real surface to verify rather than no-ops, though single-tenant-by-contract means header-based load balancing across instances is not a deployment pdf-mcp supports.
- [ ] **Error-code update.** Confirm fastmcp surfaces missing-resource errors as JSON-RPC `-32602` (was MCP-custom `-32002`). pdf-mcp's inline error contract (`{"error": ...}` with `status=OK`) sidesteps this for tool-level validation; only the framework "resource not found" path is affected.
- [ ] **Cacheable read-side responses.** Add `ttlMs` + `cacheScope` hints to slow-changing read tools (`pdf_info`, `pdf_get_toc`, `pdf_read_pages`). pdf-mcp already has authoritative mtime-based invalidation in SQLite; surfacing the metadata lets MCP clients skip redundant calls within a session. `cacheScope` = per-session (matches single-user STDIO model).
- [ ] **JSON Schema 2020-12.** Use composition operators (`oneOf`, `anyOf`, conditionals) to express `pdf_search`'s `mode × granularity` constraints and `pdf_info`'s `detail` flag. Land alongside the fastmcp v4 bump.

**v3.1+ (post-spec GA, gated on host adoption):**

- [ ] **Tasks Extension** for long-running operations: OCR on large scans and first-time embedding indexing. The redesigned API is stateless (server returns a handle; client drives `tasks/get` / `tasks/update` / `tasks/cancel`) and maps cleanly onto SQLite-backed job state. Gate on whether Claude Desktop ships task-extension UI: without host support there's no user-visible win.
- [ ] **MCP Apps** (server-rendered HTML in sandboxed iframe) for `pdf_render_pages`. Today the tool returns PNG file paths; an iframe UI could embed thumbnails / a page navigator inline with audit/consent parity. Experiment only once adoption is clear.
- [ ] **File transfer ([SEP-2631](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2631), watch item).** The only way a caller could hand the HTTP transport a *new* document. `files/authorizeUpload` negotiates an out-of-band HTTPS transfer and passes the tool a file URI, keeping bytes out of JSON-RPC; `x-mcp-file.transferModes` lets a server require that mode and refuse inline, which is the posture pdf-mcp wants (inline base64 is unaffordable; see [`docs/investigated-rejected.md`](investigated-rejected.md)). Draft as of 2026-08-02, owned by the File Uploads WG, which closed the inline-data-URI predecessor SEP-2356 in its favor. Gate on acceptance plus fastmcp support. Note the upload is performed by the client application, not the model, so the win requires a host that implements the client half.

**Out of scope for this track:** Roots, Sampling, and protocol-level Logging are deprecated by 2026-07-28 but pdf-mcp uses none, so the 12-month removal window is a no-op. The 6 OAuth/OIDC SEPs do not apply: the stdio entry point has no auth surface, and `pdf-mcp-http` is single-tenant by contract (one shared bearer token, no per-user identity), so an authorization-server flow would authenticate callers it still cannot authorize differently. See [`docs/remote-access.md`](remote-access.md).

---

## Under Consideration

Ordered by leverage ÷ effort.

### P0: ship next

_Nothing queued._

### P1: high-value, well-scoped

- [ ] **Commit `pdf_corpus_warm` embedding progress in page batches, not per document.** The one remaining hard-failure mode behind timeout-bounded MCP clients: warming is atomic per document, so a single very large document (a 370-page 10-K with embeddings) cannot finish inside a ~60s per-call window and loses its in-flight work on every attempt. Field-validated 2026-08-01: small and medium documents already return graceful partials and resume converges a cold corpus in a few re-issued calls, so the failure is isolated to the giant-document path. Fix shape: when a document's text is already cached, commit embeddings in page batches (partial embeddings are already a legal cache state), keeping text extraction atomic. Re-opens the concurrent-warm correctness gate, which was validated against per-document finalize. v2.0.0 shipped the documented mitigation only, unchanged in v2.1.0 (tool description and [`tool-reference.md`](tool-reference.md) teach budget-under-timeout and re-issue-to-continue).
- [ ] **Teach keyword-mode query shape in the tool descriptions.** `mode="keyword"` whitespace-splits and AND-joins terms, so a full-question query over-constrains and silently returns nothing, measured on the corpus ranking spike, where 7 of 36 queries scored 0 on both arms because one absent term zeroed the AND. Nothing in the `pdf_search` / `pdf_corpus_search` descriptions says so. Add short-specific-terms guidance plus mode-choice guidance (`semantic` for conceptual questions, `keyword` for rare exact tokens), since `auto` is the default and nothing signals when an explicit mode does better. No code-path change; validate with a behavioral check rather than paraphrasing, as parts of the corpus description are locked verbatim by `tests/test_tool_descriptions.py`.
- [ ] **Calibrate the semantic confidence threshold.** The current `_SEMANTIC_CONFIDENCE_THRESHOLD = 0.5` is a guess; re-eval found gibberish queries scoring 0.54 against unrelated papers under `BAAI/bge-small-en-v1.5`. Needs an empirical pass over (corpus, gibberish-query, real-query) tuples to pick a defensible floor (likely 0.6–0.65, possibly per-model), documented in [`docs/embedding-models.md`](embedding-models.md). Optional follow-up: per-corpus self-calibration mode.

### P2: investigate before committing

- [ ] **Markdown output mode.** RAG pipelines consume markdown and chunks directly. Expose it as an output format (likely `pdf_read_pages(format="markdown")`; a heading-aware chunk mode could follow, not lead). **Premise changed:** this item assumed `pymupdf4llm.to_markdown()` came for free with the `[multicolumn]` extra, but that dependency has been removed (it pulled `pymupdf_layout`, licensed Polyform Noncommercial). Re-adding it purely for markdown would reintroduce that licence and the `find_tables()` process-wide corruption that a subprocess-isolation worker used to contain. That worker was removed on 2026-08-23 once its cause was gone, so re-adding the dependency would mean rebuilding it. Settle first whether markdown is worth generating natively instead, plus the cache shape (a `page_markdown` table vs on-the-fly).
- [ ] **Rank quality on repetitive corpora: bibliography pages and near-duplicate documents.** Two independently observed precision defects with the same shape: document-level ranking stays correct, but the page list is crowded out. BM25 favours reference-list pages because query terms repeat densely in citation titles (reproduced on both the arXiv corpus and a 113-page thesis), and on a corpus holding several fiscal years of one filer a needle that ranks #1 *inside* its own document misses the corpus-wide top 10 because sibling-year filings occupy the list. Candidate fixes are a citation-density rank penalty and an optional per-document cap or best-page-per-document mode. Each must run its own fix → benchmark → corpus expand → re-benchmark loop; the reranker rejection is a standing warning against shipping an "obvious" ranking tweak unbenchmarked.
- [ ] **Layout-aware section-detector escalation.** _Not started. Distinct from the shipped `pdf-mcp[multicolumn]` extra: that fixed column **reading order** (v1.15.0); this is about section **boundary** detection._ The 7-signal heuristic in `section_detector.py` underperforms on OCR'd scans and layout-irregular preprints. If revisited, spike a layout-aware model (GROBID / Marker / Surya) on accuracy lift, install size, and licensing before budgeting.

### P3: methodology, fold into a P1/P2 item

- [ ] **Embedding-distance "coherence" scorer to guard the column-detection path in CI.** _Partially addressed. Two coherence tools now exist: the token-sequence reading-order benchmark (v1.15.0, `scripts/benchmark_reading_order.py`: `reading_order_score`, `normalize_tokens`, `classify_columns` + `benchmark_data/reading_order_corpus.json`) and the LLM-judge coherence eval harness (v1.17.0, `scripts/eval_coherence.py`, `test_coherence_no_regression_vs_baseline`, marked `slow`/billed). What's still missing is a **cheap, unbilled, CI-runnable embedding-distance scorer.**_ The containment-based excerpt benchmark is blind to reading-order scrambling: the answer substring survives column interleaving, so containment stayed flat through the two-column reading-order fix (shipped as `pdf-mcp[multicolumn]`). An embedding-distance metric (embed a paragraph-mode excerpt, embed the same text in canonical order, compare) should be ~0 on single-column, large pre-fix on two-column, ~0 post-fix. Reuse the existing corpus and scaffolding; **swap the token-sequence scorer for an embedding-distance scorer**. Unlike the billed LLM-judge harness, this could run on every CI push to catch regressions in `detect_column_boxes` / column extraction that containment cannot see.

- [ ] **Agent-task evaluation for section vs page search.** Current benchmarks measure retrieval characteristics; this would measure whether section-granularity actually helps agents *answer better questions* (LLM-graded Q&A or agent-task completion). Not a deliverable on its own: bundle the harness into whichever P1/P2 item needs it first (likely the confidence-threshold calibration).

---

## Investigated / Rejected

Paths prototyped or benchmarked and then deliberately closed are logged in [`investigated-rejected.md`](investigated-rejected.md), with the verdict and the evidence behind each. Read it before re-proposing retrieval, extraction, or embedding work.

---

## Release History

For per-release detail (features, fixes, CVE patches, breaking changes), see:

- [`CHANGELOG.md`](../CHANGELOG.md): canonical changelog, every version since v1.0
- [GitHub Releases](https://github.com/jztan/pdf-mcp/releases): release notes with installation instructions

---

**Last Updated:** 2026-08-08 (synced to v2.1.0; docs restructured, no scope change)
