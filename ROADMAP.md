# Roadmap

## Project Status

- **Current version:** v1.17.0 (released 2026-06-19)
- **MCP Registry:** Published
- **Test suite:** 857 tests across unit, integration, and retrieval-quality benchmarks. OCR tests skip cleanly when system Tesseract is absent. The `test_benchmark_*` files are fast unit tests for the benchmark scripts' helpers; the billed coherence eval (`test_coherence_no_regression_vs_baseline`, marked `slow`) is excluded from the release gate, which runs `pytest -m "not slow"`.
- **Tools:** `pdf_info`, `pdf_read_pages`, `pdf_read_all`, `pdf_search`, `pdf_get_toc`, `pdf_render_pages`, `pdf_cache_stats`, `pdf_cache_clear`, `server_info`

---

## Next Release

No release branch open. Queued on `develop` (metadata-only, not in 1.17.0): four `chore(packaging)` commits — py3.13 classifier, `Development Status` Beta→Production/Stable, refreshed package description, and author/maintainer email switched to a GitHub noreply address. They ride the next release. Cut it from develop via `python scripts/release.py patch|minor` per [`RELEASE_SOP.md`](RELEASE_SOP.md) once the `[Unreleased]` block is ready.

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

- [ ] **Calibrate the semantic confidence threshold.** The current `_SEMANTIC_CONFIDENCE_THRESHOLD = 0.5` is a guess; re-eval found gibberish queries scoring 0.54 against unrelated papers under `BAAI/bge-small-en-v1.5`. Needs an empirical pass over (corpus, gibberish-query, real-query) tuples to pick a defensible floor (likely 0.6–0.65, possibly per-model), documented in [`docs/embedding-models.md`](docs/embedding-models.md). Optional follow-up: per-corpus self-calibration mode.

### P2 — investigate before committing

- [ ] **Layout-aware section-detector escalation.** _Not started. Distinct from the shipped `pdf-mcp[multicolumn]` extra: that fixed column **reading order** (v1.15.0); this is about section **boundary** detection._ The 7-signal heuristic in `section_detector.py` underperforms on OCR'd scans and layout-irregular preprints. If revisited, spike a layout-aware model (GROBID / Marker / Surya) on accuracy lift, install size, and licensing before budgeting.

### P3 — methodology, fold into a P1/P2 item

- [ ] **Embedding-distance "coherence" scorer to guard the column-detection path in CI.** _Partially addressed. Two coherence tools now exist: the token-sequence reading-order benchmark (v1.15.0 — `scripts/benchmark_reading_order.py`: `reading_order_score`, `normalize_tokens`, `classify_columns` + `benchmark_data/reading_order_corpus.json`) and the LLM-judge coherence eval harness (v1.17.0 — `scripts/eval_coherence.py`, `test_coherence_no_regression_vs_baseline`, marked `slow`/billed). What's still missing is a **cheap, unbilled, CI-runnable embedding-distance scorer.**_ The containment-based excerpt benchmark is blind to reading-order scrambling — the answer substring survives column interleaving, so containment stayed flat through the two-column reading-order fix (shipped as `pdf-mcp[multicolumn]`). An embedding-distance metric — embed a paragraph-mode excerpt, embed the same text in canonical order, compare — should be ~0 on single-column, large pre-fix on two-column, ~0 post-fix. Reuse the existing corpus and scaffolding; **swap the token-sequence scorer for an embedding-distance scorer**. Unlike the billed LLM-judge harness, this could run on every CI push to catch regressions in `detect_column_boxes` / column extraction that containment cannot see.

- [ ] **Agent-task evaluation for section vs page search.** Current benchmarks measure retrieval characteristics; this would measure whether section-granularity actually helps agents *answer better questions* (LLM-graded Q&A or agent-task completion). Not a deliverable on its own — bundle the harness into whichever P1/P2 item needs it first (likely the confidence-threshold calibration).

---

## Investigated / Rejected

Items prototyped or benchmarked and then deliberately closed:

