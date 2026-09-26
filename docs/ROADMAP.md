# Roadmap

Direction only. Per-item detail lives in `docs_internal/backlog.md`; shipped
detail lives in [`CHANGELOG.md`](../CHANGELOG.md).

## Project Status

- **Current version:** v3.4.0 (released 2026-09-26): search hits carry their outline section and table lead-in ([#66](https://github.com/jztan/pdf-mcp/issues/66)), parallel tool calls no longer crash the server ([#61](https://github.com/jztan/pdf-mcp/issues/61)), merged-cell table values land in their printed column ([#63](https://github.com/jztan/pdf-mcp/issues/63)), and the over-cap corpus error lists the folder's PDFs ([#68](https://github.com/jztan/pdf-mcp/issues/68)). Nothing user-facing on develop is unreleased.
- **MCP Registry:** published (v3.4.0)
- **Tools:** 13 released (`pdf_info`, `pdf_read_pages`, `pdf_read_all`, `pdf_search`, `pdf_get_toc`, `pdf_render_pages`, `pdf_extract_chart`, `pdf_corpus_warm`, `pdf_corpus_overview`, `pdf_corpus_search`, `pdf_cache_stats`, `pdf_cache_clear`, `server_info`)
- **Transports:** STDIO (`pdf-mcp`) and single-tenant HTTP (`pdf-mcp-http`); multi-arch Docker images at `ghcr.io/jztan/pdf-mcp`, tagged per release
- **Tests:** 2529, on Linux (Python 3.10 to 3.14) and Windows (3.10, 3.13). The release gate runs `pytest -m "not slow"`.

---

## Tracking MCP 2026-07-28

The spec [shipped GA](https://blog.modelcontextprotocol.io/posts/2026-07-28/)
on 2026-07-28. All of it is gated on fastmcp shipping support, and it lands
across **v3.x** rather than reserving a major of its own.

- [ ] **Stateless transport** (`initialize` and `Mcp-Session-Id` removed, per-request `_meta` replaces them)
- [ ] **Error-code update** (missing-resource becomes JSON-RPC `-32602`)
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

- [ ] **Check the unconfirmed agent-tryout reports**: possible wrong signals such as `page_match_counts` counting terms rather than pages, and `warm_complete` disagreeing between the corpus overview and corpus search. Each one that reproduces gets an issue and a fix
- [ ] **Better document titles**: documents with empty metadata fall back to the filename, and some junk titles slip through; fall back to the largest text on page 1 first

### P1: high-value, well-scoped

- [x] **Within-document page ranking on deep-page paraphrase queries**: closed as measured on 2026-09-17. Sharper embedding windows lift the returned page (hybrid described page NDCG@10 +0.09, CI excluding zero) but not, on their own, what an agent can answer from the response (answerable-in-full unchanged on described). The best configuration, sharper windows with a query-term-first excerpt picker, raised described answerable-in-full by 6 points, inside the noise floor, while needle, spread and trap each moved down by about one query in every harness; that mixed trade was not shipped. LLM-written page context did not reach the twelve queries no arm ranks in the top ten. The corpus-search latency work found on the way shipped on its own: hybrid search on 100 documents went from about 1.0 to 0.3 s per query
- [x] **Portable Tesseract for zero-install OCR**: shipped in v3.3.0 with the one-click bundle (English only; other OCR languages still need a Tesseract install)
- [ ] **Raise `CORPUS_MAX_FILES` to 500**, measured with the document arm (500 docs: described doc-hit@3 0.68, needle 1.000, trap 0.985; about 3 s/query before the 2026-09-17 latency work, not yet re-measured, and the remaining cost at that size is per-document keyword work); 1,000 waits on the 900-distractor rung. Since v3.4.0 the over-cap error lists the folder's PDFs so an agent can split it. Open precondition: the cap error hint, tool descriptions and tool reference must say that a corpus of hundreds of files warms across several re-issued calls
- [ ] **Teach mode choice in the tool descriptions** (when to use keyword vs semantic); AND-matching and the OR retry on zero hits are already described
- [ ] **Calibrate the semantic confidence threshold**; 0.5 is a guess and gibberish scores 0.54

### P2: investigate before committing

- [ ] **Markdown output mode**, now needing a native generator rather than a dependency
- [ ] **Rank quality on repetitive corpora**: bibliography pages and near-duplicate filings crowd the page list
- [ ] **Layout-aware section-detector escalation**, for OCR'd scans and irregular preprints

### P3: methodology

- [ ] **Embedding-distance coherence scorer**, a cheap CI gate for reading order
- [ ] **Agent-task evaluation** for section vs page search

---

## Investigated / Rejected

Paths prototyped or benchmarked and then closed are logged in
[`investigated-rejected.md`](investigated-rejected.md) with the evidence behind
each verdict. Read it before re-proposing retrieval, extraction, or embedding
work.

---

**Last Updated:** 2026-09-26 (status refresh after v3.4.0; tryout verification and document titles queued as P0; portable Tesseract marked shipped)
