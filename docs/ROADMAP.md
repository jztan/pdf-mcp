# Roadmap

## Project Status

- **Current version:** v1.18.0 (released 2026-06-27)
- **MCP Registry:** Published
- **Test suite:** 890 tests across unit, integration, and retrieval-quality benchmarks. OCR tests skip cleanly when system Tesseract is absent. The `test_benchmark_*` files are fast unit tests for the benchmark scripts' helpers; billed/multi-minute checks (the LLM-judge coherence eval and the RRF v2 retrieval gate, both `slow`) are excluded from the release gate, which runs `pytest -m "not slow"`.
- **Tools:** `pdf_info`, `pdf_read_pages`, `pdf_read_all`, `pdf_search`, `pdf_get_toc`, `pdf_render_pages`, `pdf_cache_stats`, `pdf_cache_clear`, `server_info`

---

## Next Release (1.19.0)

No release branch open. Merged on `develop` (CHANGELOG `[Unreleased]`), awaiting the cut:

- **CJK keyword search fix** — Japanese / Chinese / Korean keyword queries now return hits for terms embedded in unspaced text. A parallel pair of character-split FTS5 indexes (`pdf_search_fts_cjk` / `pdf_section_fts_cjk`, `unicode61`, one codepoint per token) is queried with phrase semantics for CJK-containing queries, while English/Latin queries stay on the untouched `porter unicode61` index — **English keyword ranking is unchanged** (verified by the RRF v2 gate: NDCG@10 still 0.625/0.656/0.767). Excerpts come from original page text; existing caches rebuild only the FTS layer on first open (no re-extract, no re-embed). Measured CJK keyword recall on the local vertical-jp corpus: 1.00 across 13 graded queries; `厚木基地` recovers 0→3 hits. Chosen over the trigram/segmenter redesign originally scoped, which would have dropped Porter stemming for English. The `cjk_keyword_warning` advisory is removed (no longer needed).

Feature-complete on branch `feature/content-trust-hidden-text`, pending merge to `develop`:

- **Content-trust / hidden-text detection.** Flags text a human reader cannot see — invisible render mode (mode 3), sub-point fonts, transparent fill, white-on-white, off-page runs — via PyMuPDF `get_texttrace()` geometry (zero new deps). Opt-in `content_trust` block on `pdf_info` (counts, per-signal breakdown, flagged pages; per-span detail under `detail=True`), plus an always-on `hidden_text_detected` flag on `pdf_read_pages` / `pdf_read_all` (the path that actually returns the text). The safety boundary is geometry (language-agnostic); a best-effort English `injection_in_hidden` count is a severity hint over hidden spans only, never the trigger. Legitimate searchable-OCR layers (invisible text over a page image) are exempt. **Detection is flag-only — extraction is never altered.** Document-level scan + per-page flag are cached, invalidated together by a global trust-version. Synthetic benchmark: precision/recall **1.000** on a 14-file English+CJK corpus with clean controls (`scripts/benchmark_content_trust.py`). Real-world spot-check: zero false positives on real arXiv PDFs (incl. a 7 MB image-heavy paper); catches the July-2025 arXiv white-1pt "positive review only" technique applied to a real paper. **Follow-up:** wire a tracked real-world sample set (in-the-wild injected PDFs are scarce — the arXiv offenders were scrubbed post-exposure and research datasets are private), and the spec's deferred items (occlusion detection, configurable/non-English phrase list, `pdf_search` flag). Surfaced 2026-06-24 via competitive analysis vs `pdf-reader-mcp`.

Cut from develop via `python scripts/release.py minor` per [`RELEASE_SOP.md`](../docs_internal/RELEASE_SOP.md) — the `[Unreleased]` block already documents all entries.

