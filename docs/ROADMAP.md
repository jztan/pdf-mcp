# Roadmap

## Project Status

- **Current version:** v1.22.0 (released 2026-07-25)
- **MCP Registry:** Published
- **Test suite:** 1351 tests across unit, integration, and retrieval-quality benchmarks (1347 fast + 4 `slow`). OCR tests skip cleanly when system Tesseract is absent. The `test_benchmark_*` files are fast unit tests for the benchmark scripts' helpers; billed/multi-minute checks (the LLM-judge coherence eval and the RRF v2 retrieval gate, both `slow`) are excluded from the release gate, which runs `pytest -m "not slow"`.
- **Tools:** 10 released (`pdf_info`, `pdf_read_pages`, `pdf_read_all`, `pdf_search`, `pdf_get_toc`, `pdf_render_pages`, `pdf_extract_chart`, `pdf_cache_stats`, `pdf_cache_clear`, `server_info`); 13 on `develop`, adding the corpus trio (`pdf_corpus_warm`, `pdf_corpus_overview`, `pdf_corpus_search`) queued for v2.0.0

---

## Next Release

**v2.0.0, queued on `develop`** (CHANGELOG `[Unreleased]` populated; no release branch open yet). The headline is multi-document work:

- **Corpus tools**: `pdf_corpus_warm` / `pdf_corpus_overview` / `pdf_corpus_search` over a folder of local PDFs (cap 100): budgeted warming with resume, per-doc triage cards, and cross-document search fusing per-document rankings via RRF with a term-coverage-weighted tie-break. Benchmarked: hybrid NDCG@10 0.674 and doc-hit@3 1.000 on a 100-doc corpus, sub-0.5s/query warmed.
- **Concurrent warming**: process-pool extraction on larger corpora, ~3.9x faster text warm at the 100-doc cap.
- **Semantic-by-default**: `fastembed` promoted to a core dependency, so hybrid `auto` search works on every install channel without an extra.
- Plus: OR-fallback for zero-result keyword queries, excerpt-picker fixes (hyphen folding, numeric tie-break), demo v2 with the interactive corpus flow, and the 2026-08-01 field-testing hardening (client-timeout guidance for warms, never-null title contract unified across overview and search, full cache clear now reclaims disk).

Cut from `develop` via `python scripts/release.py major` per [`RELEASE_SOP.md`](../docs_internal/RELEASE_SOP.md).

---

## Tracking MCP 2026-07-28

The MCP spec [release candidate locked on 2026-05-21](https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/), with GA targeted for 2026-07-28. Protocol-level work is gated on fastmcp shipping support for the new spec; the goal is a single coordinated protocol release rather than dribbling breaking changes across patches. (Version note: v2.0.0 ships the corpus/semantic feature set above, not this protocol work; the protocol bump will land as its own major, v3.0.)

**v3.0 scope (protocol major, gated on fastmcp v4):**

- [ ] **Stateless transport.** Adopt the new request model once fastmcp supports it. The `initialize` handshake and `Mcp-Session-Id` header are removed by the spec; per-request `_meta` replaces them. STDIO is the only transport pdf-mcp ships, so HTTP-routing additions (`Mcp-Method` / `Mcp-Name` headers, header-based load balancing) are no-ops to verify.
- [ ] **Error-code update.** Confirm fastmcp surfaces missing-resource errors as JSON-RPC `-32602` (was MCP-custom `-32002`). pdf-mcp's inline error contract (`{"error": ...}` with `status=OK`) sidesteps this for tool-level validation; only the framework "resource not found" path is affected.
- [ ] **Cacheable read-side responses.** Add `ttlMs` + `cacheScope` hints to slow-changing read tools (`pdf_info`, `pdf_get_toc`, `pdf_read_pages`). pdf-mcp already has authoritative mtime-based invalidation in SQLite — surfacing the metadata lets MCP clients skip redundant calls within a session. `cacheScope` = per-session (matches single-user STDIO model).
- [ ] **JSON Schema 2020-12.** Use composition operators (`oneOf`, `anyOf`, conditionals) to express `pdf_search`'s `mode × granularity` constraints and `pdf_info`'s `detail` flag. Land alongside the fastmcp v4 bump.

**v3.1+ (post-spec GA, gated on host adoption):**

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

**Last Updated:** 2026-08-01 (Synced: **v1.22.0** released 2026-07-25 with Python 3.14 support, deterministic dedup'd multi-column extraction, and the multi-term CJK keyword fix. `develop` now carries the v2.0.0 queue described under Next Release. Protocol track renumbered from v2.0 to v3.0, freeing v2.0.0 for the corpus/semantic release. Test count 1103 to 1351.)
