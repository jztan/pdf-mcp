"""Summarise a PDF that is too long to upload.

Search-first does not help here: a summary has no query to match a page
against, and no page is safe to skip. So window the document instead.

Three passes:

  text    walk the document in page windows, take notes on each
  charts  give every page that carries a picture its own look
  merge   fold both sets of notes into one summary

The chart pass is separate on purpose. A page image buried behind 20,000
tokens of prose gets skimmed; the same image on its own gets read. Only
the pages that carry pictures cost image tokens, which on the 171-page
Form 10-K this was measured against is 8 pages of 171.

Cheap model for the prose, strong model for the pictures: transcribing a
bar chart in one pass drifts on a small model even where answering one
question about it does not. Measured total for 171 pages: about $0.53.

    pip install pdf-mcp anthropic
    export ANTHROPIC_API_KEY=...
    python examples/whole_document.py report.pdf
"""

import base64
import sys

import anthropic

from pdf_mcp.server import pdf_read_all, pdf_read_pages, pdf_render_pages

TEXT_MODEL = "claude-haiku-4-5"  # the prose, which is most of the document
CHART_MODEL = "claude-opus-5"  # the handful of pages that are pictures
WINDOW = 25  # pages of text per note-taking call

# List price per million tokens, (input, output).
PRICE = {"claude-haiku-4-5": (1.0, 5.0), "claude-opus-5": (5.0, 25.0)}

client = anthropic.Anthropic()
usage: dict[str, list[int]] = {}


def ask(system, blocks, model=TEXT_MODEL, max_tokens=2000):
    reply = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": blocks}],
    )
    counted = usage.setdefault(model, [0, 0])
    counted[0] += reply.usage.input_tokens
    counted[1] += reply.usage.output_tokens
    return "".join(b.text for b in reply.content if b.type == "text")


def picture_pages(pdf_path, first, last):
    """Pages carrying an image.

    detect_charts finds vector plot geometry, so it returns 0 on charts
    pasted in as flat rasters. image_count catches anything drawn.
    """
    result = pdf_read_pages(path=pdf_path, pages=f"{first}-{last}")
    return [p["page"] for p in result["pages"] if p["image_count"]]


def render(pdf_path, page):
    """One rendered page as an API image block, or None."""
    for block in pdf_render_pages(path=pdf_path, pages=str(page), dpi=150):
        data = getattr(block, "data", None)
        if not data:
            continue
        header = base64.b64decode(data[:12])
        jpeg = header.startswith(b"\xff\xd8\xff")
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg" if jpeg else "image/png",
                "data": data,
            },
        }
    return None


def read_charts(pdf_path, page):
    """One page, one look. Its own call, with nothing else in it."""
    image = render(pdf_path, page)
    if image is None:
        return ""
    return ask(
        "You read charts out of documents.",
        [
            {
                "type": "text",
                "text": (
                    f"Page {page}. For every chart, give its title, then each"
                    " series with its axis labels and every value you can"
                    " read, as one table per chart. If the page has no chart,"
                    " say NO CHART."
                ),
            },
            image,
        ],
        model=CHART_MODEL,
        max_tokens=6000,
    )


def summarise(pdf_path):
    total = pdf_read_all(path=pdf_path, max_pages=1)["total_pages"]
    notes, charts = [], []

    page = 1
    while page <= total:
        last = min(page + WINDOW - 1, total)
        text = pdf_read_all(path=pdf_path, start_page=page, max_pages=WINDOW)[
            "full_text"
        ]
        notes.append(
            ask(
                "You are reading a long document in sections.",
                [
                    {
                        "type": "text",
                        "text": (
                            "Take notes on this section for a summary of the"
                            f" whole document.\n\nPages {page}-{last}:"
                            f"\n\n{text}"
                        ),
                    }
                ],
            )
        )
        for chart_page in picture_pages(pdf_path, page, last):
            reading = read_charts(pdf_path, chart_page)
            # Drops letterheads and logos, which image_count also finds.
            if reading and "NO CHART" not in reading[:120]:
                charts.append(f"Page {chart_page}:\n{reading}")
        print(f"  pages {page:>4}-{last:<4} {len(text):>8,} chars", file=sys.stderr)
        page = last + 1

    print(f"  charts read on {len(charts)} pages", file=sys.stderr)
    body = (
        "SECTION NOTES\n\n"
        + "\n\n".join(notes)
        + "\n\nCHART READINGS\n\n"
        + "\n\n".join(charts)
    )
    return ask(
        "You write briefings from section notes.",
        [
            {
                "type": "text",
                "text": (
                    body + "\n\nSummarise the document. Include any figures that"
                    " appear only in the chart readings."
                ),
            }
        ],
        max_tokens=3000,
    )


def main():
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} <pdf>")
    summary = summarise(sys.argv[1])

    total_cost = 0.0
    for model, (tokens_in, tokens_out) in usage.items():
        per_in, per_out = PRICE[model]
        cost = tokens_in / 1e6 * per_in + tokens_out / 1e6 * per_out
        total_cost += cost
        print(
            f"  {model:<18} {tokens_in:>8,} in {tokens_out:>7,} out" f"  ${cost:.4f}",
            file=sys.stderr,
        )
    print(
        f"  {'TOTAL':<18} {'':>8}    {'':>7}      ${total_cost:.4f}\n", file=sys.stderr
    )
    print(summary)


if __name__ == "__main__":
    main()
