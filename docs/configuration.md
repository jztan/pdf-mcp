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

# Embedding device. 1 = use the GPU and warn if it is not available;
# 0 = always CPU; unset = fastembed auto-detects (CPU on a plain install).
# Needs onnxruntime-gpu and a CUDA runtime; see "GPU embedding (NVIDIA
# CUDA)" below.
PDF_MCP_CUDA=1

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

For what the auth token protects and the trust boundary it creates, see
[remote-access.md](remote-access.md). To set the transport up and rotate the
token, see [HTTP transport setup](#http-transport-setup) below.

### GPU embedding (NVIDIA CUDA)

Optional and off by default. On an NVIDIA card the embedding pass is one to
two orders of magnitude faster, on Linux and Windows x86_64. Vectors are the
same as the CPU path produces (cosine 0.999998 measured), so the cache is
shared and the choice is speed only.

For a Turing or newer GPU (GTX 16, RTX 20+) on driver r580+:

```bash
pip uninstall -y onnxruntime
pip install fastembed-gpu
pip install nvidia-cublas nvidia-cuda-runtime nvidia-cufft nvidia-curand \
            nvidia-cudnn-cu13
```

Maxwell, Pascal and Volta cards (GTX 900, GTX 10-series, Titan V) predate
CUDA 13 and take the CUDA 12 build:

```bash
pip uninstall -y onnxruntime
pip install fastembed-gpu
pip install --force-reinstall --no-deps onnxruntime-gpu --index-url \
  https://aiinfra.pkgs.visualstudio.com/PublicPackages/_packaging/onnxruntime-cuda-12/pypi/simple/
pip install nvidia-cublas-cu12 nvidia-cuda-runtime-cu12 nvidia-cufft-cu12 \
            nvidia-curand-cu12 nvidia-cudnn-cu12
```

Then, either way:

```bash
export PDF_MCP_CUDA=1   # Linux, macOS
set PDF_MCP_CUDA=1      # Windows
```

The uninstall is needed because `onnxruntime` and `onnxruntime-gpu` install
into the same directory, so with both present the CPU build wins the import.

What the variable does:

| `PDF_MCP_CUDA` | behaviour |
|---|---|
| `1` | GPU. If the CUDA provider cannot load, the server warns with the provider it actually got and falls back to CPU instead of running slower in silence. |
| `0` | CPU, always. |
| unset | fastembed decides. On a plain install that is the CPU, exactly as before. On a machine where onnxruntime-gpu is installed and a CUDA runtime is already on the library path (a system-wide CUDA toolkit, for example) it auto-detects the GPU even though nothing asked for it. Set `0` if that is not what you want. |

Two things to expect on the GPU path, both measured on an A10G
(`benchmark_data/cuda_embedding/RESULTS.md`): the first CUDA session in a
process takes 12 to 15 s to load the model (later ones about 1 s, CPU about
5 s), so a session that embeds only a few pages gains nothing; and
onnxruntime 1.29 prints two warnings on every CUDA session, "No registered
plugin EP device found for 'CUDAExecutionProvider'" and "Some nodes were
not assigned to the preferred execution providers", which are benign when
the pass runs at GPU speed. A runtime that is present but broken (cuDNN
missing, wrong series) can pass the provider check and fail on the first
encode instead; the error names the kernel, and reinstalling the runtime
wheels for your CUDA series is the fix.

Apple Silicon is not covered: the CoreML provider in the standard wheel
gives no speedup on the shipped model. What the CPU path does instead is
keep its batches small: texts are sorted by length and embedded 16 at a
time, so onnxruntime pads each sub-batch to a near neighbour rather than to
the longest page in a 24-page warm group. On an M4 Pro that is 1.37x
faster than one large padded batch and holds about 0.7 GB of transient
encode memory instead of 2.8 GB, with identical vectors. Thread pinning
was measured on the same machine and does not help: one intra-op thread
ran as fast as fourteen, so the encode is memory-bound, not compute-bound.

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

`./deploy.sh --help` lists the lifecycle commands (start, stop, logs,
health-check, and `--build`).

`docker compose` pulls `ghcr.io/jztan/pdf-mcp`, published for both
amd64 and arm64, so `./deploy.sh` needs no local build; `./deploy.sh --build`
builds locally instead, on whichever architecture you run it on. The built
image is around 900 MB, since it bakes in tesseract, the column-aware
extraction extra, and the embedding model so there is no cold-start download
on first use.

`PDF_MCP_IMAGE_TAG` (default `latest`) selects which published tag `docker
compose` pulls. Set it to an exact version, for example `2.1.0`, to keep a
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

## HTTP transport setup

Applies to `pdf-mcp-http` only. For what the token protects and what a leak
costs you, see [remote-access.md](remote-access.md).

`deploy/bootstrap.sh` generates the token with `openssl rand -hex 32`, writes it
into `.env`, and sets that file to mode 600. That `chmod` runs only when the
script creates the file, so after hand-editing `.env` to rotate the token,
re-check the mode: some editors rewrite a file with fresh default permissions.
The Compose stack publishes to loopback only, so nothing is reachable off the
box until you put a TLS proxy in front of it. TLS belongs to that proxy, not to
the app; `deploy/Caddyfile.example` is a starting point.

Decide on `[urls]` while you are writing the config. It is the second half of
what the token grants, and unlike `[paths]` no startup guard asks you about it:
left empty it permits fetching from any public HTTPS host.
`deploy/config.toml.example` ships a commented `[urls]` block next to the
`[paths]` one.

Decide on `/health` too. It is the one route outside auth, returning `status`
and `version` so an uptime probe needs no credential.
`deploy/Caddyfile.example` proxies the whole host to the backend, so following
it publishes that route, and with it the exact running version, to anyone who
asks. That is the precondition for matching a published CVE against your
deployment. [SECURITY.md](../SECURITY.md) accepts version disclosure on
`/health` as in bounds, so this is a deliberate default rather than an
oversight, but it is being chosen for you. To keep it internal, block the path
at the proxy and probe over loopback instead:

```caddyfile
@health path /health
respond @health 404
```

### Client configuration

A client points at the endpoint URL and sends the token as a bearer header on
every request. The block below is the Claude-family client config format
(Claude Desktop, Claude Code); other MCP clients spell the same two things, the
URL and the header, differently:

```json
{
  "mcpServers": {
    "pdf-mcp": {
      "type": "http",
      "url": "https://pdf.example.com/mcp",
      "headers": {
        "Authorization": "Bearer <token>"
      }
    }
  }
}
```

The token then lives in that client's config file in plaintext and inherits
exactly that file's protection, no more. Treat it as you would an SSH private
key.

### Rotating the token

1. Generate a replacement token: `openssl rand -hex 32`.
2. Rewrite `PDF_MCP_AUTH_TOKEN` in `.env`.
3. Restart the stack: `docker compose up -d --force-recreate`.
4. Update every client that held the old token.
5. Run both checks below, and read them together.

`--force-recreate` is deliberate. A plain `up -d` usually recreates the
container when a value in `.env` changes, but that depends on how your Compose
version hashes the service config, and the failure is silent: the container
keeps running with the old token loaded while you believe it is dead.

```bash
# 1. did the service come back? (credential-free probe)
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8802/health

# 2. is the OLD token rejected?
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer <OLD-token>" \
  http://127.0.0.1:8802/mcp
```

| what you see | what it means |
|---|---|
| `200` then `401` | Done. |
| `200` then anything else, `200` included | The old credential still validates, so the restart did not pick up the new value. Go back to step 3. |
| `000` from either check | Connection refused, so nothing is listening and the service did not come back at all. Repeating step 3 will not help. Run `docker compose logs pdf-mcp` and read the last lines. The usual cause is an `.env` edit that left `PDF_MCP_AUTH_TOKEN` empty, which trips the fail-closed startup guard and exits before the port is bound. |

Substitute your own port if you changed `PDF_MCP_HOST_PORT` from its 8802
default.

## Offline prewarm (`pdf-mcp-warm`)

`pdf_corpus_warm` (the MCP tool) is built to fit inside one client-side
tool call: it caps at 100 files and `budget_seconds <= 300` per call (see
[tool-reference.md](tool-reference.md#pdf_corpus_warm)), so a folder that
does not fit either limit means re-issuing the same call by hand,
possibly many times, from inside a chat session — and a query against a
still-partly-warm corpus just times out in the meantime.

`pdf-mcp-warm` is a separate command installed alongside `pdf-mcp` (same
`pip install`/`uvx` package, a second entry point) that runs outside any
MCP client, so neither limit applies. It walks a whole folder and warms
it to completion in one run, writing to the exact same on-disk cache
(`PDF_MCP_CACHE_DIR`, this file's `[paths]` allow-list, `[embedding].model`)
the server reads — run it once before a chat session so `pdf_search` /
`pdf_corpus_search` hit a warm cache instead of timing out.

```bash
pdf-mcp-warm /path/to/reports/ --recursive
pdf-mcp-warm doc1.pdf doc2.pdf --no-embeddings   # text only, skip vectors
pdf-mcp-warm /path/to/reports/ --model BAAI/bge-small-en-v1.5
```

Embeddings **and** the section-granularity search index are both warmed
by default (`--no-embeddings` / `--no-sections` opt out of each). Unlike
`pdf_corpus_warm`, which leaves `sections` off by default to stay
budget-conscious, this CLI has no budget to protect — and skipping either
just moves the cost to query time instead of removing it: a prewarm that
skips vectors leaves semantic search to time out later, and skipping
sections leaves a large document's first
`pdf_search(granularity="section")` call to build that index from
scratch, on its own, which can by itself exceed a timeout-bounded MCP
client's budget even on an otherwise fully-warmed corpus. Progress and a
final summary go to stderr; already-cached documents are free (see
`pdf_corpus_warm` above), so re-running after an interrupt (Ctrl-C, a
crash) resumes rather than redoing work. If your corpus is large enough
to need this, also raise `PDF_MCP_CACHE_TTL` (below) — the default
24-hour TTL will otherwise
start expiring a prewarm you're not actively querying.

## Caching

The server uses SQLite for persistent caching.

**Cache location:** `~/.cache/pdf-mcp/cache.db`

The cache runs in SQLite [WAL mode](https://www.sqlite.org/wal.html), so two
sidecar files sit alongside it: `cache.db-wal` and `cache.db-shm`. Back up or
mount all three together — copying `cache.db` alone can capture a database
missing its most recent writes. `pdf_cache_clear` and `pdf_cache_stats`
already account for them.

WAL makes cache writes faster than the rollback journal on every OS measured,
most on Windows: a first-time page write there costs about 11ms under WAL
against about 57ms under the rollback journal (which creates and deletes a
journal file on every commit), roughly 5x. On Linux and macOS the gap is
smaller, about 1.5x and 1.2x. A filesystem that does not support WAL (some
network mounts) is detected at startup and the cache falls back to the
rollback journal rather than failing. `server_info` reports which
mode is in effect under `storage.journal_mode`.

**SQLite version:** no minimum is enforced, but 3.9.0 or newer is recommended.
Below it, FTS5 is unavailable and keyword search degrades to unranked
substring matching; `server_info` reports this as
`storage.keyword_search_ranked: false`.

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