> **Shipped in v1.18.0** (2026-06-27): RRF benchmark v2 keyword-NDCG gate (one-time finding: hybrid RRF beats single modes, **0.767 vs 0.625 keyword / 0.656 semantic**), single-column extraction pre-gate, per-user URL download cache (issue #15), relative-redirect TLS fix (issue #16), and the metadata `chore(packaging)` commits. See [`CHANGELOG.md`](../CHANGELOG.md).

---

## Tracking MCP 2026-07-28

The MCP spec [release candidate locked on 2026-05-21](https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/), with GA targeted for 2026-07-28. Protocol-level work is gated on fastmcp shipping support for the new spec; the goal is a single coordinated v2.0 release rather than dribbling breaking changes across patches.

**v2.0 scope (target: Q3 2026, gated on fastmcp v4):**

- [ ] **Stateless transport.** Adopt the new request model once fastmcp supports it. The `initialize` handshake and `Mcp-Session-Id` header are removed by the spec; per-request `_meta` replaces them. STDIO is the only transport pdf-mcp ships, so HTTP-routing additions (`Mcp-Method` / `Mcp-Name` headers, header-based load balancing) are no-ops to verify.
- [ ] **Error-code update.** Confirm fastmcp surfaces missing-resource errors as JSON-RPC `-32602` (was MCP-custom `-32002`). pdf-mcp's inline error contract (`{"error": ...}` with `status=OK`) sidesteps this for tool-level validation; only the framework "resource not found" path is affected.
- [ ] **Cacheable read-side responses.** Add `ttlMs` + `cacheScope` hints to slow-changing read tools (`pdf_info`, `pdf_get_toc`, `pdf_read_pages`). pdf-mcp already has authoritative mtime-based invalidation in SQLite — surfacing the metadata lets MCP clients skip redundant calls within a session. `cacheScope` = per-session (matches single-user STDIO model).
- [ ] **JSON Schema 2020-12.** Use composition operators (`oneOf`, `anyOf`, conditionals) to express `pdf_search`'s `mode × granularity` constraints and `pdf_info`'s `detail` flag. Land alongside the fastmcp v4 bump.

**v2.1+ (post-spec GA, gated on host adoption):**

- [ ] **Tasks Extension** for long-running operations: OCR on large scans and first-time embedding indexing. The redesigned API is stateless (server returns a handle; client drives `tasks/get` / `tasks/update` / `tasks/cancel`) and maps cleanly onto SQLite-backed job state. Gate on whether Claude Desktop ships task-extension UI — without host support there's no user-visible win.
- [ ] **MCP Apps** (server-rendered HTML in sandboxed iframe) for `pdf_render_pages`. Today the tool returns PNG file paths; an iframe UI could embed thumbnails / a page navigator inline with audit/consent parity. Experiment only once adoption is clear.

**Out of scope for this track:** Roots, Sampling, and protocol-level Logging are deprecated by 2026-07-28 but pdf-mcp uses none, so the 12-month removal window is a no-op. The 6 OAuth/OIDC SEPs do not apply — pdf-mcp has no auth surface.

---

## Under Consideration

Ordered by leverage ÷ effort. P0 = ship next; P3 = methodology, fold into a P1/P2 item rather than tracking standalone.

### P0 — ship next

_Nothing queued._

### P1 — high-value, well-scoped

- [ ] **Calibrate the semantic confidence threshold.** The current `_SEMANTIC_CONFIDENCE_THRESHOLD = 0.5` is a guess; re-eval found gibberish queries scoring 0.54 against unrelated papers under `BAAI/bge-small-en-v1.5`. Needs an empirical pass over (corpus, gibberish-query, real-query) tuples to pick a defensible floor (likely 0.6–0.65, possibly per-model), documented in [`docs/embedding-models.md`](embedding-models.md). Optional follow-up: per-corpus self-calibration mode.

### P2 — investigate before committing

- [ ] **Layout-aware section-detector escalation.** _Not started. Distinct from the shipped `pdf-mcp[multicolumn]` extra: that fixed column **reading order** (v1.15.0); this is about section **boundary** detection._ The 7-signal heuristic in `section_detector.py` underperforms on OCR'd scans and layout-irregular preprints. If revisited, spike a layout-aware model (GROBID / Marker / Surya) on accuracy lift, install size, and licensing before budgeting.

### P3 — methodology, fold into a P1/P2 item

- [ ] **Embedding-distance "coherence" scorer to guard the column-detection path in CI.** _Partially addressed. Two coherence tools now exist: the token-sequence reading-order benchmark (v1.15.0 — `scripts/benchmark_reading_order.py`: `reading_order_score`, `normalize_tokens`, `classify_columns` + `benchmark_data/reading_order_corpus.json`) and the LLM-judge coherence eval harness (v1.17.0 — `scripts/eval_coherence.py`, `test_coherence_no_regression_vs_baseline`, marked `slow`/billed). What's still missing is a **cheap, unbilled, CI-runnable embedding-distance scorer.**_ The containment-based excerpt benchmark is blind to reading-order scrambling — the answer substring survives column interleaving, so containment stayed flat through the two-column reading-order fix (shipped as `pdf-mcp[multicolumn]`). An embedding-distance metric — embed a paragraph-mode excerpt, embed the same text in canonical order, compare — should be ~0 on single-column, large pre-fix on two-column, ~0 post-fix. Reuse the existing corpus and scaffolding; **swap the token-sequence scorer for an embedding-distance scorer**. Unlike the billed LLM-judge harness, this could run on every CI push to catch regressions in `detect_column_boxes` / column extraction that containment cannot see.

- [ ] **Agent-task evaluation for section vs page search.** Current benchmarks measure retrieval characteristics; this would measure whether section-granularity actually helps agents *answer better questions* (LLM-graded Q&A or agent-task completion). Not a deliverable on its own — bundle the harness into whichever P1/P2 item needs it first (likely the confidence-threshold calibration).

---

## Investigated / Rejected

Paths prototyped or benchmarked and then deliberately closed are logged separately in [`investigated-rejected.md`](investigated-rejected.md) (hybrid section search, default embedding-model benchmark, text-extraction parallelism, the MLX backend fork, boilerplate stripping).

---

## Release History

For per-release detail (features, fixes, CVE patches, breaking changes), see:

- [`CHANGELOG.md`](../CHANGELOG.md) — canonical changelog, every version since v1.0
- [GitHub Releases](https://github.com/jztan/pdf-mcp/releases) — release notes with installation instructions

---

**Last Updated:** 2026-06-27 (v1.18.0 released; CJK keyword search fix merged to develop (`8d24850`) for 1.19.0; **content-trust / hidden-text detection feature-complete on `feature/content-trust-hidden-text`, pending merge** — adds the `content_trust.py` module, `content_trust` block on `pdf_info`, and `hidden_text_detected` flag on the read tools; +~30 tests, synthetic benchmark 1.000/1.000)
