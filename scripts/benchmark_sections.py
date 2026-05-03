#!/usr/bin/env python
"""
scripts/benchmark_sections.py

Benchmark: section-granularity vs page-granularity for pdf_search.

Three groups:
  1. Boundary precision  - is the detector finding section starts?
  2. Completeness        - does section-mode return more of the gold section?
  3. Tool-call simulation - how many extra reads does the agent need?

Usage:
    python scripts/benchmark_sections.py                     # validation gate (PDFs 1+2)
    python scripts/benchmark_sections.py --include-blog-pdf  # also run the GPT-3 PDF
    python scripts/benchmark_sections.py --calibrate         # print numbers, no gating
    python scripts/benchmark_sections.py --groups 1,2        # run a subset

Exit codes: 0 = PASS, 1 = FAIL, 2 = setup error.
"""

from __future__ import annotations

import argparse
import json
import pymupdf
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# ---- Threshold constants (placeholders — calibrate before relying on them) ----
THRESHOLD_BOUNDARY_F1 = 0.80  # Group 1, per PDF
THRESHOLD_SECTION_RECALL_MEAN = 0.90  # Group 2
THRESHOLD_SECTION_PRECISION_MEAN = 0.85  # Group 2
THRESHOLD_RECALL_DELTA_MEAN = 0.50  # Group 2 (section - page)
THRESHOLD_FRACTION_ZERO_EXTRA_READS = 0.90  # Group 3
SECTION_MIN_CHARS = 1000  # Group 2/3 size filter
BOUNDARY_TOLERANCE_PAGES = 1  # Group 1 ±N tolerance
BOILERPLATE_LINE_FREQUENCY_THRESHOLD = 0.5  # Strip lines on >=50% of pages
COVERAGE_TARGET_FRACTION = 0.95  # Group 3 stop condition
MAX_EXTRA_READS = 10  # Group 3 cap

# ---- PDFs ----
PDFS_VALIDATION = [
    {
        "key": "gnn_review",
        "title": "Graph Neural Networks: A Review of Methods and Applications",
        "url": "https://arxiv.org/pdf/1812.08434",
    },
    {
        "key": "llm_survey",
        "title": "A Survey of Large Language Models",
        "url": "https://arxiv.org/pdf/2303.18223",
    },
]
PDF_BLOG_EXTRA = {
    "key": "gpt3",
    "title": "Language Models are Few-Shot Learners",
    "url": "https://arxiv.org/pdf/2005.14165",
}


# ---- Core dataclass ----
@dataclass
class Section:
    title: str
    start_page: int  # 1-indexed
    end_page: int  # 1-indexed, inclusive
    text: str = ""  # full concatenated text of all pages in [start_page, end_page]


# ---- Printing / output buffering (mirrors benchmark_rrf.py) ----
_OUTPUT: list[str] = []
_IS_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _IS_TTY else text


def green(t: str) -> str:
    return _c("32", t)


def red(t: str) -> str:
    return _c("31", t)


def yellow(t: str) -> str:
    return _c("33", t)


def cyan(t: str) -> str:
    return _c("36", t)


def bold(t: str) -> str:
    return _c("1", t)


def _p(text: str = "") -> None:
    _OUTPUT.append(text)
    print(text)


def _section(title: str) -> None:
    width = 68
    _p()
    _p(bold(cyan("=" * width)))
    _p(bold(cyan(f"  {title}")))
    _p(bold(cyan("=" * width)))


def _row(label: str, value: str, ok: bool | None = None) -> None:
    marker = ""
    if ok is True:
        marker = green(" ✓")
    elif ok is False:
        marker = red(" ✗")
    _p(f"  {label:<44} {value}{marker}")


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _toc_entries_to_sections(
    toc: list[tuple[int, str, int]] | list[list[Any]],
    total_pages: int,
) -> list[Section]:
    """
    Convert PyMuPDF's get_toc() output into Sections with derived end_page.

    end_page for entry i at level N = (start_page of next entry j>i with
    level_j <= N) - 1, or total_pages if no such j exists.

    Args:
        toc: list of (level, title, start_page) entries (1-indexed start_page)
        total_pages: total pages in the document (1-indexed last page)

    Returns:
        list[Section] with text="" — caller fills text via PDF I/O.

    Raises:
        ValueError: if toc is empty (TOC-derived ground truth is required).
    """
    if not toc:
        raise ValueError("Cannot extract sections from empty TOC")

    sections: list[Section] = []
    for i, entry in enumerate(toc):
        level, title, start = entry[0], entry[1], entry[2]
        end = total_pages
        for j in range(i + 1, len(toc)):
            next_level, _, next_start = toc[j][0], toc[j][1], toc[j][2]
            if next_level <= level:
                end = next_start - 1
                break
        sections.append(Section(title=title, start_page=start, end_page=end, text=""))
    return sections


def _extract_toc_boundaries(pdf_path: str) -> list[Section]:
    """
    Open the PDF, derive sections from its TOC, and concatenate page text
    for each section into Section.text.

    Raises ValueError on empty TOC (from `_toc_entries_to_sections`). Caller is
    expected to convert this to exit code 2 at the CLI boundary.
    """
    doc = pymupdf.open(pdf_path)
    try:
        toc = doc.get_toc()
        sections = _toc_entries_to_sections(toc, total_pages=len(doc))
        for s in sections:
            if s.start_page > s.end_page:
                # Malformed (e.g. consecutive entries on same page). Leave text empty.
                continue
            pages_text = []
            for p in range(s.start_page - 1, s.end_page):  # 0-indexed for PyMuPDF
                pages_text.append(doc[p].get_text())
            s.text = "\n".join(pages_text)
        return sections
    finally:
        doc.close()
