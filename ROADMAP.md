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

## Under Consideration

- [ ] **Calibrate the semantic confidence threshold.** The current `_SEMANTIC_CONFIDENCE_THRESHOLD = 0.5` is a guess; re-eval found gibberish queries scoring 0.54 against unrelated papers under `BAAI/bge-small-en-v1.5`. Needs an empirical pass over (corpus, gibberish-query, real-query) tuples to pick a defensible floor (likely 0.6–0.65, possibly per-model), documented in [`docs/embedding-models.md`](docs/embedding-models.md). Optional follow-up: per-corpus self-calibration mode.
- [ ] **Unify `_resolve_path` errors with the inline-error contract.** Path/URL validation still raises `ToolError` while page-spec errors return inline `{"error", "hint"}`. Agents need two recovery paths. Migrate `_resolve_path` callers to the inline contract; ship with shape tests across every affected tool.
- [ ] **Two-column / complex layout reading order.** PyMuPDF's `sort=True` doesn't understand columns, so academic two-column layouts interleave paragraphs. Investigate `pymupdf-pro` or GROBID / Marker / Surya as an optional `pdf-mcp[layout]` extra. Overlaps with the heuristic-detector escalation item below.
- [ ] **`pdf_render_pages` page labels.** `ImageContent` blocks currently have no page annotation; if `render_failed_pages` fires, surviving images may be misaligned. Use FastMCP `Image.annotations` to embed `{"page": N}` so agents can correlate images to pages regardless of failures.
- [ ] **Heuristic detector escalation for low-quality PDFs.** For OCR'd scans and layout-irregular preprints where the 7-signal detector underperforms, explore CRF / transformer layout models (GROBID, Marker, Surya) as an optional `pdf-mcp[layout]` extra.
- [ ] **Agent-task evaluation for section vs page search.** Current benchmarks measure retrieval characteristics. Add a downstream eval (LLM-graded Q&A or agent-task completion) to measure whether section-granularity actually helps agents answer better questions.

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

**Last Updated:** 2026-05-24
