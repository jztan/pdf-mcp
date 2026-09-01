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
| Large PDFs | Context overflow | Read only the pages you need |
| Finding content | Load everything | Hybrid search: BM25 keyword + semantic |
| Folders of PDFs | One document at a time | Warm, triage, and search a whole folder |
| Tables and charts | Lost in raw text | Structured rows, and `(x, y)` data from vector charts |
| Multi-column and vertical layouts | Columns interleaved | Correct reading order, including Japanese tategaki |
| Scanned PDFs | No text at all | OCR via Tesseract, parallel across pages |
| Repeated access | Re-parse every time | SQLite cache that survives restarts |
| Hidden or injected text | Silently ingested | Flagged as untrusted, nothing stripped |

## Installation

```bash
pip install pdf-mcp
```

That is the whole install: hybrid search, corpus tools, multi-column and
CJK reading order all work out of the box.

OCR on scanned PDFs additionally needs system Tesseract:

```bash
brew install tesseract        # macOS
apt install tesseract-ocr     # Ubuntu/Debian
winget install Tesseract-OCR  # Windows
```

## Quick Start

```bash
claude mcp add pdf-mcp -- pdf-mcp
```

Then ask Claude to read a PDF. For Claude Desktop, VS Code, Codex CLI,
Kiro, or any other MCP client, see **[docs/clients.md](docs/clients.md)**.

