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
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import pymupdf

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pdf_mcp.extractor import extract_text_from_page  # noqa: E402

CORPUS = Path(__file__).parent.parent / "benchmark_data" / "reading_order_corpus.json"
PDF_CACHE = Path(__file__).parent.parent / "benchmark_data" / ".reading_order_pdfs"
PAGE_CAP = 6  # first N pages — matches the token window, bounds fetch/OCR cost
TOKEN_CAP = 1500

_LATEX_CMD = re.compile(r"\\[a-zA-Z]+")
_NON_ALNUM = re.compile(r"[^a-z0-9 ]")


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


def run(limit: int | None = None) -> dict[str, Any]:
    """Run the benchmark; return per-doc rows and per-group aggregates."""
    corpus = json.loads(CORPUS.read_text())
    rows = []
    for group, ids in corpus.items():
        for arxiv_id in ids[:limit] if limit else ids:
            pdf = _fetch_pdf(arxiv_id)
            gt = _load_gt(arxiv_id)
            if pdf is None or gt is None:
                print(f"  skip {arxiv_id}: fetch/GT unavailable", file=sys.stderr)
                continue
            pdfmcp = reading_order_score(_pdfmcp_text(pdf), gt)
            ref_text = _p4llm_text(pdf)
            ref = reading_order_score(ref_text, gt) if ref_text else None
            rows.append(
                {"id": arxiv_id, "group": group, "pdfmcp": pdfmcp, "p4llm_ref": ref}
            )
            print(
                f"  {group:11} {arxiv_id:12} pdfmcp={pdfmcp:.3f} "
                f"ref={'%.3f' % ref if ref is not None else 'n/a'}",
                file=sys.stderr,
            )

    aggregates = {}
    for group in corpus:
        sub = [r for r in rows if r["group"] == group]
        if not sub:
            continue
        mc = sum(r["pdfmcp"] for r in sub) / len(sub)
        refs = [r["p4llm_ref"] for r in sub if r["p4llm_ref"] is not None]
        mr = sum(refs) / len(refs) if refs else None
        aggregates[group] = {
            "n": len(sub),
            "pdfmcp": mc,
            "p4llm_ref": mr,
            "delta": (mr - mc) if mr is not None else None,
        }
    return {"rows": rows, "aggregates": aggregates}


def format_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Reading-order fidelity benchmark",
        "",
        "Score = sequence similarity of normalized token streams vs READoc "
        "ground truth (higher is better, max 1.0). `pdfmcp` = current "
        "`extract_text_from_page`; `p4llm_ref` = PyMuPDF4LLM column-aware "
        "reference (upper bound).",
        "",
        "## Aggregates",
        "",
        "| group | n | pdfmcp | p4llm_ref | delta |",
        "| --- | --- | --- | --- | --- |",
    ]
    for group, a in result["aggregates"].items():
        ref = "%.3f" % a["p4llm_ref"] if a["p4llm_ref"] is not None else "n/a"
        delta = "%+.3f" % a["delta"] if a["delta"] is not None else "n/a"
        lines.append(f"| {group} | {a['n']} | {a['pdfmcp']:.3f} | {ref} | {delta} |")
    lines += [
        "",
        "## Per-document",
        "",
        "| id | group | pdfmcp | p4llm_ref |",
        "| --- | --- | --- | --- |",
    ]
    for r in result["rows"]:
        ref = "%.3f" % r["p4llm_ref"] if r["p4llm_ref"] is not None else "n/a"
        lines.append(f"| {r['id']} | {r['group']} | {r['pdfmcp']:.3f} | {ref} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="docs per group")
    parser.add_argument("--output", type=str, default=None, help="write md table")
    args = parser.parse_args()

    result = run(limit=args.limit)
    md = format_markdown(result)
    print(md)
    if args.output:
        Path(args.output).write_text(md, encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
