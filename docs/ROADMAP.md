# Roadmap

Direction only. Per-item detail lives in `docs_internal/backlog.md`; shipped
detail lives in [`CHANGELOG.md`](../CHANGELOG.md).

## Project Status

- **Current version:** v3.1.0 (released 2026-09-05). Next cut: unscheduled; candidates are under P1 below.
- **MCP Registry:** published (v3.0.0)
- **Tools:** 13 released (`pdf_info`, `pdf_read_pages`, `pdf_read_all`, `pdf_search`, `pdf_get_toc`, `pdf_render_pages`, `pdf_extract_chart`, `pdf_corpus_warm`, `pdf_corpus_overview`, `pdf_corpus_search`, `pdf_cache_stats`, `pdf_cache_clear`, `server_info`)
- **Transports:** STDIO (`pdf-mcp`) and single-tenant HTTP (`pdf-mcp-http`); multi-arch Docker images at `ghcr.io/jztan/pdf-mcp`, tagged per release
- **Tests:** 1761, on Linux (Python 3.10 to 3.14) and Windows (3.10, 3.13). The release gate runs `pytest -m "not slow"`.

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

_Nothing queued._

### P1: high-value, well-scoped

- [ ] **Raise `CORPUS_MAX_FILES` to 500**, measured with the document arm (500 docs: described doc-hit@3 0.68, needle 1.000, trap 0.985, 3 s/query); 1,000 waits on the 900-distractor rung
- [ ] **`pdf_corpus_warm`: commit embeddings in page batches**, so a giant document can finish inside a client timeout
- [ ] **Teach keyword-mode query shape in the tool descriptions**, since AND-joined terms silently return nothing
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

**Last Updated:** 2026-08-23 (cut back to an index; per-item detail moved to `docs_internal/backlog.md`)
