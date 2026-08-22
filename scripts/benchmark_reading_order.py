#!/usr/bin/env python
"""
scripts/benchmark_reading_order.py

Reading-order fidelity benchmark for pdf-mcp's text extraction.

pdf-mcp's `extract_text_from_page` uses a positional sort that interleaves
columns on multi-column PDFs, scrambling the text that feeds search,
excerpts, and embeddings. This benchmark quantifies that on a committed
corpus of arXiv documents (classified by column count) by scoring extracted
text against READoc ground-truth markdown, and reports PyMuPDF4LLM (which
does column-aware extraction) as a reference upper bound.

Corpus: benchmark_data/reading_order_corpus.json — arXiv IDs grouped by
column count. PDFs are fetched on demand from arxiv.org (latest version;
minor version drift vs READoc GT is acceptable for this directional metric)
and cached under benchmark_data/.reading_order_pdfs/ (gitignored). READoc
ground truth comes from the `lazyc/READoc` HuggingFace dataset.

Usage:
    python scripts/benchmark_reading_order.py                 # full run
    python scripts/benchmark_reading_order.py --limit 5       # quick subset
    python scripts/benchmark_reading_order.py --output FILE   # write md table

The PyMuPDF4LLM reference column is skipped automatically if the package is
not installed, so the pdf-mcp baseline is always measurable.
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import io
import json
import os
import re
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

import pymupdf

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from pdf_mcp.extractor import extract_text_from_page  # noqa: E402
from _mcp_client import MCPClient  # noqa: E402

CORPUS = Path(__file__).parent.parent / "benchmark_data" / "reading_order_corpus.json"
PDF_CACHE = Path(__file__).parent.parent / "benchmark_data" / ".reading_order_pdfs"
PAGE_CAP = 6  # first N pages — matches the token window, bounds fetch/OCR cost
TOKEN_CAP = 1500
ORDER_GAIN_MIN = 0.03  # reference must beat p4llm order score by more than this
RECALL_GAP_MAX = 0.02  # ...without out-recalling p4llm by more than this

_LATEX_CMD = re.compile(r"\\[a-zA-Z]+")
_NON_ALNUM = re.compile(r"[^a-z0-9 ]")


def compute_verdict(order_gain: float, recall_gap: float) -> str:
    """Go/no-go verdict for porting an XY-cut, from the two-column deltas.

    order_gain = reference.order_score  - p4llm.order_score
    recall_gap = reference.recall_score - p4llm.recall_score

    PORT-WORTH  : beat p4llm on order without out-recalling it (real ordering win)
    CONFOUNDED  : beat p4llm on order but also out-recalled it (text-layer diff)
    NO-GO       : did not clear the order bar
    """
    if order_gain <= ORDER_GAIN_MIN:
        return "NO-GO"
    if recall_gap > RECALL_GAP_MAX:
        return "CONFOUNDED"
    return "PORT-WORTH"


def fill_reference_args(template: str, path: str, pages: str) -> dict[str, Any]:
    """Substitute {path} and {pages} into a JSON args template and parse it.

    Keeps the reference server's exact call shape out of committed code: the
    operator supplies the template at run time. `pages` is the 1-indexed page
    window (e.g. "1-6") matching PAGE_CAP; a reference that cannot honour it is
    a comparability caveat, not this helper's concern.
    """
    filled = template.replace("{path}", path).replace("{pages}", pages)
    return json.loads(filled)


def normalize_tokens(text: str, cap: int | None = None) -> list[str]:
    """Lowercase, strip LaTeX commands, keep alphanumeric word tokens.

    Reduces text to a comparable word stream so the score reflects reading
    order and content recall rather than markup/formatting differences.
    """
    text = _LATEX_CMD.sub(" ", text)
    text = _NON_ALNUM.sub(" ", text.lower())
    toks = text.split()
    return toks[:cap] if cap is not None else toks


def reading_order_score(pred: str, gt: str) -> float:
    """Sequence similarity of normalized token streams, in [0, 1].

    1.0 = identical order and content; lower as order is scrambled or
    content is lost. Token streams are capped for tractable comparison.
    """
    a = normalize_tokens(pred, cap=TOKEN_CAP)
    b = normalize_tokens(gt, cap=TOKEN_CAP)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def recall_score(pred: str, gt: str) -> float:
    """Order-insensitive token recall in [0, 1]: multiset overlap of normalized
    tokens vs ground truth.

    Isolates content recovery from ordering. Paired with reading_order_score, a
    reading-order win can be told apart from a text-layer extraction difference:
    a higher order score with an equal recall score is a genuine ordering win.
    """
    a = Counter(normalize_tokens(pred, cap=TOKEN_CAP))
    b = Counter(normalize_tokens(gt, cap=TOKEN_CAP))
    if not a or not b:
        return 0.0
    overlap = sum((a & b).values())
    return overlap / sum(b.values())


def classify_columns(doc: pymupdf.Document) -> int:
    """Heuristic column count (1 or 2) from text-block x-positions.

    Looks at the first 3 pages: if a meaningful share of blocks begin in the
    right half of the page, the layout is two-column.
    """
    right = total = 0
    for page in list(doc)[:3]:
        width = page.rect.width
        for block in page.get_text("blocks"):
            if not block[4].strip():
                continue
            total += 1
            if block[0] > 0.55 * width:
                right += 1
    return 2 if total and right / total > 0.18 else 1


def _fetch_pdf(arxiv_id: str) -> Path | None:
    PDF_CACHE.mkdir(parents=True, exist_ok=True)
    pdf = PDF_CACHE / f"{arxiv_id}.pdf"
    if pdf.exists():
        return pdf
    try:
        req = urllib.request.Request(
            f"https://arxiv.org/pdf/{arxiv_id}",
            headers={"User-Agent": "Mozilla/5.0 (pdf-mcp reading-order benchmark)"},
        )
        pdf.write_bytes(urllib.request.urlopen(req, timeout=30).read())
        time.sleep(1.2)  # be polite to arxiv.org
        return pdf
    except Exception:
        return None


def _load_gt(arxiv_id: str) -> str | None:
    try:
        from huggingface_hub import hf_hub_download

        path = hf_hub_download(
            "lazyc/READoc",
            f"arxiv_ground_truth/{arxiv_id}.md",
            repo_type="dataset",
        )
        return Path(path).read_text(encoding="utf-8")
    except Exception:
        return None


def _pdfmcp_text(pdf: Path) -> str:
    """Extract via the column-aware path.

    PDF_MCP_TEXT_BACKEND=pdfium routes the same path through the
    permissive backend, so both engines are scored against the SAME
    ground truth in one run rather than across two runs whose corpora
    could silently differ.
    """
    if os.environ.get("PDF_MCP_TEXT_BACKEND") == "pdfium":
        from pdf_mcp.backend.text import open_text_page

        import pypdfium2 as _pdfium

        doc = _pdfium.PdfDocument(str(pdf))
        n = min(PAGE_CAP, len(doc))
        doc.close()
        return "\n".join(
            extract_text_from_page(open_text_page(str(pdf), i)) for i in range(n)
        )

    doc = pymupdf.open(pdf)
    try:
        n = min(PAGE_CAP, doc.page_count)
        return "\n".join(extract_text_from_page(doc[i]) for i in range(n))
    finally:
        doc.close()


def _p4llm_text(pdf: Path) -> str | None:
    try:
        import pymupdf4llm
    except ImportError:
        return None
    doc = pymupdf.open(pdf)
    pages = list(range(min(PAGE_CAP, doc.page_count)))
    doc.close()
    with (
        contextlib.redirect_stdout(io.StringIO()),
        contextlib.redirect_stderr(io.StringIO()),
    ):
        return pymupdf4llm.to_markdown(str(pdf), pages=pages, show_progress=False)


def _reference_text(
    client: MCPClient, pdf: Path, tool: str, args_template: str
) -> str | None:
    """Reading-ordered text for the first PAGE_CAP pages from a reference MCP
    server, or None on any failure (fail-open, like _p4llm_text)."""
    try:
        args = fill_reference_args(args_template, path=str(pdf), pages=f"1-{PAGE_CAP}")
        result = client.call_tool(tool, args)
        if result.get("isError"):
            return None
        parts = [
            block["text"]
            for block in result.get("content", [])
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        text = "\n".join(parts)
        return text or None
    except Exception:
        return None


def _fmt(value: float | None) -> str:
    return "%.3f" % value if value is not None else "n/a"


def _mean(rows: list[dict[str, Any]], key: str) -> float | None:
    vals = [r[key] for r in rows if r[key] is not None]
    return sum(vals) / len(vals) if vals else None


def run(
    limit: int | None = None,
    reference_cmd: list[str] | None = None,
    reference_tool: str | None = None,
    reference_args: str | None = None,
) -> dict[str, Any]:
    """Run the benchmark; return per-doc rows and per-group aggregates.

    When reference_cmd/tool/args are all provided, an external reading-order
    reference is spawned once and scored alongside the two built-in extractors.
    """
    corpus = json.loads(CORPUS.read_text())
    use_ref = bool(reference_cmd and reference_tool and reference_args)
    client = None
    if use_ref:
        try:
            client = MCPClient(reference_cmd)
            client.initialize()
        except Exception as exc:  # server failed to start -> drop the column
            print(f"  reference unavailable: {exc}", file=sys.stderr)
            client = None
            use_ref = False

    rows = []
    try:
        for group, ids in corpus.items():
            for arxiv_id in ids[:limit] if limit else ids:
                pdf = _fetch_pdf(arxiv_id)
                gt = _load_gt(arxiv_id)
                if pdf is None or gt is None:
                    print(f"  skip {arxiv_id}: fetch/GT unavailable", file=sys.stderr)
                    continue
                mc_text = _pdfmcp_text(pdf)
                ref_text = _p4llm_text(pdf)
                row = {
                    "id": arxiv_id,
                    "group": group,
                    "pdfmcp_order": reading_order_score(mc_text, gt),
                    "pdfmcp_recall": recall_score(mc_text, gt),
                    "p4llm_order": (
                        reading_order_score(ref_text, gt) if ref_text else None
                    ),
                    "p4llm_recall": recall_score(ref_text, gt) if ref_text else None,
                    "ref_order": None,
                    "ref_recall": None,
                }
                if use_ref and client is not None:
                    rt = _reference_text(client, pdf, reference_tool, reference_args)
                    if rt:
                        row["ref_order"] = reading_order_score(rt, gt)
                        row["ref_recall"] = recall_score(rt, gt)
                rows.append(row)
                print(
                    f"  {group:11} {arxiv_id:12} "
                    f"pdfmcp={row['pdfmcp_order']:.3f} "
                    f"p4llm={_fmt(row['p4llm_order'])} "
                    f"ref={_fmt(row['ref_order'])}",
                    file=sys.stderr,
                )
    finally:
        if client is not None:
            client.close()

    aggregates = {}
    for group in corpus:
        sub = [r for r in rows if r["group"] == group]
        if not sub:
            continue
        aggregates[group] = {
            "n": len(sub),
            "pdfmcp_order": _mean(sub, "pdfmcp_order"),
            "pdfmcp_recall": _mean(sub, "pdfmcp_recall"),
            "p4llm_order": _mean(sub, "p4llm_order"),
            "p4llm_recall": _mean(sub, "p4llm_recall"),
            "ref_order": _mean(sub, "ref_order"),
            "ref_recall": _mean(sub, "ref_recall"),
        }
    return {"rows": rows, "aggregates": aggregates, "used_reference": use_ref}


def format_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Reading-order fidelity benchmark",
        "",
        "Order score = sequence similarity of normalized token streams vs "
        "READoc ground truth (order-sensitive). Recall = order-insensitive "
        "token overlap vs the same ground truth. `pdfmcp` = current "
        "`extract_text_from_page`; `p4llm` = PyMuPDF4LLM column-aware path we "
        "ship; `reference` = external XY-cut reference (when provided).",
        "",
        "## Aggregates",
        "",
        "| group | n | pdfmcp_order | p4llm_order | ref_order "
        "| pdfmcp_recall | p4llm_recall | ref_recall |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for group, a in result["aggregates"].items():
        lines.append(
            f"| {group} | {a['n']} | {_fmt(a['pdfmcp_order'])} "
            f"| {_fmt(a['p4llm_order'])} | {_fmt(a['ref_order'])} "
            f"| {_fmt(a['pdfmcp_recall'])} | {_fmt(a['p4llm_recall'])} "
            f"| {_fmt(a['ref_recall'])} |"
        )
    lines += [
        "",
        "## Per-document",
        "",
        "| id | group | pdfmcp_order | p4llm_order | ref_order |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in result["rows"]:
        lines.append(
            f"| {r['id']} | {r['group']} | {_fmt(r['pdfmcp_order'])} "
            f"| {_fmt(r['p4llm_order'])} | {_fmt(r['ref_order'])} |"
        )

    if result.get("used_reference"):
        tc = result["aggregates"].get("two_column")
        if tc and tc["ref_order"] is not None and tc["p4llm_order"] is not None:
            order_gain = tc["ref_order"] - tc["p4llm_order"]
            recall_gap = (tc["ref_recall"] or 0.0) - (tc["p4llm_recall"] or 0.0)
            verdict = compute_verdict(order_gain, recall_gap)
            lines += [
                "",
                "## Verdict (two_column)",
                "",
                f"- order_gain (reference - p4llm) = {order_gain:+.3f}",
                f"- recall_gap (reference - p4llm) = {recall_gap:+.3f}",
                f"- **{verdict}**",
            ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="docs per group")
    parser.add_argument("--output", type=str, default=None, help="write md table")
    parser.add_argument(
        "--reference-cmd",
        type=str,
        default=None,
        help="launch command for an external reading-order MCP server "
        "(e.g. 'npx -y <server>'); omitted -> no reference column",
    )
    parser.add_argument(
        "--reference-tool",
        type=str,
        default=None,
        help="tool name to call on the reference server",
    )
    parser.add_argument(
        "--reference-args",
        type=str,
        default=None,
        help="JSON args template with {path} and {pages} placeholders, e.g. "
        '\'{"sources":[{"path":"{path}","pages":"{pages}"}],"full":true}\'',
    )
    args = parser.parse_args()

    result = run(
        limit=args.limit,
        reference_cmd=args.reference_cmd.split() if args.reference_cmd else None,
        reference_tool=args.reference_tool,
        reference_args=args.reference_args,
    )
    md = format_markdown(result)
    print(md)
    if args.output:
        Path(args.output).write_text(md, encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
