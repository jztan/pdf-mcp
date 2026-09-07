# Examples

Using pdf-mcp as a plain Python library, from the Anthropic SDK, on
documents the API will not accept as an upload.

Both scripts import pdf-mcp's tools directly. They are ordinary
functions, so nothing here runs an MCP server or spawns a subprocess:

```python
from pdf_mcp.server import pdf_read_pages, pdf_search
```

```bash
pip install pdf-mcp anthropic
export ANTHROPIC_API_KEY=...
```

| Script | Use it when | Measured cost |
|---|---|---|
| [`search_first.py`](search_first.py) | You have a question. Search the PDF on disk, send only the pages that matched. | about $0.008 per question |
| [`whole_document.py`](whole_document.py) | You need the whole document (summary, translation, every instance of something). Window the text, render only the pages that are pictures. | about $0.53 for 171 pages |

```bash
python examples/search_first.py report.pdf "What were 2022 revenues?"
python examples/whole_document.py report.pdf
```

Do not run `search_first.py` on a whole-document task. Given search tools
over a chart, Claude Haiku 4.5 answered 5 of 8 questions wrong and
confidently, because retrieval raises its confidence that it found the
answer. Charts need the second script's vision pass.

## Notes

These make billed API calls, so they are not run in CI and can drift if a
tool's response shape changes. Costs are Claude Haiku 4.5 and Claude
Opus 5 list price, and both scripts print their own token usage.

Measured on 2026-09-06 against a 171-page Form 10-K with pdf-mcp 3.1.0.
The full write-up, including where the page and token ceilings actually
sit, is in [How to Send PDFs Over 100 Pages to Claude's
API](https://blog.jztan.com/llm-api-pdf-page-limits/?utm_source=github&utm_medium=referral&utm_campaign=pdf-mcp&utm_content=examples-readme).