Why this exists, and what broke along the way: [Claude's 100-page PDF limit and how I got around it](https://blog.jztan.com/how-i-built-pdf-mcp-solving-claude-large-pdf-limitations/?utm_source=github&utm_medium=referral&utm_campaign=pdf-mcp&utm_content=quickstart-how-i-built-pdf-mcp-solving-claude-large-pdf-limitations)

## Tools

13 specialized tools rather than one monolithic one. Typical pattern:
`pdf_info` to plan, `pdf_search` to locate (its paragraph excerpts often
answer the question outright), `pdf_read_pages` when you need more. For a
folder, `pdf_corpus_overview` to triage, then `pdf_corpus_search`.

| Tool | What it does |
|------|--------------|
| `pdf_info` | Page count, metadata, TOC summary, scanned-page detection. **Call first.** |
| `pdf_search` | Hybrid search (keyword + semantic), page or section granularity, paragraph or context-window excerpts with source coordinates |
| `pdf_read_pages` | Read specific pages or ranges, with OCR on demand, tables, and embedded images |
| `pdf_read_all` | Read a whole document in one call, byte-capped |
| `pdf_get_toc` | Full table of contents for documents with many bookmarks |
| `pdf_render_pages` | Render pages as PNG for vision models: diagrams, handwriting, scans |
| `pdf_extract_chart` | Chart data as exact `(x, y)` tables, read from plot geometry |
| `pdf_corpus_warm` | Warm a folder of PDFs into the cache within a time budget |
| `pdf_corpus_overview` | Per-document triage cards for a folder |
| `pdf_corpus_search` | Search across a folder, with document and page provenance; `excerpt_style="auto"` picks the excerpt unit per query |
| `pdf_cache_stats` | Per-document cache breakdown and total size |
| `pdf_cache_clear` | Clear expired or all cache entries |
| `server_info` | Which optional features and config are active |

Text returned by any of these is untrusted content extracted from a PDF.
`pdf_info(content_trust=True)` reports hidden text a human reader cannot
see, and the read tools flag it per page.

Example prompts:

```
"Read the PDF at /path/to/document.pdf"
"Which pages discuss supply chain risks?"
"Find sections about the training process"
"Show me what page 5 looks like"
"OCR pages 3-5 of the scanned PDF"
```

Full reference, every parameter and response shape:
**[docs/tool-reference.md](docs/tool-reference.md)**. Embedding model
selection: **[docs/embedding-models.md](docs/embedding-models.md)**.

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

STDIO is the default and is what every example above uses. `pdf-mcp-http`
serves the same tools over HTTP, for clients that cannot spawn a process
(the Anthropic API MCP connector, claude.ai custom connectors) and for a
warm corpus shared by several clients.

```bash
export PDF_MCP_AUTH_TOKEN="$(openssl rand -hex 32)"
pdf-mcp-http
```

Paths resolve on the server, so an HTTP agent reads what is already there:
files under an allow-listed root, or a URL the server fetches. It cannot
hand over a file from its own machine. It is single-tenant and fails
closed: with no auth token and no `[paths]` allow list, the process exits
rather than serving an open endpoint.

Docker images are published to GHCR for amd64 and arm64, with everything
baked in, so every tool works on the first request:

```bash
./deploy.sh              # token, image, start, health-check
cp your.pdf documents/   # this folder is the server's /data/pdfs
```

Read **[docs/remote-access.md](docs/remote-access.md)** for the trust
boundary and threat model before deploying, and
**[docs/configuration.md](docs/configuration.md#http-transport-setup)**
for setup, client config, and token rotation.

## Configuration

pdf-mcp works out of the box. To restrict which paths and URL hosts the
server may touch, tune cache and worker settings, or add your own
content-trust phrases, see **[docs/configuration.md](docs/configuration.md)**.

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

**The story behind the releases.** Building pdf-mcp keeps surprising me: benchmarks that go the wrong way, formats that break everything, features I had to remove. I write about that thinking in [The Dispatch](https://blog.jztan.com/newsletter/?utm_source=github&utm_medium=referral&utm_campaign=pdf-mcp&utm_content=list-newsletter). Come along if that's your kind of thing.

Background, benchmarks, and design notes from building pdf-mcp:

**Getting started**

- [How I Built pdf-mcp](https://blog.jztan.com/how-i-built-pdf-mcp-solving-claude-large-pdf-limitations/?utm_source=github&utm_medium=referral&utm_campaign=pdf-mcp&utm_content=list-how-i-built-pdf-mcp-solving-claude-large-pdf-limitations): The problem with large PDFs in AI agents and a working solution
- [How Claude Code Actually Reads PDFs](https://blog.jztan.com/how-claude-code-actually-reads-pdfs-lessons-from-building-an-mcp-server/?utm_source=github&utm_medium=referral&utm_campaign=pdf-mcp&utm_content=list-how-claude-code-actually-reads-pdfs-lessons-from-building-an-mcp-server): How AI agents use pdf-mcp tools to read and navigate PDF documents
- [How AI Agents Should Read PDFs: 5 Patterns That Survived Production](https://blog.jztan.com/ai-agent-pdf-reading-patterns/?utm_source=github&utm_medium=referral&utm_campaign=pdf-mcp&utm_content=list-ai-agent-pdf-reading-patterns): Five production-tested patterns for how agents should navigate PDFs at scale

**Corpus & multi-document search**

- [A Knowledge Base Is Just a Folder](https://blog.jztan.com/ai-agent-pdf-knowledge-base/?utm_source=github&utm_medium=referral&utm_campaign=pdf-mcp&utm_content=list-ai-agent-pdf-knowledge-base): Turning a folder of PDFs into an agent knowledge base with the corpus tools, no ingestion pipeline or vector store
- [Cross-Document Retrieval for AI Agents Without a Vector Database](https://blog.jztan.com/rag-without-vector-database/?utm_source=github&utm_medium=referral&utm_campaign=pdf-mcp&utm_content=list-rag-without-vector-database): Why BM25 scores don't merge across per-document indexes but ranks do, and how two-stage RRF puts a gold document in the top 3 on 89.9% of 89 queries over a 100-PDF corpus

**Search & retrieval**

- [Semantic vs Keyword Search for AI Agents](https://blog.jztan.com/semantic-vs-keyword-search-ai-agents/?utm_source=github&utm_medium=referral&utm_campaign=pdf-mcp&utm_content=list-semantic-vs-keyword-search-ai-agents): Benchmarks and a dual-search routing pattern: FTS5 for exact identifiers, embeddings for natural language
- [Hybrid Search vs Query Routing for AI Agents](https://blog.jztan.com/hybrid-search-vs-query-routing-ai-agents/?utm_source=github&utm_medium=referral&utm_campaign=pdf-mcp&utm_content=list-hybrid-search-vs-query-routing-ai-agents): Why pdf-mcp uses hybrid RRF instead of query routing: benchmarks showing RRF wins across query types
- [Section Chunking vs Page Chunking for AI Agents](https://blog.jztan.com/section-chunking-vs-page-chunking-ai-agents/?utm_source=github&utm_medium=referral&utm_campaign=pdf-mcp&utm_content=list-section-chunking-vs-page-chunking-ai-agents): Why section-aware search delivers full section content in one call while page-mode costs 2–6 extra tool calls per query
- [Section-Level RAG: Why BM25 Beat Hybrid Search in My Benchmark](https://blog.jztan.com/bm25-vs-hybrid-search-section-rag/?utm_source=github&utm_medium=referral&utm_campaign=pdf-mcp&utm_content=list-bm25-vs-hybrid-search-section-rag): Why pdf-mcp's section-grain search is BM25-only: hybrid RRF caused a 33% lexical regression at section grain, so granularity decides the search technique
- [How One Search Change Eliminated an Entire Agent Step](https://blog.jztan.com/how-paragraph-excerpts-changed-agent-behavior/?utm_source=github&utm_medium=referral&utm_campaign=pdf-mcp&utm_content=list-how-paragraph-excerpts-changed-agent-behavior): Switching pdf_search from fixed-width snippets to paragraph excerpts turned it from a pivot tool into a terminal tool: 97% vs 80% answer containment across a 30-query benchmark

**Engineering & security**

- [MCP Server Security: 8 Vulnerabilities](https://blog.jztan.com/mcp-server-security-8-vulnerabilities/?utm_source=github&utm_medium=referral&utm_campaign=pdf-mcp&utm_content=list-mcp-server-security-8-vulnerabilities): What we found when we audited an MCP server for security holes
- [Your LLM Is Free QA for Your MCP Server](https://blog.jztan.com/llm-free-qa-mcp-server/?utm_source=github&utm_medium=referral&utm_campaign=pdf-mcp&utm_content=list-llm-free-qa-mcp-server): Four Payload UX bugs in pdf-mcp that schema tests missed but Claude Desktop surfaced during real use
- [Why Multi-Column PDFs Scramble Reading Order in RAG](https://blog.jztan.com/multi-column-pdf-reading-order/?utm_source=github&utm_medium=referral&utm_campaign=pdf-mcp&utm_content=list-multi-column-pdf-reading-order): Fixing two-column extraction (0.564 → 0.816 fidelity), the title-page author-grid regression it caused, and the aggregate metric that stayed blind to both
- [How I Fixed Vertical Japanese PDF Extraction](https://blog.jztan.com/vertical-japanese-pdf-reading-order/?utm_source=github&utm_medium=referral&utm_campaign=pdf-mcp&utm_content=list-vertical-japanese-pdf-reading-order): Tategaki pages extract scrambled because reading order is geometric, not stored; rebuilding it from glyph positions (columns right to left, characters top to bottom), with no OCR and no new dependency
