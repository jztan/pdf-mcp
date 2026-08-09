# pdf-mcp

[![PyPI version](https://img.shields.io/pypi/v/pdf-mcp)](https://pypi.org/project/pdf-mcp/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![GitHub Issues](https://img.shields.io/github/issues/jztan/pdf-mcp)](https://github.com/jztan/pdf-mcp/issues)
[![CI](https://github.com/jztan/pdf-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/jztan/pdf-mcp/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/jztan/pdf-mcp/graph/badge.svg)](https://codecov.io/gh/jztan/pdf-mcp)
[![Downloads](https://pepy.tech/badge/pdf-mcp)](https://pepy.tech/project/pdf-mcp)

**Surgical PDF access for AI agents: search, read, and extract without flooding context.**

An [MCP](https://modelcontextprotocol.io/) server that lets Claude Code and other AI agents search a PDF by meaning or keyword, read only the pages that matter, and cleanly pull out tables, images, and scanned text, even from multi-column and Japanese layouts.

**mcp-name: io.github.jztan/pdf-mcp**

## Try it in your browser

**[See what your AI agent sees →](https://pdf-mcp.jztan.com/)**

Drop in any PDF, or a whole folder of them, and watch an agent triage the corpus, search across every document at once, and read only the pages that matter, using a fraction of the tokens. 100% client-side, no install required.

<p align="center">
  <a href="https://pdf-mcp.jztan.com/"><img src="https://raw.githubusercontent.com/jztan/pdf-mcp/develop/docs/images/demo.gif" alt="pdf-mcp browser demo: an AI agent warms a 6-PDF corpus, triages it, searches across all six documents, and reads only the matching page, with 97.5% of the corpus never entering the context window" width="760"></a>
</p>

## Why pdf-mcp?

| | Without pdf-mcp | With pdf-mcp |
|---|---|---|
| Large PDFs | Context overflow | Chunked reading |
| Token budgeting | Guess and overflow | Estimated tokens before reading |
| Finding content | Load everything | Hybrid search (BM25 keyword + semantic) |
| Tables | Lost in raw text | Extracted and inlined per page |
| Charts | Trapped in the plot image | Extracted as `(x, y)` data tables |
| Multi-column PDFs | Columns interleaved in extracted text | Column-aware reading order (`pdf-mcp[multicolumn]`) |
| Vertical scripts (Japanese) | Columns scrambled / glyph soup | Geometric reorder of vertical text (tategaki / 縦書き); CJK keyword search works on unspaced Japanese/Chinese/Korean text via a char-split FTS index |
| Images | Ignored | Extracted as PNG files |
| Repeated access | Re-parse every time | SQLite cache |
| Scanned PDFs | No text extracted | OCR via Tesseract, parallelized across pages (`pdf_read_pages(ocr=True)`) |
| Visual content | Must describe in words | Render page as image (`pdf_render_pages`) |
| Hidden / injected text | Silently ingested as if a human vetted it | Flagged as untrusted: hidden-text detection (`content_trust=True`) |
| Folders of PDFs | One document at a time | Corpus tools: warm, triage, and search across a whole folder |
| Tool design | Single monolithic tool | 13 specialized tools |

## Features

- **Hybrid search**: find relevant pages with a question, not a page range. Combines BM25 keyword and semantic search via Reciprocal Rank Fusion
- **Corpus search**: point the server at a folder of PDFs: warm them into the cache, get per-document triage cards, and search across all documents at once with ranked, document-attributed hits
- **Paginated reading**: fetch only the pages your agent needs; large documents don't blow your context window
- **OCR**: scanned and image-based PDFs are fully readable and searchable via Tesseract, parallelized across pages for ~2–3x faster extraction on typical scans
- **Structured extraction**: tables, embedded images, and table of contents returned as structured data, not text soup
- **Chart data extraction**: pull exact `(x, y)` tables from vector charts, read from the plot geometry rather than guessed from the image; declines with a rendered image when a chart can't be read reliably
- **Vertical-script reading order**: Japanese tategaki (縦書き) reconstructed from glyph geometry into correct top-to-bottom, right-to-left order; article segmentation for dense magazine layouts; mojibake filtered
- **Persistent cache**: SQLite-backed; re-reads are instant and survive server restarts
- **Secure URL fetching**: HTTPS-only with SSRF protection; local network ranges are blocked
- **Content-trust / hidden-text detection**: flags text a human reader can't see (invisible render mode, sub-point fonts, transparent or white-on-white fill, off-page) so an agent treats it as untrusted rather than vetted. Flag-only: nothing is stripped

## Contents

- [Installation](#installation)
- [Quick Start](#quick-start)
- [Tools](#tools)
- [Example Workflow](#example-workflow)
- [Remote / HTTP transport](#remote--http-transport)
- [Configuration](#configuration)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [Contributors](#contributors)
- [Security](#security)
- [License](#license)

## Installation

```bash
pip install pdf-mcp
```

Semantic search is included by default (hybrid `auto` search is built on it;
~67 MB embedding model download on first use). The former `[semantic]` and
`[cjk]` extras remain as no-op aliases. Platform note: the bundled
`onnxruntime` has no wheels for Intel macOS on Python 3.14+ or Alpine/musl;
use Python ≤ 3.13 there.

For correct reading order on multi-column PDFs (adds `pymupdf4llm`, which pulls `pymupdf_layout`/`onnxruntime`):

```bash
pip install 'pdf-mcp[multicolumn]'
```

Without it, multi-column pages fall back to positional-sort extraction, which can interleave columns.

Japanese/Chinese/Korean PDFs work out of the box: keyword search uses a
char-split FTS index that matches unspaced CJK terms, and semantic CJK
search is covered by the default install.

For OCR on scanned PDFs (requires system Tesseract):

```bash
# macOS
brew install tesseract

# Ubuntu/Debian
apt install tesseract-ocr

# On Windows, download the installer from:
# https://github.com/UB-Mannheim/tesseract/wiki
# Then add the install directory to your PATH.
```

## Quick Start

Choose your MCP client below to get started:

<details open>
<summary><strong>Claude Code</strong></summary>

```bash
claude mcp add pdf-mcp -- pdf-mcp
```

Or add to `~/.claude.json`:

```json
{
  "mcpServers": {
    "pdf-mcp": {
      "command": "pdf-mcp"
    }
  }
}
```

</details>

<details>
<summary><strong>Claude Desktop</strong></summary>

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "pdf-mcp": {
      "command": "pdf-mcp"
    }
  }
}
```

Config file location:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Restart Claude Desktop after updating the config.

</details>

<details>
<summary><strong>Visual Studio Code</strong></summary>

Requires VS Code 1.101+ with GitHub Copilot.

**CLI:**
```bash
code --add-mcp '{"name":"pdf-mcp","command":"pdf-mcp"}'
```

**Command Palette:**
1. Open Command Palette (`Cmd/Ctrl+Shift+P`)
2. Run `MCP: Open User Configuration` (global) or `MCP: Open Workspace Folder Configuration` (project-specific)
3. Add the configuration:
   ```json
   {
     "servers": {
       "pdf-mcp": {
         "command": "pdf-mcp"
       }
     }
   }
   ```
4. Save. VS Code will automatically load the server.

**Manual:** Create `.vscode/mcp.json` in your workspace:
```json
{
  "servers": {
    "pdf-mcp": {
      "command": "pdf-mcp"
    }
  }
}
```

</details>

<details>
<summary><strong>Codex CLI</strong></summary>

```bash
codex mcp add pdf-mcp -- pdf-mcp
```

Or configure manually in `~/.codex/config.toml`:

```toml
[mcp_servers.pdf-mcp]
command = "pdf-mcp"
```

</details>

<details>
<summary><strong>Kiro</strong></summary>

Create or edit `.kiro/settings/mcp.json` in your workspace:

```json
{
  "mcpServers": {
    "pdf-mcp": {
      "command": "pdf-mcp",
      "args": [],
      "disabled": false
    }
  }
}
```

Save and restart Kiro.

</details>

<details>
<summary><strong>Other MCP Clients</strong></summary>

Most MCP clients use a standard configuration format:

```json
{
  "mcpServers": {
    "pdf-mcp": {
      "command": "pdf-mcp"
    }
  }
}
```

With `uvx` (for isolated environments):

```json
{
  "mcpServers": {
    "pdf-mcp": {
      "command": "uvx",
      "args": ["pdf-mcp"]
    }
  }
}
```

</details>

### Verify Installation

```bash
pdf-mcp --help
```

## Tools

The typical pattern: call `pdf_info` first to plan, then `pdf_search` to locate; its paragraph excerpts are often enough to answer directly. Use `pdf_read_pages` or `pdf_read_all` when you need deeper context. For a folder of PDFs, start with `pdf_corpus_overview` to triage, then `pdf_corpus_search` to search across documents.

| Tool | What it does |
|------|--------------|
| `pdf_info` | Page count, metadata, TOC summary, scanned-page detection. **Call first.** Pass `content_trust=True` for a `content_trust` block (`suspicious`, `hidden_text_runs`, `hidden_chars`, `injection_in_hidden`, `pages_flagged`, `signals`); add `detail=True` for per-span `spans`. |
| `pdf_get_toc` | Full table of contents for documents with >50 bookmarks |
| `pdf_corpus_warm` | Warm a folder (or list) of PDFs into the cache, text and optional embeddings, within a time budget. Returns per-doc status plus `unprocessed`/`skipped`. |
| `pdf_corpus_overview` | Per-document triage cards for a folder: title, page count, top TOC entries, text coverage. Auto-warms within the budget. |
| `pdf_corpus_search` | Search across a folder of PDFs (keyword, semantic, or hybrid), returning ranked hits with document and page provenance, excerpts, and coverage. |
| `pdf_read_pages` | Read specific pages or ranges; OCR-on-demand; embedded images + tables, each with source `bbox` + `clip` coordinates. Always returns `hidden_text_detected` (response level) and per-page `hidden_text`; `hidden_text_detected: true` means some returned text was invisible to a human reader and should be treated as especially untrusted. |
| `pdf_read_all` | Read entire document in one call (byte-capped for safety). Always returns `hidden_text_detected`; `hidden_text_detected: true` means some returned text was invisible to a human reader and should be treated as especially untrusted. |
| `pdf_render_pages` | Render pages as PNG for vision models: diagrams, handwriting, scans |
| `pdf_extract_chart` | Extract chart data as exact `(x, y)` tables from vector charts; declines with a rendered image when not reliably extractable |
| `pdf_search` | Hybrid RRF search (keyword + semantic), page or section granularity, optional paragraph excerpts (paragraph hits also carry `bbox` + `clip` coordinates) |
| `pdf_cache_stats` | Per-document cache breakdown + total size |
| `pdf_cache_clear` | Clear expired or all cache entries |
| `server_info` | Which optional features (column-aware, OCR, semantic) and config are active. **Call before feature-dependent calls.** |

Example prompts:

```
"Read the PDF at /path/to/document.pdf"
"Which pages discuss supply chain risks?"
"Find sections about the training process"
"Show me what page 5 looks like"
"OCR pages 3-5 of the scanned PDF"
```

See **[docs/tool-reference.md](docs/tool-reference.md)** for the complete reference: every parameter, response shape, security contract, and example. For semantic-search model selection, see **[docs/embedding-models.md](docs/embedding-models.md)**.

## Example Workflow

For a large document (e.g., a 200-page annual report):

```
User: "Summarize the risk factors in this annual report"

Agent workflow:
1. pdf_info("report.pdf")
   → 200 pages, TOC shows "Risk Factors" on page 89

2. pdf_search("report.pdf", "risk factors")
   → Matches with structural paragraph excerpts: each excerpt
     is the bullet, paragraph, or heading that matched, not a
     fixed-width window. Often enough to answer directly.

3. If excerpts are sufficient → synthesize answer

4. If more context needed:
   pdf_read_pages("report.pdf", "89-95")
   → Full page text for deeper reading
```

## Remote / HTTP transport

STDIO remains the default and is what every example above uses. A second entry
point serves the same tools over HTTP, but the two transports suit different
jobs:

| transport | what it serves | use it for |
|---|---|---|
| **STDIO** (default) | any local file the agent can name, since agent and server share a filesystem | ad hoc documents on your own machine |
| **HTTP** (`pdf-mcp-http`) | a curated corpus on the server, plus `https://` URLs it can fetch | clients that cannot spawn a process (Anthropic API MCP connector, claude.ai custom connectors), and a warm corpus shared by several clients |

```bash
export PDF_MCP_AUTH_TOKEN="$(openssl rand -hex 32)"
pdf-mcp-http
```

Because paths resolve on the server, an agent connected over HTTP reads what is
already there: files under an allow-listed root, or a URL the server fetches. It
cannot hand over a file from its own machine. Call `server_info` to discover the
roots a server will open. See
**[Getting documents to the server](docs/remote-access.md#getting-documents-to-the-server)**.

It is single-tenant and fails closed: without an auth token and a `[paths]`
allow list, the process exits rather than starting an open endpoint. Before
you deploy it, read **[docs/remote-access.md](docs/remote-access.md)** for the
trust boundary and the threat model versus stdio, and
**[docs/configuration.md](docs/configuration.md#http-transport-setup)** for
setup, client config, and token rotation.

### Docker

```bash
./deploy.sh              # generates .env with a token, pulls the image, starts, health-checks
cp your.pdf documents/   # the intake path: this folder is the server's /data/pdfs
```

The image is published to GHCR for amd64 and arm64, so nothing is compiled
locally. Everything is baked in (OCR, column-aware extraction, embedding
model), so all tools work on the first request. The container runs as a
non-root user and publishes to host loopback only; put a TLS proxy in front
for public access.

`./deploy.sh --help` lists the lifecycle commands, including `--build` to
build locally instead of pulling. For the environment variables (host port,
image tag, auth token) and the deployment guards, see
[docs/configuration.md](docs/configuration.md).

## Configuration

pdf-mcp works out of the box with no configuration. To restrict which paths and URL hosts the server can access, tune cache and worker settings, or understand what's cached, see **[docs/configuration.md](docs/configuration.md)**.

- **Access control**: `~/.config/pdf-mcp/config.toml` allow/deny rules for paths and URLs, plus response byte caps
- **Content-trust phrases**: extend the hidden-text `injection_in_hidden` hint with your own (including non-English) phrases via `[content_trust].injection_phrases`
- **Environment variables**: cache directory, TTL, and parallel OCR/render worker count
- **HTTP transport setup**: token generation, TLS, client config, and token rotation for `pdf-mcp-http`
- **Caching**: SQLite-backed persistence, what's cached, and invalidation

## Roadmap

See [ROADMAP.md](docs/ROADMAP.md) for planned features and release history.

## Contributing

Contributions are welcome. See **[docs/contributing.md](docs/contributing.md)** for setup, checks, the coherence eval harness, and quality-loop guidelines.

## Contributors

Thank you to everyone who has helped improve this project through code, reviews, testing, and feature requests:

<!-- contributors:start -->
[@Summer907](https://github.com/Summer907) · [@ebbsanchez](https://github.com/ebbsanchez) · [@VooDisss](https://github.com/VooDisss) · [@DerDennisOP](https://github.com/DerDennisOP) · [@deepdmk](https://github.com/deepdmk)
<!-- contributors:end -->

<a href="https://github.com/jztan/pdf-mcp/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=jztan/pdf-mcp" alt="Contributors" />
</a>

Per-release contributor credits are listed in the [Changelog](./CHANGELOG.md).

## Security

Found a vulnerability? See [SECURITY.md](SECURITY.md) for the threat model, reporting channel, and expected response timeline. Please do not open a public GitHub issue for unpatched security reports.

## License

MIT. See [LICENSE](LICENSE).

## Links

- [pdf-mcp on PyPI](https://pypi.org/project/pdf-mcp/)
- [pdf-mcp on GitHub](https://github.com/jztan/pdf-mcp)

## Blog posts

**The story behind the releases.** Building pdf-mcp keeps surprising me: benchmarks that go the wrong way, formats that break everything, features I had to remove. I write about that thinking in [The Dispatch](https://blog.jztan.com/newsletter/?utm_source=github&utm_medium=readme&utm_campaign=pdf-mcp). Come along if that's your kind of thing.

Background, benchmarks, and design notes from building pdf-mcp:

**Getting started**

- [How I Built pdf-mcp](https://blog.jztan.com/how-i-built-pdf-mcp-solving-claude-large-pdf-limitations/?utm_source=github&utm_medium=readme&utm_campaign=pdf-mcp): The problem with large PDFs in AI agents and a working solution
- [How Claude Code Actually Reads PDFs](https://blog.jztan.com/how-claude-code-actually-reads-pdfs-lessons-from-building-an-mcp-server/?utm_source=github&utm_medium=readme&utm_campaign=pdf-mcp): How AI agents use pdf-mcp tools to read and navigate PDF documents
- [How AI Agents Should Read PDFs: 5 Patterns That Survived Production](https://blog.jztan.com/ai-agent-pdf-reading-patterns/?utm_source=github&utm_medium=readme&utm_campaign=pdf-mcp): Five production-tested patterns for how agents should navigate PDFs at scale
- [A Knowledge Base Is Just a Folder](https://blog.jztan.com/ai-agent-pdf-knowledge-base/?utm_source=github&utm_medium=readme&utm_campaign=pdf-mcp): Turning a folder of PDFs into an agent knowledge base with the corpus tools, no ingestion pipeline or vector store

**Search & retrieval**

- [Semantic vs Keyword Search for AI Agents](https://blog.jztan.com/semantic-vs-keyword-search-ai-agents/?utm_source=github&utm_medium=readme&utm_campaign=pdf-mcp): Benchmarks and a dual-search routing pattern: FTS5 for exact identifiers, embeddings for natural language
- [Hybrid Search vs Query Routing for AI Agents](https://blog.jztan.com/hybrid-search-vs-query-routing-ai-agents/?utm_source=github&utm_medium=readme&utm_campaign=pdf-mcp): Why pdf-mcp uses hybrid RRF instead of query routing: benchmarks showing RRF wins across query types
- [Section Chunking vs Page Chunking for AI Agents](https://blog.jztan.com/section-chunking-vs-page-chunking-ai-agents/?utm_source=github&utm_medium=readme&utm_campaign=pdf-mcp): Why section-aware search delivers full section content in one call while page-mode costs 2–6 extra tool calls per query
- [Section-Level RAG: Why BM25 Beat Hybrid Search in My Benchmark](https://blog.jztan.com/bm25-vs-hybrid-search-section-rag/?utm_source=github&utm_medium=readme&utm_campaign=pdf-mcp): Why pdf-mcp's section-grain search is BM25-only: hybrid RRF caused a 33% lexical regression at section grain, so granularity decides the search technique
- [How One Search Change Eliminated an Entire Agent Step](https://blog.jztan.com/how-paragraph-excerpts-changed-agent-behavior/?utm_source=github&utm_medium=readme&utm_campaign=pdf-mcp): Switching pdf_search from fixed-width snippets to paragraph excerpts turned it from a pivot tool into a terminal tool: 97% vs 80% answer containment across a 30-query benchmark

**Engineering & security**

- [MCP Server Security: 8 Vulnerabilities](https://blog.jztan.com/mcp-server-security-8-vulnerabilities/?utm_source=github&utm_medium=readme&utm_campaign=pdf-mcp): What we found when we audited an MCP server for security holes
- [Your LLM Is Free QA for Your MCP Server](https://blog.jztan.com/llm-free-qa-mcp-server/?utm_source=github&utm_medium=readme&utm_campaign=pdf-mcp): Four Payload UX bugs in pdf-mcp that schema tests missed but Claude Desktop surfaced during real use
- [Why Multi-Column PDFs Scramble Reading Order in RAG](https://blog.jztan.com/multi-column-pdf-reading-order/?utm_source=github&utm_medium=readme&utm_campaign=pdf-mcp): Fixing two-column extraction (0.564 → 0.816 fidelity), the title-page author-grid regression it caused, and the aggregate metric that stayed blind to both