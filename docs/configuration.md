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

# HTTP transport only (pdf-mcp-http); ignored by the stdio entry point.
PDF_MCP_AUTH_TOKEN=<secret>       # required, no default
PDF_MCP_HTTP_HOST=127.0.0.1       # bind address
PDF_MCP_HTTP_PORT=8000            # bind port (under the shipped compose
                                  # file this is pinned to 8000 and a value
                                  # in .env has no effect; change
                                  # PDF_MCP_HOST_PORT to move the host port)
PDF_MCP_HTTP_PATH=/mcp            # endpoint path
PDF_MCP_ALLOW_ANY_PATH=1          # start with no [paths] allow list
```

The `PDF_MCP_HTTP_*` and `PDF_MCP_ALLOW_ANY_PATH` variables affect
`pdf-mcp-http` only; the stdio entry point ignores them.

For what the auth token protects, the trust boundary it creates, and how to
rotate it, see [remote-access.md](remote-access.md).

### Docker deployment notes

The Compose stack maps `PDF_MCP_HOST_PORT` (default `8802`) on the host to a
fixed container port `8000`, so multiple pdf-mcp containers can each publish a
different host port without colliding. `deploy/bootstrap.sh` generates the
auth token with `openssl rand -hex 32` and writes it to `.env` (Compose loads
this file automatically, both for `${VAR}` interpolation in
`docker-compose.yml` and via `env_file:` into the container), and
creates the `./documents` directory that the container mounts. The container
runs as a non-root user with `documents/` and the cache volume owned by that
user.

`./documents` is where documents come in. It is mounted read-only at
`/data/pdfs`, and `deploy/config.docker.toml` allow-lists that one directory,
so the contents of the folder are exactly what the server can open. The mount
is read-only because the container never writes there: adding a document is
always something you do on the host, with `cp`, `rsync`, a sync client, or a
scheduled job. An agent connected over HTTP cannot put a file here. It reads
what is already present, or an `https://` URL the server fetches for it. See
[Getting documents to the server](remote-access.md#getting-documents-to-the-server).

`docker compose` pulls `ghcr.io/jztan/pdf-mcp`, published for both
amd64 and arm64, so `./deploy.sh` needs no local build; `./deploy.sh --build`
builds locally instead, on whichever architecture you run it on. The built
image is around 900 MB, since it bakes in tesseract, the column-aware
extraction extra, and the embedding model so there is no cold-start download
on first use.

`PDF_MCP_IMAGE_TAG` (default `latest`) selects which published tag `docker
compose` pulls. Set it to an exact version, for example `2.0.0`, to keep a
deployment on a known release; `latest` moves with every release. Published
tags are the full version, the major.minor line, the major line, and
`latest`. Only `docker-compose.yml` reads this variable, and the server
itself ignores it; building locally with `./deploy.sh --build` ignores it
entirely.

Two startup guards apply to `pdf-mcp-http` (including in the Docker image):
the process exits without `PDF_MCP_AUTH_TOKEN` set, and it exits without a
non-empty `[paths]` allow list, since an absent allow list otherwise makes
`PDFConfig` permissive, which is correct for a local stdio install but unsafe
for a remote endpoint. `deploy/config.docker.toml` supplies that allow list
in the Docker image and is mounted read-only; set `PDF_MCP_ALLOW_ANY_PATH=1`
to override deliberately. That override widens which existing paths the server
may read; it does not create a way for a caller to deliver a new file.

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
