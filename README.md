# pdf-mcp

[![PyPI version](https://img.shields.io/pypi/v/pdf-mcp)](https://pypi.org/project/pdf-mcp/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![GitHub Issues](https://img.shields.io/github/issues/jztan/pdf-mcp)](https://github.com/jztan/pdf-mcp/issues)
[![CI](https://github.com/jztan/pdf-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/jztan/pdf-mcp/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/jztan/pdf-mcp/graph/badge.svg)](https://codecov.io/gh/jztan/pdf-mcp)
[![Downloads](https://pepy.tech/badge/pdf-mcp)](https://pepy.tech/project/pdf-mcp)

A [Model Context Protocol](https://modelcontextprotocol.io/) (MCP) server that enables AI agents to read, search, and extract content from PDF files. Built with Python and PyMuPDF, with SQLite-based caching for persistence across server restarts.

**mcp-name: io.github.jztan/pdf-mcp**

## Features

- **8 specialized tools** for different PDF operations
- **SQLite caching** — persistent cache survives server restarts (essential for STDIO transport)
- **Paginated reading** — read large PDFs in manageable chunks
- **Full-text search** — FTS5 index with BM25 ranking and Porter stemming
- **Semantic search** — find pages by meaning using local embeddings (no external API)
- **Image extraction** — per-page images returned as PNG file paths alongside text
- **Table extraction** — per-page tables with header and row data, detected via visible borders
- **URL support** — read PDFs from HTTP/HTTPS URLs

## Installation

```bash
pip install pdf-mcp
```

For semantic search (adds `fastembed` and `numpy`, ~67 MB model download on first use):

```bash
pip install 'pdf-mcp[semantic]'
```

## Quick Start

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

Requires VS Code 1.102+ with GitHub Copilot.

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

### `pdf_info` — Get Document Information

Returns page count, metadata, file size, and estimated token count. **Call this first** to understand a document before reading it. Includes `toc_entry_count` and inline TOC entries when the document has ≤50 bookmarks; larger TOCs (e.g. slide decks) return `toc_truncated: true` — use `pdf_get_toc` to retrieve the full outline.

```
"Read the PDF at /path/to/document.pdf"
```

### `pdf_read_pages` — Read Specific Pages

Read selected pages to manage context size. Each page dict includes `text`, `images`/`image_count`, and `tables`/`table_count`. Tables are extracted as structured data (header + rows) and inlined directly in the page response — no separate tool call needed. Table detection requires visible borders in the PDF.

```
"Read pages 1-10 of the PDF"
"Read pages 15, 20, and 25-30"
```

### `pdf_read_all` — Read Entire Document

Read a complete document in one call. Subject to a safety limit on page count.

```
"Read the entire PDF (it's only 10 pages)"
```

### `pdf_search` — Search Within PDF

Find relevant pages before loading content. Uses a SQLite FTS5 index with Porter stemming and BM25 relevance ranking — results are ordered by relevance, not page number. Morphological variants are matched automatically (e.g. searching "managing" finds pages containing "management"). Falls back to a linear scan on SQLite builds without FTS5 support.

```
"Search for 'quarterly revenue' in the PDF"
```

### `pdf_semantic_search` — Search by Meaning

Find pages by conceptual similarity rather than exact keywords. Searching "revenue growth" matches pages about "sales increase" or "financial performance" even without literal keyword overlap. Uses `BAAI/bge-small-en-v1.5` embeddings (384-dim, local ONNX Runtime — no external API, no GPU required).

Embeddings are generated once per page and cached in SQLite. The first call for a document takes ~8–15 s on CPU; subsequent queries are instant.

Requires: `pip install 'pdf-mcp[semantic]'`

```
"Find pages about revenue growth in the PDF"
"Which pages discuss supply chain risks?"
```

### `pdf_get_toc` — Get Table of Contents

```
"Show me the table of contents"
```

### `pdf_cache_stats` — View Cache Statistics

```
"Show PDF cache statistics"
```

### `pdf_cache_clear` — Clear Cache

```
"Clear expired PDF cache entries"
```

## Example Workflow

For a large document (e.g., a 200-page annual report):

```
User: "Summarize the risk factors in this annual report"

Agent workflow:
1. pdf_info("report.pdf")
   → 200 pages, TOC shows "Risk Factors" on page 89

2. pdf_search("report.pdf", "risk factors")
   → Relevant pages: 89-110

3. pdf_read_pages("report.pdf", "89-100")
   → First batch

4. pdf_read_pages("report.pdf", "101-110")
   → Second batch

5. Synthesize answer from chunks
```

## Caching

The server uses SQLite for persistent caching. This is necessary because MCP servers using STDIO transport are spawned as a new process for each conversation.

**Cache location:** `~/.cache/pdf-mcp/cache.db`

**What's cached:**

| Data | Benefit |
|------|---------|
| Metadata | Avoid re-parsing document info |
| Page text | Skip re-extraction |
| Images | Skip re-encoding |
| Tables | Skip re-detection |
| TOC | Skip re-parsing |
| FTS5 index | O(log N) search with BM25 ranking after first query |
| Embeddings | Instant semantic search after first indexing run |

**Cache invalidation:**
- Automatic when file modification time changes
- Manual via the `pdf_cache_clear` tool
- TTL: 24 hours (configurable)

## Configuration

Environment variables:

```bash
# Cache directory (default: ~/.cache/pdf-mcp)
PDF_MCP_CACHE_DIR=/path/to/cache

# Cache TTL in hours (default: 24)
PDF_MCP_CACHE_TTL=48
```

## Development

```bash
git clone https://github.com/jztan/pdf-mcp.git
cd pdf-mcp

# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Type checking
mypy src/

# Linting
flake8 src/ tests/

# Formatting
black src/ tests/
```

## Why pdf-mcp?

| | Without pdf-mcp | With pdf-mcp |
|---|---|---|
| Large PDFs | Context overflow | Chunked reading |
| Token budgeting | Guess and overflow | Estimated tokens before reading |
| Finding content | Load everything | Keyword search (FTS5 + BM25) or semantic search (local embeddings) |
| Tables | Lost in raw text | Extracted and inlined per page |
| Images | Ignored | Extracted as PNG files |
| Repeated access | Re-parse every time | SQLite cache |
| Tool design | Single monolithic tool | 7 specialized tools |

## Roadmap

See [ROADMAP.md](ROADMAP.md) for planned features and release history.

## Contributing

Contributions are welcome. Please submit a pull request.

## License

MIT — see [LICENSE](LICENSE).

## Links

- [pdf-mcp on PyPI](https://pypi.org/project/pdf-mcp/)
- [pdf-mcp on GitHub](https://github.com/jztan/pdf-mcp)
- [How I Built pdf-mcp](https://blog.jztan.com/how-i-built-pdf-mcp-solving-claude-large-pdf-limitations/) — The problem with large PDFs in AI agents and a working solution
- [MCP Server Security: 8 Vulnerabilities](https://blog.jztan.com/mcp-server-security-8-vulnerabilities/) — What we found when we audited an MCP server for security holes
- [How Claude Code Actually Reads PDFs](https://blog.jztan.com/how-claude-code-actually-reads-pdfs-lessons-from-building-an-mcp-server/) — How chunked reading, FTS5, and SQLite caching work together
- [Semantic vs Keyword Search for AI Agents](https://blog.jztan.com/semantic-vs-keyword-search-ai-agents/) — Benchmarks and a dual-search routing pattern: FTS5 for exact identifiers, embeddings for natural language
