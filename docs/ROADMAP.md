# Roadmap

Direction only. Per-item detail lives in `docs_internal/backlog.md`; shipped
detail lives in [`CHANGELOG.md`](../CHANGELOG.md).

## Project Status

- **Current version:** v3.7.0 (released 2026-11-07): corpus tools take up to 500 PDFs per call, embeddings keep going between corpus calls ([#89](https://github.com/jztan/pdf-mcp/issues/89)), `pdf_corpus_warm` lists only the documents a call changed ([#86](https://github.com/jztan/pdf-mcp/issues/86)), `pdf_read_pages` gains `force_ocr` and `suspect_cells` ([#76](https://github.com/jztan/pdf-mcp/issues/76), [#79](https://github.com/jztan/pdf-mcp/issues/79)), a cancelled tool call no longer crashes the server and corpus calls return at `budget_seconds` ([#82](https://github.com/jztan/pdf-mcp/issues/82)), and Python 3.11 or later is required. Unreleased on develop: the cache keeps an unchanged PDF while it exists, with a size cap ([#88](https://github.com/jztan/pdf-mcp/issues/88)); see [`CHANGELOG.md`](../CHANGELOG.md).
- **MCP Registry:** published (v3.6.0)
- **Tools:** 13 released (`pdf_info`, `pdf_read_pages`, `pdf_read_all`, `pdf_search`, `pdf_get_toc`, `pdf_render_pages`, `pdf_extract_chart`, `pdf_corpus_warm`, `pdf_corpus_overview`, `pdf_corpus_search`, `pdf_cache_stats`, `pdf_cache_clear`, `server_info`)
- **Transports:** STDIO (`pdf-mcp`) and single-tenant HTTP (`pdf-mcp-http`); multi-arch Docker images at `ghcr.io/jztan/pdf-mcp`, tagged per release
- **Tests:** 2872, on Linux (Python 3.11 to 3.14) and Windows (3.11, 3.13). The release gate runs `pytest -m "not slow"`.

---

## Tracking MCP 2026-07-28

The spec [shipped GA](https://blog.modelcontextprotocol.io/posts/2026-07-28/)
on 2026-07-28. pdf-mcp serves it alongside the older versions through FastMCP 4
(built on `mcp` SDK 2). The rest lands across **v3.x** rather than reserving a
major of its own.

- [x] **Stateless transport** (`initialize` and `Mcp-Session-Id` removed, per-request `_meta` replaces them)
- [x] **Error-code update** (missing-resource becomes JSON-RPC `-32602`)
- [ ] **Cacheable read-side responses** (`ttlMs` / `cacheScope` on `pdf_info`, `pdf_get_toc`, `pdf_read_pages`)
- [ ] **JSON Schema 2020-12** for `pdf_search`'s mode × granularity constraints
- [ ] **Tasks Extension**, **MCP Apps**, **file transfer** ([SEP-2631](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2631)): later v3.x, gated on host adoption

Roots, Sampling and protocol-level Logging are deprecated but unused here. The
OAuth/OIDC SEPs do not apply: stdio has no auth surface and `pdf-mcp-http` is
single-tenant by contract.

---

## Under Consideration

Ordered by leverage ÷ effort. Evidence, prior attempts and gates for each are in
`docs_internal/backlog.md`; read it before proposing work on any of these.

### P0: ship next

- [x] **Check the unconfirmed agent-tryout reports**: closed 2026-10-01. The silent OR fallback and `hidden_text` false positives were real and fixed in v3.5.0; the missing language field and image-cover ranking are listed under P2. `page_match_counts` and the different `warm_complete` from overview and search behave as designed
- [x] **Better document titles**: on develop for v3.7.0; documents with empty or junk metadata now use the largest text on page 1 before the filename

### P1: high-value, well-scoped

- [x] **Within-document page ranking on deep-page paraphrase queries**: closed as measured on 2026-09-17. Sharper embedding windows lift the returned page (hybrid paraphrase page NDCG@10 +0.09, CI excluding zero) but not, on their own, what an agent can answer from the response (answerable-in-full unchanged on paraphrase). The best configuration, sharper windows with a query-term-first excerpt picker, raised paraphrase answerable-in-full by 6 points, inside the noise floor, while exact_match, multi_doc and lexical_distractor each moved down by about one query in every harness; that mixed trade was not shipped. LLM-written page context did not reach the twelve queries no arm ranks in the top ten. The corpus-search latency work found on the way shipped on its own: hybrid search on 100 documents went from about 1.0 to 0.3 s per query
- [x] **Portable Tesseract for zero-install OCR**: shipped in v3.3.0 with the one-click bundle (English only; other OCR languages still need a Tesseract install)
- [x] **Raise `CORPUS_MAX_FILES` to 500**, measured with the document arm (500 docs: paraphrase doc-hit@3 0.68, exact_match 1.000, lexical_distractor 0.985; hybrid 0.87 s/query re-measured 2026-10-27 on the 184-query set; a warm of all 500 with embeddings took about 27 minutes). Shipped in v3.6.0; 1,000 waits on the 900-distractor rung
- [x] **Teach mode choice in the tool descriptions** (when to use keyword vs semantic): closed 2026-11-10 as measured, no description change needed; AND-matching and the OR retry on zero hits are already described
- [x] **Calibrate the semantic confidence threshold**: on develop; 0.65 for bge-small, measured on arXiv and 10-K corpora, single-document and corpus search (#101)

### P2: investigate before committing

- [ ] **Markdown output mode**, now needing a native generator rather than a dependency
- [ ] **Rank quality on repetitive corpora**: bibliography pages and near-duplicate filings crowd the page list
- [ ] **Layout-aware section-detector escalation**, for OCR'd scans and irregular preprints
- [x] **OCR on corpus warm**: `pdf_corpus_warm` OCRs image-only pages between calls by default, so a folder of scans becomes searchable (#103)
- [ ] **Language on overview cards**, so an agent can query a document in its own language
- [ ] **Document ranking for image-cover PDFs**: a document with no text on page 1 gets no document-level score
- [ ] **Raise `CORPUS_MAX_FILES` to 1,000**, once the 900-distractor rung is measured

### P3: methodology

- [ ] **Answer-level agent evaluation on long reports**, born-digital and scanned, tracked release over release
- [ ] **Embedding-distance coherence scorer**, a cheap CI gate for reading order
- [ ] **Agent-task evaluation** for section vs page search

---

## Investigated / Rejected

Paths prototyped or benchmarked and then closed are logged in
[`investigated-rejected.md`](investigated-rejected.md) with the evidence behind
each verdict. Read it before re-proposing retrieval, extraction, or embedding
work.

---

**Last Updated:** 2026-11-01 (status refresh after v3.6.0; unreleased develop work listed)
