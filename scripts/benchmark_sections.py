#!/usr/bin/env python
"""
scripts/benchmark_sections.py

Benchmark: section-granularity vs page-granularity for pdf_search.

Three groups:
  1. Boundary precision  - is the detector finding section starts?
  2. Completeness        - does section-mode return more of the gold section?
  3. Tool-call simulation - how many extra reads does the agent need?

Usage:
    python scripts/benchmark_sections.py          # validation gate (PDFs 1+2)
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
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pdf_mcp.server as server_module  # noqa: E402
from pdf_mcp.cache import PDFCache  # noqa: E402
from pdf_mcp.server import _resolve_path  # noqa: E402
from pdf_mcp.server import pdf_search as _PDF_SEARCH_FN  # noqa: E402

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


def _compute_boundary_f1(
    gold: list[Section],
    detected: list[Section],
    tolerance: int = BOUNDARY_TOLERANCE_PAGES,
) -> dict:
    """
    Precision/Recall/F1 on section-start pages, with ±tolerance page slack.

    Both sides are deduplicated to a set of distinct start pages (a PDF with
    multiple TOC entries on the same page contributes one gold boundary).

    A detected start D is a true positive if min(|D - g|) <= tolerance for
    some g in gold; recall is symmetric (a gold start is matched if any
    detected start is within tolerance).
    """
    gold_pages = {s.start_page for s in gold}
    det_pages = {s.start_page for s in detected}

    if not gold_pages:
        return {
            "precision": 0.0,
            "recall": 0.0,
            "f1": 0.0,
            "tp": 0,
            "fp": len(det_pages),
            "fn": 0,
            "n_gold": 0,
            "n_detected": len(det_pages),
        }

    tp_detected = sum(
        1 for d in det_pages if any(abs(d - g) <= tolerance for g in gold_pages)
    )
    matched_gold = sum(
        1 for g in gold_pages if any(abs(d - g) <= tolerance for d in det_pages)
    )

    n_gold = len(gold_pages)
    n_det = len(det_pages)
    precision = tp_detected / n_det if n_det else 0.0
    recall = matched_gold / n_gold

    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )

    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "tp": tp_detected,
        "fp": n_det - tp_detected,
        "fn": n_gold - matched_gold,
        "n_gold": n_gold,
        "n_detected": n_det,
    }


_PAGE_NUMBER_RE = re.compile(
    r"^\s*(page\s+)?\d+(\s*(of|/)\s*\d+)?\s*$",
    re.IGNORECASE,
)


def _detect_boilerplate(
    page_texts: list[str],
    threshold: float = BOILERPLATE_LINE_FREQUENCY_THRESHOLD,
) -> set[str]:
    """
    Build a set of lines that appear on >= threshold fraction of pages.

    Two passes:
      1. Exact-match path — lines repeated verbatim (running titles, footers).
      2. Page-number family — lines matching `_PAGE_NUMBER_RE` (e.g. "Page 1
         of 144", bare "5") are collapsed into one family; if the family
         appears on >= threshold pages, all its raw forms join boilerplate.

    Whitespace is stripped before counting so trailing-space variants dedupe.
    """
    if not page_texts:
        return set()

    counter: Counter[str] = Counter()
    page_number_lines: set[str] = set()
    pages_with_page_number = 0

    for page in page_texts:
        unique_lines = {line.strip() for line in page.splitlines() if line.strip()}
        page_has_pagenum = False
        for line in unique_lines:
            if _PAGE_NUMBER_RE.match(line):
                page_number_lines.add(line)
                page_has_pagenum = True
            else:
                counter[line] += 1
        if page_has_pagenum:
            pages_with_page_number += 1

    n_pages = len(page_texts)
    cutoff = threshold * n_pages
    boilerplate = {line for line, count in counter.items() if count >= cutoff}
    if pages_with_page_number >= cutoff:
        boilerplate |= page_number_lines
    return boilerplate


def _strip_boilerplate(text: str, boilerplate: set[str]) -> str:
    """Remove any line whose stripped form is in `boilerplate`."""
    if not boilerplate:
        return text
    kept = [line for line in text.splitlines() if line.strip() not in boilerplate]
    return "\n".join(kept)


# ---- Tokenization and n-gram coverage metrics ----

_WORD_PUNCT_RE = re.compile(r"[^\w\s\-]")  # keep word chars + hyphens


def _tokenize(text: str) -> list[str]:
    """Lowercase, strip non-word punctuation (keep hyphens for tokens like GPT-3),
    split on whitespace."""
    cleaned = _WORD_PUNCT_RE.sub(" ", text.lower())
    return cleaned.split()


def _ngram_set(tokens: list[str], n: int = 5) -> set[tuple[str, ...]]:
    """Set of contiguous n-grams. Returns empty set if len(tokens) < n."""
    if len(tokens) < n:
        return set()
    return {tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)}  # noqa: E203


def _coverage_metrics(returned: str, gold: str, n: int = 5) -> dict:
    """
    Recall  = |grams_returned ∩ grams_gold| / |grams_gold|
    Precision = |grams_returned ∩ grams_gold| / |grams_returned|

    Returns 0.0 for either metric when its denominator is empty.
    """
    g_returned = _ngram_set(_tokenize(returned), n)
    g_gold = _ngram_set(_tokenize(gold), n)
    inter = g_returned & g_gold
    recall = len(inter) / len(g_gold) if g_gold else 0.0
    precision = len(inter) / len(g_returned) if g_returned else 0.0
    return {"recall": recall, "precision": precision, "intersection": len(inter)}


def _walk_order(initial: int, doc_total: int) -> list[int]:
    """Yield N+1, N-1, N+2, N-2, ... clipped to [1, doc_total], in order,
    until exhausted."""
    order: list[int] = []
    delta = 1
    while True:
        forward = initial + delta
        backward = initial - delta
        added = False
        if 1 <= forward <= doc_total:
            order.append(forward)
            added = True
        if 1 <= backward <= doc_total:
            order.append(backward)
            added = True
        if not added:
            return order
        delta += 1


def _token_coverage(returned: str, gold: str) -> float:
    """
    Fraction of unique gold tokens present in returned text. Used by the
    agent-read simulation in place of 5-gram recall to avoid the
    cross-page-boundary artefact: when reads concatenate non-adjacent
    pages, gold's contiguous 5-grams across page joins are unrecoverable
    and recall under-counts even when all tokens are present. Token-level
    coverage is order-independent and answers the simulation's actual
    question — "did the agent read the section's content".
    """
    g_returned = set(_tokenize(returned))
    g_gold = set(_tokenize(gold))
    if not g_gold:
        return 0.0
    return len(g_gold & g_returned) / len(g_gold)


def _simulate_agent_reads(
    initial_page: int,
    gold_section: Section,
    get_page: Callable[[int], str],
    doc_total_pages: int,
    coverage_target: float = COVERAGE_TARGET_FRACTION,
    max_extra: int = MAX_EXTRA_READS,
) -> int:
    """
    Simulate page-mode agent walking outward from a search hit until token
    coverage >= target or max_extra additional reads have been issued.
    Out-of-range pages are skipped without counting toward the cap.

    Returns the number of additional pdf_read_pages calls beyond the initial hit.
    """
    accumulated_text = get_page(initial_page)
    if _token_coverage(accumulated_text, gold_section.text) >= coverage_target:
        return 0

    extra_reads = 0
    for page in _walk_order(initial_page, doc_total_pages):
        if extra_reads >= max_extra:
            break
        accumulated_text = accumulated_text + "\n" + get_page(page)
        extra_reads += 1
        if _token_coverage(accumulated_text, gold_section.text) >= coverage_target:
            break
    return extra_reads


# Heading regex per spec sketch:
#   - Numbered sections: "1", "1.1", "3.2.1" followed by whitespace and an
#     UPPERCASE letter (so "1km" and "100 widgets total" don't fire — section
#     titles in academic PDFs are Title Case or ALL CAPS, while prose is not).
#   - Chapter/Section/Part keyword followed by a number (case-insensitive
#     for the keyword via a localized flag, so the [A-Z] guard above is not
#     defeated by a global IGNORECASE).
#   - Standalone academic headings (Abstract, References, Acknowledg(e)ments,
#     Bibliography) anchored to whole-line so body words don't match. Added
#     after first calibration showed the detector under-fires by missing
#     unnumbered top-level sections.
#   - "Appendix A", "Appendix B Title", etc. — uppercase letter follows the
#     keyword, so common prose like "the appendix discusses" can't match.
_HEADING_RE = re.compile(
    r"^(?:"
    r"\d+(?:\.\d+)*\s+[A-Z]"
    r"|(?i:Chapter|Section|Part)\s+\d+"
    r"|(?i:Abstract|References|Acknowledgements?|Acknowledgments?|Bibliography)\s*$"
    r"|Appendix\s+[A-Z]"
    r")",
)


def _detect_boundaries_from_lines(
    lines: list[tuple[int, str]],
    total_pages: int,
) -> list[Section]:
    """
    Apply the heading regex to a flat list of (page, line_text) tuples and
    derive Sections. Pure function — no PDF I/O; tests inject lines directly.
    """
    candidates: list[tuple[int, str]] = []
    for page, text in lines:
        stripped = text.strip()
        if not stripped:
            continue
        if _HEADING_RE.match(stripped):
            candidates.append((page, stripped))

    sections: list[Section] = []
    for i, (page, title) in enumerate(candidates):
        if i + 1 < len(candidates):
            end_page = candidates[i + 1][0] - 1
        else:
            end_page = total_pages
        sections.append(
            Section(title=title, start_page=page, end_page=end_page, text="")
        )
    return sections


def _detect_boundaries(pdf_path: str) -> list[Section]:
    """
    PDF-aware wrapper: extract text lines from each page, apply the regex
    detector, and fill `Section.text` with concatenated page text for the
    detected page range.

    NOTE: This is the in-script implementation. If the benchmark passes
    its calibration thresholds, this function (or its descendant) is the
    body that should be promoted to `pdf_mcp/section_detector.py` when
    the feature is upstreamed.
    """
    doc = pymupdf.open(pdf_path)
    try:
        lines: list[tuple[int, str]] = []
        for page_idx in range(len(doc)):
            # `get_text("blocks", sort=True)` returns blocks in
            # top-to-bottom, left-to-right order — critical for two-column
            # PDFs where plain get_text() can interleave columns. Mirrors
            # the pattern used in pdf_mcp/extractor.py:127.
            blocks = doc[page_idx].get_text("blocks", sort=True)
            for block in blocks:
                block_text = block[
                    4
                ]  # PyMuPDF block tuple: (x0,y0,x1,y1,text,block_no,block_type)
                for line in block_text.splitlines():
                    lines.append((page_idx + 1, line))
        sections = _detect_boundaries_from_lines(lines, total_pages=len(doc))
        for s in sections:
            if s.start_page > s.end_page:
                continue
            pages_text = []
            for p in range(s.start_page - 1, s.end_page):
                pages_text.append(doc[p].get_text())
            s.text = "\n".join(pages_text)
        return sections
    finally:
        doc.close()


def run_boundary_group(pdfs: list[dict]) -> dict:
    """
    Group 1: Boundary precision per PDF.

    For each PDF: derive gold sections from TOC, run the detector, compute F1.
    Returns {"per_pdf": {key: {precision, recall, f1, ...}}, "min_f1": float}
    """
    _section("Group 1: Boundary Precision")
    per_pdf: dict[str, dict] = {}

    for pdf in pdfs:
        _p()
        _p(f"  PDF: {bold(pdf['title'])}")
        # Let ValueError (empty TOC) propagate — main() converts it to exit 2.
        gold = _extract_toc_boundaries(pdf["_local_path"])
        detected = _detect_boundaries(pdf["_local_path"])

        metrics = _compute_boundary_f1(
            gold, detected, tolerance=BOUNDARY_TOLERANCE_PAGES
        )
        per_pdf[pdf["key"]] = metrics

        _row("Gold boundaries (deduped pages)", str(metrics["n_gold"]))
        _row("Detected boundaries (deduped)", str(metrics["n_detected"]))
        _row("True positives (±1 tolerance)", str(metrics["tp"]))
        _row("Precision", f"{metrics['precision']:.3f}")
        _row("Recall", f"{metrics['recall']:.3f}")
        _row(
            "F1",
            f"{metrics['f1']:.3f}",
            ok=metrics["f1"] >= THRESHOLD_BOUNDARY_F1,
        )

    f1_values = [m["f1"] for m in per_pdf.values() if "error" not in m]
    min_f1 = min(f1_values) if f1_values else 0.0
    _p()
    _row("min F1 across PDFs", f"{min_f1:.3f}", ok=min_f1 >= THRESHOLD_BOUNDARY_F1)
    return {"per_pdf": per_pdf, "min_f1": min_f1}


def _get_page_text(pdf_path: str, page: int) -> str:
    """1-indexed page text accessor (opens doc each call;
    cheap because tempdir cache is hot)."""
    doc = pymupdf.open(pdf_path)
    try:
        return doc[page - 1].get_text()
    finally:
        doc.close()


def _keyword_page_search(pdf_path: str, query: str, top_k: int = 1) -> dict:
    """Page-mode baseline: keyword search via pdf_search(mode='keyword')."""
    return _PDF_SEARCH_FN(pdf_path, query, mode="keyword", max_results=top_k)


def _doc_total_pages(pdf_path: str) -> int:
    doc = pymupdf.open(pdf_path)
    try:
        return len(doc)
    finally:
        doc.close()


def _section_search(
    pdf_path: str,
    query: str,
    sections: list[Section],
    top_k: int = 1,
) -> dict:
    """
    In-script section-granularity search. Runs the existing keyword search,
    maps each rank-ordered page hit to the section containing it, and
    returns the first `top_k` distinct sections (preserving rank order).

    This is the benchmark's stand-in for `pdf_search(granularity="section")`.
    If the benchmark passes, this is the surface area to upstream — likely
    re-implemented internally with section-aware ranking rather than this
    page-hit-then-lookup approach.
    """
    # Pull more keyword hits than top_k since multiple hits can fall in one
    # section; we want top_k *distinct* sections.
    raw = _PDF_SEARCH_FN(pdf_path, query, mode="keyword", max_results=top_k * 5)
    matches = raw.get("matches", [])

    seen_titles: set[str] = set()
    out: list[dict] = []
    for m in matches:
        page = m.get("page")
        for sec in sections:
            if sec.start_page <= page <= sec.end_page and sec.title not in seen_titles:
                seen_titles.add(sec.title)
                out.append(
                    {
                        "title": sec.title,
                        "start_page": sec.start_page,
                        "end_page": sec.end_page,
                        "text": sec.text,
                    }
                )
                break
        if len(out) >= top_k:
            break
    return {"sections": out}


def run_completeness_group(pdfs: list[dict]) -> dict:
    """
    Group 2: For each gold section >= SECTION_MIN_CHARS, query its title under both
    granularities and compare 5-gram recall + precision against the gold section text.
    """
    _section("Group 2: Completeness")
    per_pdf: dict[str, dict] = {}
    all_section_recalls: list[float] = []
    all_section_precisions: list[float] = []
    all_recall_deltas: list[float] = []

    for pdf in pdfs:
        _p()
        _p(f"  PDF: {bold(pdf['title'])}")
        path = pdf["_local_path"]
        # Let ValueError (empty TOC) propagate — main() converts it to exit 2.
        gold_sections = _extract_toc_boundaries(path)
        # Detected sections are the index for the section-search shim.
        detected_sections = _detect_boundaries(path)

        # Pre-build the boilerplate set once per PDF
        page_texts = [
            _get_page_text(path, p) for p in range(1, _doc_total_pages(path) + 1)
        ]
        boilerplate = _detect_boilerplate(page_texts)

        eligible = [s for s in gold_sections if len(s.text) >= SECTION_MIN_CHARS]
        sec_results: list[dict] = []
        for sec in eligible:
            page_hit = _keyword_page_search(path, sec.title, top_k=1)
            page_matches = page_hit.get("matches", [])
            if not page_matches:
                continue
            page_chunk = _strip_boilerplate(
                _get_page_text(path, page_matches[0]["page"]), boilerplate
            )
            section_resp = _section_search(
                path, sec.title, sections=detected_sections, top_k=1
            )
            section_chunks = section_resp.get("sections", [])
            if not section_chunks:
                continue
            section_chunk = _strip_boilerplate(section_chunks[0]["text"], boilerplate)

            gold_clean = _strip_boilerplate(sec.text, boilerplate)
            page_metrics = _coverage_metrics(page_chunk, gold_clean)
            section_metrics = _coverage_metrics(section_chunk, gold_clean)
            sec_results.append(
                {
                    "title": sec.title,
                    "start_page": sec.start_page,
                    "end_page": sec.end_page,
                    "char_count": len(sec.text),
                    "page_mode": page_metrics,
                    "section_mode": section_metrics,
                    "recall_delta": section_metrics["recall"] - page_metrics["recall"],
                }
            )

        per_pdf[pdf["key"]] = {"sections": sec_results}
        if sec_results:
            mean_sec_recall = sum(
                r["section_mode"]["recall"] for r in sec_results
            ) / len(sec_results)
            mean_sec_prec = sum(
                r["section_mode"]["precision"] for r in sec_results
            ) / len(sec_results)
            mean_delta = sum(r["recall_delta"] for r in sec_results) / len(sec_results)
            per_pdf[pdf["key"]].update(
                {
                    "mean_section_recall": mean_sec_recall,
                    "mean_section_precision": mean_sec_prec,
                    "mean_recall_delta": mean_delta,
                    "n_sections": len(sec_results),
                }
            )
            _row("Sections evaluated (>=1000 chars)", str(len(sec_results)))
            _row(
                "Mean section-mode recall",
                f"{mean_sec_recall:.3f}",
                ok=mean_sec_recall >= THRESHOLD_SECTION_RECALL_MEAN,
            )
            _row(
                "Mean section-mode precision",
                f"{mean_sec_prec:.3f}",
                ok=mean_sec_prec >= THRESHOLD_SECTION_PRECISION_MEAN,
            )
            _row(
                "Mean recall delta (section - page)",
                f"{mean_delta:.3f}",
                ok=mean_delta >= THRESHOLD_RECALL_DELTA_MEAN,
            )
            all_section_recalls.append(mean_sec_recall)
            all_section_precisions.append(mean_sec_prec)
            all_recall_deltas.append(mean_delta)

    return {
        "per_pdf": per_pdf,
        "min_section_recall": min(all_section_recalls) if all_section_recalls else 0.0,
        "min_section_precision": (
            min(all_section_precisions) if all_section_precisions else 0.0
        ),
        "min_recall_delta": min(all_recall_deltas) if all_recall_deltas else 0.0,
    }


def run_toolcall_group(pdfs: list[dict]) -> dict:
    """
    Group 3: simulate page-mode agent's extra-read cost vs section mode (always 0).
    Reports fraction of sections with 0 extra reads, per mode, per PDF.
    """
    _section("Group 3: Tool Call Simulation")
    per_pdf: dict[str, dict] = {}
    cross_section_zero_fractions: list[float] = []

    for pdf in pdfs:
        _p()
        _p(f"  PDF: {bold(pdf['title'])}")
        path = pdf["_local_path"]
        # Let ValueError (empty TOC) propagate — main() converts it to exit 2.
        gold = _extract_toc_boundaries(path)

        eligible = [s for s in gold if len(s.text) >= SECTION_MIN_CHARS]
        total_pages = _doc_total_pages(path)
        sec_results: list[dict] = []

        for sec in eligible:
            page_hit = _keyword_page_search(path, sec.title, top_k=1)
            matches = page_hit.get("matches", [])
            if not matches:
                continue
            initial = matches[0]["page"]
            page_extra = _simulate_agent_reads(
                initial_page=initial,
                gold_section=sec,
                get_page=lambda p, _path=path: _get_page_text(_path, p),
                doc_total_pages=total_pages,
            )
            sec_results.append(
                {
                    "title": sec.title,
                    "page_mode_extra_reads": page_extra,
                    "section_mode_extra_reads": 0,
                }
            )

        n = len(sec_results)
        page_zero = sum(1 for r in sec_results if r["page_mode_extra_reads"] == 0)
        page_frac = page_zero / n if n else 0.0
        section_frac = 1.0 if n else 0.0
        per_pdf[pdf["key"]] = {
            "sections": sec_results,
            "n_sections": n,
            "page_mode_zero_read_fraction": page_frac,
            "section_mode_zero_read_fraction": section_frac,
            "page_mode_mean_extra_reads": (
                sum(r["page_mode_extra_reads"] for r in sec_results) / n if n else 0.0
            ),
        }
        if n:
            _row("Sections evaluated (>=1000 chars)", str(n))
            _row(
                "Page-mode mean extra reads",
                f"{per_pdf[pdf['key']]['page_mode_mean_extra_reads']:.2f}",
            )
            _row("Page-mode 0-extra-reads fraction", f"{page_frac:.2%}")
            _row(
                "Section-mode 0-extra-reads fraction",
                f"{section_frac:.2%}",
                ok=section_frac >= THRESHOLD_FRACTION_ZERO_EXTRA_READS,
            )
            cross_section_zero_fractions.append(section_frac)

    return {
        "per_pdf": per_pdf,
        "min_section_zero_fraction": (
            min(cross_section_zero_fractions) if cross_section_zero_fractions else 0.0
        ),
    }


def _save_results(results: dict, file_timestamp: str, iso_timestamp: str) -> None:
    out_dir = Path("benchmark_results")
    out_dir.mkdir(exist_ok=True)
    base = out_dir / f"sections_{file_timestamp}"

    txt_content = _strip_ansi("\n".join(_OUTPUT))
    base.with_suffix(".txt").write_text(txt_content, encoding="utf-8")

    payload = {
        "timestamp": iso_timestamp,
        "thresholds": {
            "boundary_f1": THRESHOLD_BOUNDARY_F1,
            "section_recall_mean": THRESHOLD_SECTION_RECALL_MEAN,
            "section_precision_mean": THRESHOLD_SECTION_PRECISION_MEAN,
            "recall_delta_mean": THRESHOLD_RECALL_DELTA_MEAN,
            "fraction_zero_extra_reads": THRESHOLD_FRACTION_ZERO_EXTRA_READS,
        },
        "results": results,
    }
    base.with_suffix(".json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )


def _print_summary(results: dict, calibrate: bool) -> tuple[bool, list[str]]:
    """
    Print final pass/fail table and return (passed, list_of_failures).
    In calibrate mode, every check returns "INFO" and passed=True.
    """
    _section("Summary")
    failures: list[str] = []

    def _check(label: str, value: float, threshold: float, op: str = ">=") -> None:
        if calibrate:
            _row(
                label, f"{value:.3f} (threshold {threshold:.3f}, calibrate-only)", None
            )
            return
        passed = value >= threshold if op == ">=" else value <= threshold
        _row(label, f"{value:.3f} (threshold {threshold:.3f})", ok=passed)
        if not passed:
            failures.append(f"{label}: {value:.3f} < {threshold:.3f}")

    g1 = results.get("group_1")
    if g1:
        for key, m in g1["per_pdf"].items():
            if "error" in m:
                failures.append(f"Group 1 [{key}]: {m['error']}")
                continue
            _check(f"Group 1 [{key}] F1", m["f1"], THRESHOLD_BOUNDARY_F1)

    g2 = results.get("group_2")
    if g2:
        _check(
            "Group 2 min section-mode recall",
            g2.get("min_section_recall", 0.0),
            THRESHOLD_SECTION_RECALL_MEAN,
        )
        _check(
            "Group 2 min section-mode precision",
            g2.get("min_section_precision", 0.0),
            THRESHOLD_SECTION_PRECISION_MEAN,
        )
        _check(
            "Group 2 min recall delta",
            g2.get("min_recall_delta", 0.0),
            THRESHOLD_RECALL_DELTA_MEAN,
        )

    g3 = results.get("group_3")
    if g3:
        _check(
            "Group 3 min section-mode 0-read fraction",
            g3.get("min_section_zero_fraction", 0.0),
            THRESHOLD_FRACTION_ZERO_EXTRA_READS,
        )

    return (len(failures) == 0, failures)


def _resolve_pdf_local_path(url: str) -> str:
    """Wrapper around server._resolve_path (kept separate so tests can stub it)."""
    return _resolve_path(url)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark section-granularity vs page-granularity for pdf_search.",
    )
    parser.add_argument(
        "--groups",
        default="1,2,3",
        help="Comma-separated subset of groups to run (default: 1,2,3)",
    )
    parser.add_argument(
        "--include-blog-pdf",
        action="store_true",
        help=(
            "Append the GPT-3 paper for blog-comparison data "
            "(not run by validation gate)"
        ),
    )
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help=(
            "Print achieved numbers but never enforce thresholds "
            "(always exits 0 unless setup error)"
        ),
    )
    args = parser.parse_args(argv)

    selected_groups = {int(g.strip()) for g in args.groups.split(",")}

    now = datetime.now()
    file_ts = now.strftime("%Y%m%d_%H%M%S")
    iso_ts = now.strftime("%Y-%m-%dT%H:%M:%S")

    pdfs = [dict(p) for p in PDFS_VALIDATION]
    if args.include_blog_pdf:
        pdfs.append(dict(PDF_BLOG_EXTRA))

    _p(bold("\npdf-mcp Section Chunking Benchmark"))
    _p("─" * 68)
    if args.calibrate:
        _p(yellow("  Calibration mode: thresholds are NOT enforced."))

    # Resolve URLs to local paths (network → cache).
    try:
        for pdf in pdfs:
            pdf["_local_path"] = _resolve_pdf_local_path(pdf["url"])
    except Exception as exc:  # noqa: BLE001
        _p(red(f"  Setup error: cannot resolve PDF: {exc}"))
        sys.exit(2)

    results: dict = {}

    with tempfile.TemporaryDirectory() as tmp:
        original_cache = server_module.cache
        server_module.cache = PDFCache(cache_dir=Path(tmp), ttl_hours=1)
        try:
            if 1 in selected_groups:
                results["group_1"] = run_boundary_group(pdfs)
            if 2 in selected_groups:
                results["group_2"] = run_completeness_group(pdfs)
            if 3 in selected_groups:
                results["group_3"] = run_toolcall_group(pdfs)
        except ValueError as exc:
            _p(red(f"  Setup error: {exc}"))
            _save_results(results, file_ts, iso_ts)
            sys.exit(2)
        finally:
            server_module.cache = original_cache

    passed, failures = _print_summary(results, calibrate=args.calibrate)
    _save_results(results, file_ts, iso_ts)

    _p()
    _p(f"  Results saved to benchmark_results/sections_{file_ts}.{{txt,json}}")

    if args.calibrate:
        sys.exit(0)
    if not passed:
        _p()
        _p(red(f"  FAIL — {len(failures)} threshold(s) missed:"))
        for f in failures:
            _p(red(f"    - {f}"))
        sys.exit(1)
    _p()
    _p(green("  PASS — all thresholds met."))
    sys.exit(0)


if __name__ == "__main__":
    main()
