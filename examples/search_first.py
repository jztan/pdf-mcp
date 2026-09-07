"""Answer a question about a PDF that is too long to upload.

The Claude API caps a request at 100 PDF pages on models with a context
window under 1M tokens, and 600 above it. Both caps are on the *request*,
so the way past them is to keep the document out of it: the PDF stays on
disk and the model reaches it through pdf-mcp's tools.

pdf-mcp's tools are plain Python functions, so this needs no MCP server
and no subprocess. Measured on a 171-page Form 10-K that Claude Haiku 4.5
refuses outright: two questions, two to three turns each, about $0.008 per
question.

    pip install pdf-mcp anthropic
    export ANTHROPIC_API_KEY=...
    python examples/search_first.py report.pdf "What were 2022 revenues?"
"""

import json
import sys

import anthropic

from pdf_mcp.server import pdf_read_pages, pdf_search

MODEL = "claude-haiku-4-5"

TOOLS = [
    {
        "name": "pdf_search",
        "description": (
            "Search the PDF and return ranked matching pages with excerpts."
            " Use this first to locate relevant pages."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Short, specific search terms.",
                },
                "max_results": {"type": "integer", "default": 3},
            },
            "required": ["query"],
        },
    },
    {
        "name": "pdf_read_pages",
        "description": (
            "Read the full text of specific pages, e.g. '56', '56,73' or"
            " '56-58'. Use when a search excerpt is not enough."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pages": {
                    "type": "string",
                    "description": "Page spec such as '56' or '56-58'.",
                }
            },
            "required": ["pages"],
        },
    },
]

IMPL = {"pdf_search": pdf_search, "pdf_read_pages": pdf_read_pages}


def answer(client, pdf_path, question):
    """Run the tool loop until the model stops asking for tools."""
    messages = [
        {
            "role": "user",
            "content": (
                "The document is available through your tools. Answer using"
                f" them; do not guess.\n\n{question}"
            ),
        }
    ]
    tokens_in = tokens_out = turns = 0

    while True:
        turns += 1
        reply = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            tools=TOOLS,
            messages=messages,
        )
        tokens_in += reply.usage.input_tokens
        tokens_out += reply.usage.output_tokens

        if reply.stop_reason != "tool_use":
            text = "".join(b.text for b in reply.content if b.type == "text")
            print(text.strip())
            break

        messages.append({"role": "assistant", "content": reply.content})
        results = []
        for block in reply.content:
            if block.type != "tool_use":
                continue
            print(
                f"  [turn {turns}] {block.name}({json.dumps(block.input)})",
                file=sys.stderr,
            )
            # The path comes from this script, never from the model.
            out = IMPL[block.name](path=pdf_path, **block.input)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(out, default=str),
                }
            )
        messages.append({"role": "user", "content": results})

    # Haiku 4.5 list price, $1 / $5 per million tokens.
    cost = tokens_in / 1e6 * 1.0 + tokens_out / 1e6 * 5.0
    print(
        f"\n{turns} turns, {tokens_in:,} in / {tokens_out:,} out,"
        f" about ${cost:.5f}",
        file=sys.stderr,
    )


def main():
    if len(sys.argv) < 3:
        sys.exit(f"usage: {sys.argv[0]} <pdf> <question>")
    answer(anthropic.Anthropic(), sys.argv[1], " ".join(sys.argv[2:]))


if __name__ == "__main__":
    main()
