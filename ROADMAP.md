# Roadmap

## Project Status

- **Current Version:** v1.13.1 (released 2026-05-21)
- **On Develop (unreleased):** starlette 1.0.0 → 1.1.0 bump for PYSEC-2026-161 — see `[Unreleased]` in [`CHANGELOG.md`](CHANGELOG.md)
- **MCP Registry Status:** Published
- **Test Suite:** 645 tests across unit, integration, and retrieval-quality benchmarks. OCR tests skip cleanly when system Tesseract is absent; benchmark tests are kept off the CI fast path.
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

- [ ] **Paragraph-aware excerpts in `pdf_search`.** The dominant agent
  workflow is `pdf_search` → `pdf_read_pages`, because the current
  ~120-char `context_chars` window is almost always too thin to ground
  an answer. Add an opt-in mode (e.g. `passage=True` or
  `excerpt_style="paragraph"`) that returns the full text block
  containing the hit, using PyMuPDF's `get_text("blocks")` to find
  paragraph boundaries. Cap per-excerpt at ~2000 chars and fall back
  to the windowed snippet for oversized blocks. Dedupe overlapping
  paragraphs when multiple hits land in the same block. Goal: cut the
  most common follow-up `pdf_read_pages` call out of the loop in the
  ~70% of cases where one paragraph is enough. Default preserves
  current behavior (snippet) so the change is additive. Inspired by
  comparing agent-side ergonomics against Anthropic's `pdf-viewer`
  plugin, where the equivalent `get_text` returns full-page text by
  design.
- [ ] **Calibrate the semantic confidence threshold.** The current `_SEMANTIC_CONFIDENCE_THRESHOLD = 0.5` is a guess; re-eval found gibberish queries scoring 0.54 against unrelated papers under `BAAI/bge-small-en-v1.5`. Needs an empirical pass over (corpus, gibberish-query, real-query) tuples to pick a defensible floor (likely 0.6–0.65, possibly per-model), documented in [`docs/embedding-models.md`](docs/embedding-models.md). Optional follow-up: per-corpus self-calibration mode.

### P2 — investigate before committing

- [ ] **Optional `pdf-mcp[layout]` extra (two-column reading order + heuristic detector escalation).** PyMuPDF's `sort=True` doesn't understand columns, so academic two-column layouts interleave paragraphs; the 7-signal section detector also underperforms on OCR'd scans and layout-irregular preprints. Both want the same dependency: a layout-aware model behind an optional extra. Spike on `pymupdf-pro` vs GROBID / Marker / Surya — install size, licensing, and accuracy lift — before budgeting delivery time.

### P3 — methodology, fold into a P1/P2 item

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

**Last Updated:** 2026-05-24 (added P1 paragraph-aware excerpts)
