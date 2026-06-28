# Configuration

pdf-mcp runs with sensible defaults and needs no configuration to work. The settings below let you restrict what the server can access, tune deployment knobs, and understand the cache.

## Access control (optional)

Create `~/.config/pdf-mcp/config.toml` to restrict which local paths and URL hosts the server will access. The file is optional — if absent, the server is permissive within the built-in SSRF floor (HTTPS-only, blocked private IP ranges).

A complete `config.toml` — every section is optional; include only what you
need:

```toml
[paths]
allow = ["~/Documents/**", "/data/pdfs/**"]
deny  = ["~/.ssh/**", "~/.aws/**"]

[urls]
allow = ["*.internal.example.com"]
deny  = ["untrusted.example.com"]

[limits]
max_response_bytes = 200000

[embedding]
model = "BAAI/bge-small-en-v1.5"

[content_trust]
injection_phrases = ["忽略以上所有指示", "以前の指示を無視してください", "ignorez les instructions"]
```

**`[paths]` / `[urls]`** — shell-glob allow/deny rules (`*` matches across path
separators); `deny` wins when both match. Path matching operates on the resolved
path after symlink expansion. A malformed config file prevents the server from
starting — it never silently falls back to permissive.

**`[limits]`** — caps text-payload byte size on `pdf_read_all` and
section-granularity `pdf_search`; see [docs/response-limits.md](response-limits.md).

**`[embedding]`** — the semantic-search model; the default shown above is
`BAAI/bge-small-en-v1.5`. See [docs/embedding-models.md](embedding-models.md).

**`[content_trust]`** — extends the hidden-text `injection_in_hidden` severity
hint with your own (including non-English) phrases. They **extend** the built-in
English phrases (never replace them); each is matched case-insensitively,
space-insensitively, inside already-hidden text only — a severity hint, never a
trigger. A non-list value aborts startup. Phrases are matched independently, so
one that is a substring of another (or of a built-in) can each contribute to the
count — the result is a hint, not an exact tally.

## Environment variables

```bash
# Cache directory (default: ~/.cache/pdf-mcp)
PDF_MCP_CACHE_DIR=/path/to/cache

# Cache TTL in hours (default: 24)
PDF_MCP_CACHE_TTL=48

# Max worker processes for parallel OCR / rendering in pdf_read_pages
# (default: auto = min(cpu_count, pages, 8)). Set to 1 to force sequential.
PDF_MCP_MAX_WORKERS=8
```

## Caching

The server uses SQLite for persistent caching.

**Cache location:** `~/.cache/pdf-mcp/cache.db`

**What's cached:**

| Data | Benefit |
|------|---------|
| Metadata + text coverage | Avoid re-parsing document info |
| Page text | Skip re-extraction |
| Images | Skip re-encoding |
| Tables | Skip re-detection |
| TOC | Skip re-parsing |
| FTS5 index | O(log N) search with BM25 ranking after first query |
| Embeddings | Instant semantic search after first indexing run |
| Rendered PNGs | Skip re-rendering; shared between `pdf_render_pages` and `pdf_read_pages(render_dpi=…)` |

**Cache invalidation:**
- Automatic when file modification time changes
- Manual via the `pdf_cache_clear` tool
- TTL: 24 hours (configurable)
