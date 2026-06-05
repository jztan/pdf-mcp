# Roadmap

## Project Status

- **Current Version:** v1.13.1 (released 2026-05-21)
- **On Develop (unreleased):** paragraph-aware excerpts (`excerpt_style="paragraph"`) for `pdf_search`, starlette 1.0.0 → 1.1.0 bump for PYSEC-2026-161 — see `[Unreleased]` in [`CHANGELOG.md`](CHANGELOG.md)
- **MCP Registry Status:** Published
- **Test Suite:** 696 tests across unit, integration, and retrieval-quality benchmarks. OCR tests skip cleanly when system Tesseract is absent; benchmark tests are kept off the CI fast path.
- **Tools:** 8 MCP tools — `pdf_info`, `pdf_read_pages`, `pdf_read_all`, `pdf_search`, `pdf_get_toc`, `pdf_render_pages`, `pdf_cache_stats`, `pdf_cache_clear`

---

## Next Release

No release branch open. Cut the next patch from develop via `python scripts/release.py patch` per [`RELEASE_SOP.md`](RELEASE_SOP.md) once the `[Unreleased]` block is ready.

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

_(empty — `pdf_render_pages` page-correlation contract landed; see `[Unreleased]` in [`CHANGELOG.md`](CHANGELOG.md).)_

### P1 — high-value, well-scoped

- [ ] **Parallelize OCR with a process pool.** OCR is the slowest operation by far (~2–9 s/page; a multi-page scan blocks the whole call). A `ProcessPoolExecutor` over pages gives a near-linear speedup — **6.3x at 8 workers** on an M4 Pro (53.7 s → 8.5 s; see [`benchmark_data/parallel_pages_results.md`](benchmark_data/parallel_pages_results.md)). Must be processes, not threads: PyMuPDF OCR goes through Leptonica, whose global state is **not thread-safe even across separate `Document` handles** and crashes (`Attempt to use Leptonica from 2 threads at once`). Each worker opens its own `Document` (PyMuPDF documents aren't shareable across workers); a plain page-level pool is enough — chunking (one open per worker) measured no better since PDF opens are cheap. Implementation notes: `max_workers ≈ cpu_count` but capped (gains stay sublinear and spawn cost grows); gate by page count so single-page OCR doesn't pay spawn (~0.3–0.5 s/worker on macOS/Windows); SQLite cache writes stay in the parent. Fits the v2.1 **Tasks Extension** framing above for long OCR jobs.

- [ ] **Calibrate the semantic confidence threshold.** The current `_SEMANTIC_CONFIDENCE_THRESHOLD = 0.5` is a guess; re-eval found gibberish queries scoring 0.54 against unrelated papers under `BAAI/bge-small-en-v1.5`. Needs an empirical pass over (corpus, gibberish-query, real-query) tuples to pick a defensible floor (likely 0.6–0.65, possibly per-model), documented in [`docs/embedding-models.md`](docs/embedding-models.md). Optional follow-up: per-corpus self-calibration mode.

### P2 — investigate before committing

- [ ] **Parallelize `pdf_render_pages` with a process pool.** Same machinery as the OCR item, but a more modest, more variable payoff: **1.6x (synthetic) – 2.2x (real PDFs) at 8 workers**, sublinear and machine-sensitive — render's ~60 ms/page sits in the awkward middle where per-worker spawn cost (~0.3–0.5 s on macOS/Windows) eats a meaningful slice. Worth doing once OCR parallelization lands and shares the helper, but only behind a page-count gate; not worth its own effort otherwise. (For contrast, plain **text extraction stays sequential** — at ~4 ms/page the process pool loses 0.13–0.24x on spawn and reaches at most ~1.6x only on Linux/fork with many pages, never justifying the spawn cost and lost shared cache. Threads help nothing anywhere: GIL-bound at 0.7–1.0x.) Full data in [`benchmark_data/parallel_pages_results.md`](benchmark_data/parallel_pages_results.md).

- [ ] **Layout-aware section-detector escalation.** The 7-signal heuristic section detector underperforms on OCR'd scans and layout-irregular preprints. (The related two-column *reading-order* problem shipped separately as the optional `pdf-mcp[multicolumn]` extra — see `[Unreleased]` in [`CHANGELOG.md`](CHANGELOG.md) — using `pymupdf4llm`'s column detector behind a fail-safe wrapper; the detector-quality gap on scans is what remains.) If revisited, spike a layout-aware model (GROBID / Marker / Surya) on accuracy lift, install size, and licensing before budgeting.

### P3 — methodology, fold into a P1/P2 item

- [ ] **Reading-order "coherence" metric to guard the column-detection path.** The containment-based excerpt benchmark is blind to reading-order scrambling — the answer substring survives column interleaving, so containment stayed flat through the two-column reading-order fix (shipped as `pdf-mcp[multicolumn]`). A coherence metric — embed a paragraph-mode excerpt, embed the same text in canonical order, compare — should be ~0 on single-column, large pre-fix on two-column, ~0 post-fix. Reuse the `benchmark_data/reading_order_corpus.json` corpus and `reading_order_score` scaffolding; swap the token-sequence scorer for an embedding-distance scorer. Catches future regressions in `detect_column_boxes` / column extraction that containment cannot see.

- [ ] **Agent-task evaluation for section vs page search.** Current benchmarks measure retrieval characteristics; this would measure whether section-granularity actually helps agents *answer better questions* (LLM-graded Q&A or agent-task completion). Not a deliverable on its own — bundle the harness into whichever P1/P2 item needs it first (likely the confidence-threshold calibration).

---

## Investigated / Rejected

Items prototyped or benchmarked and then deliberately closed:

- **Hybrid (BM25 + semantic) section search** (2026-05-04) — Built full Phase-1 validation on `feature/hybrid-section-validation` (15 commits, 550 tests) plus a 45-query confirmation calibration. Hybrid RRF gave only ~5% lift over BM25 on scientific papers (below the 0.10 MRR gate) and caused a severe lexical regression at section grain (0.93 → 0.63 MRR, −33%) because RRF dilutes BM25's clean rank-1 on title-style queries. v1.8.0 page-grain hybrid is 3× better on paraphrase queries. No query class where hybrid-section wins. SOTA systems (PaperQA2) use semantic + LLM rerank instead.
- **Default embedding model benchmark** (2026-05-09) — Live MRR + latency benchmark of 4 fast English fastembed models on the 7-scenario arxiv ground truth via `scripts/benchmark_embedding_models.py`. Gate: MRR lift ≥ 0.05 AND p50 latency ≤ 1.5× baseline. `bge-small-en-v1.5` won by 0.116 MRR over the best challenger; no challenger met the lift threshold. arctic-embed-m collapsed to MRR 0.029 (likely missing query/passage prefix protocol). Default kept; numbers in [`docs/embedding-models.md`](docs/embedding-models.md).

---

## Release History

For per-release detail (features, fixes, CVE patches, breaking changes), see:

- [`CHANGELOG.md`](CHANGELOG.md) — canonical changelog, every version since v1.0
- [GitHub Releases](https://github.com/jztan/pdf-mcp/releases) — release notes with installation instructions

---

**Last Updated:** 2026-05-27 (paragraph-aware excerpts shipped on develop; moved from P1 to done)
