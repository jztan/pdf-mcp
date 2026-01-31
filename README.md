# pdf-mcp 📄

[![PyPI version](https://img.shields.io/pypi/v/pdf-mcp)](https://pypi.org/project/pdf-mcp/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![GitHub Issues](https://img.shields.io/github/issues/jztan/pdf-mcp)](https://github.com/jztan/pdf-mcp/issues)
[![CI](https://github.com/jztan/pdf-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/jztan/pdf-mcp/actions/workflows/ci.yml)
[![Downloads](https://pepy.tech/badge/pdf-mcp)](https://pepy.tech/project/pdf-mcp)

**Production-ready MCP server for PDF processing with intelligent caching.**

A Python implementation of the Model Context Protocol (MCP) server that enables AI agents like Claude to read, search, and extract content from PDF files efficiently.

## ✨ Features

- 🚀 **8 Specialized Tools** - Purpose-built tools for different PDF operations
- 💾 **SQLite Caching** - Persistent cache survives server restarts (essential for STDIO transport)
- 📄 **Smart Pagination** - Read large PDFs in manageable chunks
- 🔍 **Full-Text Search** - Find content without loading entire document
- 🖼️ **Image Extraction** - Extract images as base64 PNG
- 🌐 **URL Support** - Read PDFs from HTTP/HTTPS URLs
- ⚡ **Fast Subsequent Access** - Cached pages load instantly

## 📦 Installation

```bash
pip install pdf-mcp
```

## 🚀 Quick Start

### Claude Code

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

### Claude Desktop

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

**Location of config file:**
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

After updating the config, restart Claude Desktop to load the MCP server.

## 🛠️ Tools

### 1. `pdf_info` - Get Document Information

**Always call this first** to understand the document before reading.

```
"Read the PDF at /path/to/document.pdf"
```

Returns: page count, metadata, table of contents, file size, estimated tokens.

### 2. `pdf_read_pages` - Read Specific Pages

Read pages in chunks to manage context size.

```
"Read pages 1-10 of the PDF"
"Read pages 15, 20, and 25-30"
```

### 3. `pdf_read_all` - Read Entire Document

For small documents only (has safety limit).

```
"Read the entire PDF (it's only 10 pages)"
```

### 4. `pdf_search` - Search Within PDF

Find relevant pages before loading content.

```
"Search for 'quarterly revenue' in the PDF"
```

### 5. `pdf_get_toc` - Get Table of Contents

```
"Show me the table of contents"
```

### 6. `pdf_extract_images` - Extract Images

```
"Extract images from pages 1-5"
```

### 7. `pdf_cache_stats` - View Cache Statistics

```
"Show PDF cache statistics"
```

### 8. `pdf_cache_clear` - Clear Cache

```
"Clear expired PDF cache entries"
```

## 📋 Example Workflow

For a large document (e.g., 200-page annual report):

```
User: "Summarize the risk factors in this annual report"

Claude's workflow:
1. pdf_info("report.pdf") 
   → Learns: 200 pages, TOC shows "Risk Factors" on page 89

2. pdf_search("report.pdf", "risk factors")
   → Finds relevant pages: 89-110

3. pdf_read_pages("report.pdf", "89-100")
   → Reads first batch

4. pdf_read_pages("report.pdf", "101-110")
   → Reads second batch

5. Synthesizes answer from chunks
```

## 💾 Caching

The server uses **SQLite for persistent caching** because MCP with STDIO transport spawns a new process for each conversation.

### Cache Location
- `~/.cache/pdf-mcp/cache.db`

### What's Cached
| Data | Benefit |
|------|---------|
| Metadata | Instant document info |
| Page text | Skip re-extraction |
| Images | Skip re-encoding |
| TOC | Fast navigation |

### Cache Invalidation
- Automatic when file modification time changes
- Manual via `pdf_cache_clear` tool
- TTL: 24 hours (configurable)

## ⚙️ Configuration

Environment variables:

```bash
# Cache directory (default: ~/.cache/pdf-mcp)
PDF_MCP_CACHE_DIR=/path/to/cache

# Cache TTL in hours (default: 24)
PDF_MCP_CACHE_TTL=48
```

## 🔧 Development

```bash
# Clone
git clone https://github.com/jztan/pdf-mcp.git
cd pdf-mcp

# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Type checking
mypy src/

# Linting
ruff check src/
```

## 📊 Comparison

| Feature | Traditional Approach | pdf-mcp |
|---------|---------------------|---------|
| Large PDFs | Context overflow | Chunked reading |
| Repeated access | Re-parse every time | SQLite cache |
| Find content | Load everything | Search first |
| Multiple tools | One monolithic tool | 8 specialized tools |

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## 📄 License

MIT License - see [LICENSE](LICENSE) file.

## 🔗 Links

- [PyPI Package](https://pypi.org/project/pdf-mcp/)
- [MCP Documentation](https://modelcontextprotocol.io/)
- [GitHub Repository](https://github.com/jztan/pdf-mcp)
- [Blog Post: How I Built pdf-mcp](https://blog.jztan.com/how-i-built-pdf-mcp-solving-claude-large-pdf-limitations/)