- **Hybrid (BM25 + semantic) section search** (2026-05-04) — Built full Phase-1 validation on `feature/hybrid-section-validation` (15 commits, 550 tests) plus a 45-query confirmation calibration. Hybrid RRF gave only ~5% lift over BM25 on scientific papers (below the 0.10 MRR gate) and caused a severe lexical regression at section grain (0.93 → 0.63 MRR, −33%) because RRF dilutes BM25's clean rank-1 on title-style queries. v1.8.0 page-grain hybrid is 3× better on paraphrase queries. No query class where hybrid-section wins. SOTA systems (PaperQA2) use semantic + LLM rerank instead.
- **Default embedding model benchmark** (2026-05-09) — Live MRR + latency benchmark of 4 fast English fastembed models on the 7-scenario arxiv ground truth via `scripts/benchmark_embedding_models.py`. Gate: MRR lift ≥ 0.05 AND p50 latency ≤ 1.5× baseline. `bge-small-en-v1.5` won by 0.116 MRR over the best challenger; no challenger met the lift threshold. arctic-embed-m collapsed to MRR 0.029 (likely missing query/passage prefix protocol). Default kept; numbers in [`docs/embedding-models.md`](docs/embedding-models.md).
- **Parallelize text extraction; threads for any page op** (2026-06-06) — The parallel page-processing benchmark (`scripts/benchmark_parallel_pages.py`, [`benchmark_data/parallel_pages_results.md`](benchmark_data/parallel_pages_results.md)) closed two paths. **Text extraction:** at ~4 ms/page it is too cheap to parallelize — the process pool loses (0.13–0.24x; spawn cost plus cross-process pickling of the extracted text dominate), reaching ~1.6x only on Linux/fork with many pages, never worth the spawn cost and the lost shared SQLite cache. **Threads anywhere:** GIL/native-lock bound (0.7–1.0x on render), and PyMuPDF OCR crashes under threads (Leptonica not thread-safe). Only OCR (and conditionally render) survived — promoted to P0.
- **MLX/Apple-Silicon embedding backend fork** (`pdf-mcp-mlx`) — Benchmarked an MLX-backed embedding path (with E5 query/passage prefixes) against the shipped `fastembed`-CPU + `bge-small-en-v1.5` default. The fork did not beat the CPU baseline; `bge-small` + `fastembed`-CPU remained optimal, so the fork was rejected. The one genuine deliverable surfaced — fastembed 0.8 returning unnormalized vectors for some models (e.g. `multilingual-e5-large`) — was the embedding L2-normalization fix, since **shipped in v1.17.0**.
- **Running-header/footer (boilerplate) stripping** (2026-06-11) — Prototyped a frequency-based detector (positional margin bands + digit-normalized signatures + odd/even parity + consecutive-run rule) and benchmarked detection and downstream impact across three scripts. **Detection works:** on a synthetic corpus with injected boilerplate the full method hit F1 1.00 across all edge cases, and on real PDFs (`scripts/benchmark_boilerplate.py --real`: Attention, GPT-3, GDPR) it reached recall 1/1 with zero precision suspects — collapsing GDPR's varying `L 119/N Official Journal …` header to one signature and removing it from 100% of 88 pages, vs a RAG-on-PDF-style naive filter that would strip `Abstract`/`CHAPTER I`/section headings. **But the payoff doesn't justify it:** token savings were only 0.2–1.6%, and the search-impact benchmark (`scripts/benchmark_search_impact.py`) showed BM25's IDF already neutralizes per-page boilerplate — realistic queries were unchanged (MRR 0.571 → 0.571, 0/7 top-10 changes; GDPR control jaccard 1.00). The only movement was on contrived queries whose terms overlap a word-bearing running header (GDPR collision jaccard ~0.41). Real but narrow value (legal/standards/journal docs only); shelved as a possible future opt-in, index-side only.

---

## Release History

For per-release detail (features, fixes, CVE patches, breaking changes), see:

- [`CHANGELOG.md`](CHANGELOG.md) — canonical changelog, every version since v1.0
- [GitHub Releases](https://github.com/jztan/pdf-mcp/releases) — release notes with installation instructions

---

**Last Updated:** 2026-06-20 (post-v1.17.0 — vertical-script CJK and the embedding L2-norm fix shipped; removed the Pending-release section; refreshed status/test count; reworded the P3 coherence item around the now-shipped LLM-judge harness; logged the rejected MLX fork)
