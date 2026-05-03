"""
Multi-signal section detector for PDF documents.

Combines 7 weak signals (font face delta, bold marker, whitespace gap,
top-of-page, heading regex, title-case, short-line) via a weighted score
to identify section boundaries in academic PDFs.

Public API:
    Section         — dataclass representing a detected section
    detect_boundaries(pdf_path) — run the detector on a PDF file
    HEADING_SCORE_THRESHOLD     — tunable threshold constant (default 4)
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

import pymupdf


# ---- Core dataclass ----
@dataclass
class Section:
    title: str
    start_page: int  # 1-indexed
    end_page: int  # 1-indexed, inclusive
    text: str = ""  # full concatenated text of all pages in [start_page, end_page]


# ---- Internal constants ----

# Heading regex:
#   - Numbered headings like "1 Introduction", "1.1 Background", etc.
#     (digit-dot sequences followed by uppercase start; avoids prose like
#     "1km of cable" which has no uppercase after the number, and avoids
#     defeating a global IGNORECASE).
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

_BOLD_NAME_MARKERS = ("Bold", "-B", ".B")
_TOP_OF_PAGE_FRACTION = 0.15
_WHITESPACE_GAP_RATIO = 1.5
_SHORT_LINE_CHARS = 80

_NUMBER_ONLY_RE = re.compile(r"^\d+(\.\d+)*\.?$")

HEADING_SCORE_THRESHOLD = 4
_SIGNAL_WEIGHTS = {
    "face_delta": 2,
    "bold_marker": 2,
    "whitespace_above": 1,
    "top_of_page": 1,
    "regex_match": 3,
    "title_case_or_caps": 1,
    "short_line": 1,
}


# ---- Internal helpers ----


def _compute_body_fingerprint(
    lines: list[dict[str, Any]],
) -> tuple[str, bool] | None:
    """
    Identify the document's body text fingerprint as the most common
    (font_name, is_bold) tuple across all non-empty lines.

    Each line contributes its dominant span (longest text). A line with
    no text is ignored.

    Returns None if no non-empty lines are provided.
    """
    counter: Counter[tuple[str, bool]] = Counter()
    for line in lines:
        spans = line.get("spans", [])
        non_empty = [s for s in spans if s.get("text", "").strip()]
        if not non_empty:
            continue
        dominant = max(non_empty, key=lambda s: len(s["text"]))
        face = dominant["font"]
        is_bold = bool(dominant.get("flags", 0) & 16)
        counter[(face, is_bold)] += 1
    if not counter:
        return None
    return counter.most_common(1)[0][0]


def _looks_bold(font_name: str, flags: int) -> bool:
    """A line is bold if the flag bit is set OR the font name has a bold marker."""
    if flags & 16:
        return True
    return any(marker in font_name for marker in _BOLD_NAME_MARKERS)


def _is_title_case_or_caps(text: str) -> bool:
    """Title Case (most words start uppercase) or ALL CAPS — heading typography."""
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.isupper() and any(c.isalpha() for c in stripped):
        return True
    words = [w for w in stripped.split() if any(c.isalpha() for c in w)]
    if not words:
        return False
    initial_caps = sum(1 for w in words if w[0].isupper())
    return initial_caps / len(words) >= 0.6


def _line_features(
    line: dict[str, Any],
    body_fingerprint: tuple[str, bool] | None,
    prev_line: dict[str, Any] | None,
    page_height: float,
) -> dict[str, bool]:
    """
    Compute the 7 weak signals for one line. Returns a dict of bool flags.
    """
    spans = line.get("spans", [])
    non_empty = [s for s in spans if s.get("text", "").strip()]
    empty_features: dict[str, bool] = {
        k: False
        for k in (
            "face_delta",
            "bold_marker",
            "whitespace_above",
            "top_of_page",
            "regex_match",
            "title_case_or_caps",
            "short_line",
        )
    }
    if not non_empty:
        return empty_features
    dominant = max(non_empty, key=lambda s: len(s["text"]))
    text = "".join(s["text"] for s in spans).strip()
    font = dominant["font"]
    flags = dominant.get("flags", 0)
    is_bold_flag = bool(flags & 16)
    bbox = line.get("bbox", [0, 0, 0, 0])
    y0 = bbox[1]
    y1 = bbox[3]
    line_height = max(y1 - y0, 1.0)

    face_delta = (
        body_fingerprint is not None and (font, is_bold_flag) != body_fingerprint
    )
    bold_marker = _looks_bold(font, flags)

    if prev_line is None:
        whitespace_above = True
    else:
        prev_y1 = prev_line.get("bbox", [0, 0, 0, 0])[3]
        gap = y0 - prev_y1
        whitespace_above = gap >= _WHITESPACE_GAP_RATIO * line_height

    top_of_page = y0 < _TOP_OF_PAGE_FRACTION * page_height
    regex_match = bool(_HEADING_RE.match(text))
    title_case_or_caps = _is_title_case_or_caps(text)
    short_line = len(text) <= _SHORT_LINE_CHARS

    return {
        "face_delta": face_delta,
        "bold_marker": bold_marker,
        "whitespace_above": whitespace_above,
        "top_of_page": top_of_page,
        "regex_match": regex_match,
        "title_case_or_caps": title_case_or_caps,
        "short_line": short_line,
    }


def _heading_score(features: dict[str, bool]) -> int:
    """Sum of weighted signals — see _SIGNAL_WEIGHTS for rationale."""
    return sum(_SIGNAL_WEIGHTS[k] for k, fired in features.items() if fired)


def _is_heading(
    features: dict[str, bool], threshold: int = HEADING_SCORE_THRESHOLD
) -> bool:
    """A line is a heading iff its summed signal score >= threshold."""
    return _heading_score(features) >= threshold


def _merge_split_headings(
    candidates: list[tuple[int, str, float]],
    max_y_gap: float = 50.0,
) -> list[tuple[int, str, float]]:
    """
    When a heading is rendered as a bare number line followed by its title
    on the next line (same page, vertically close), merge them into one
    candidate. Otherwise pass through unchanged.

    candidates: list of (page, text, y_position).
    """
    merged: list[tuple[int, str, float]] = []
    i = 0
    while i < len(candidates):
        page, text, y = candidates[i]
        if (
            _NUMBER_ONLY_RE.match(text.strip())
            and i + 1 < len(candidates)
            and candidates[i + 1][0] == page
            and abs(candidates[i + 1][2] - y) <= max_y_gap
        ):
            next_text = candidates[i + 1][1]
            merged.append((page, f"{text.strip()} {next_text.strip()}", y))
            i += 2
        else:
            merged.append((page, text, y))
            i += 1
    return merged


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


def _filter_to_leaves(sections: list[Section]) -> list[Section]:
    """
    Filter to leaf sections: those whose page range contains no other
    section's start_page. Removes parent containers in nested TOC
    hierarchies, yielding a non-overlapping partition.

    A section is a leaf iff no other section starts strictly within its
    (start_page, end_page] range. Heuristic-mode output (already flat)
    passes through unchanged.
    """
    starts = [s.start_page for s in sections]
    out = []
    for s in sections:
        has_child = any(
            other_start > s.start_page and other_start <= s.end_page
            for other_start in starts
        )
        if not has_child:
            out.append(s)
    return out


def extract_toc_sections(doc: pymupdf.Document) -> list[Section]:
    """
    Derive sections from the PDF's TOC, filling section.text from page text.

    Caller is responsible for opening and closing the document.

    Raises ValueError if doc.get_toc() is empty.
    """
    toc = doc.get_toc()
    sections = _toc_entries_to_sections(toc, total_pages=len(doc))
    for s in sections:
        if s.start_page > s.end_page:
            # Malformed (e.g. consecutive entries on same page). Skip text fill.
            continue
        pages_text = []
        for p in range(s.start_page - 1, s.end_page):
            pages_text.append(doc[p].get_text())
        s.text = "\n".join(pages_text)
    return sections


def detect_boundaries(pdf_path: str) -> list[Section]:
    """
    Multi-signal section detector. Combines 7 weak signals (font face delta,
    bold marker, whitespace gap, top-of-page, heading regex, title-case,
    short-line) via a weighted score; threshold-4 wins.

    Multi-line headings (a number line followed by the title text on the
    next line) are merged via _merge_split_headings before section assembly.
    """
    doc = pymupdf.open(pdf_path)
    try:
        # Phase 1: collect every line with its dict-shape attributes.
        all_lines: list[tuple[int, dict[str, Any], float]] = []
        # ^ (page_1idx, line_dict, page_height)
        for page_idx in range(len(doc)):
            page = doc[page_idx]
            page_height = page.rect.height
            for blk in page.get_text("dict")["blocks"]:
                if "lines" not in blk:
                    continue
                for line in blk["lines"]:
                    all_lines.append((page_idx + 1, line, page_height))

        # Phase 2: compute body fingerprint from raw line dicts.
        body_fingerprint = _compute_body_fingerprint([line for _, line, _ in all_lines])

        # Phase 3: score each line; collect candidates with their y-position.
        candidates: list[tuple[int, str, float]] = []
        prev_line_per_page: dict[int, dict[str, Any]] = {}
        for page, line, page_height in all_lines:
            features = _line_features(
                line,
                body_fingerprint,
                prev_line_per_page.get(page),
                page_height,
            )
            prev_line_per_page[page] = line
            if not _is_heading(features):
                continue
            text = "".join(s["text"] for s in line.get("spans", [])).strip()
            if not text:
                continue
            y0 = line.get("bbox", [0, 0, 0, 0])[1]
            candidates.append((page, text, y0))

        # Phase 4: merge split number/title pairs.
        candidates = _merge_split_headings(candidates)

        # Phase 5: assemble Sections from candidate boundaries.
        sections: list[Section] = []
        for i, (page, title, _y) in enumerate(candidates):
            if i + 1 < len(candidates):
                end_page = candidates[i + 1][0] - 1
            else:
                end_page = len(doc)
            sections.append(
                Section(title=title, start_page=page, end_page=end_page, text="")
            )

        # Phase 6: fill section text from page ranges.
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


def derive_sections(pdf_path: str) -> list[Section]:
    """
    TOC-first / heuristic-fallback dispatcher.

    If the PDF has a TOC, returns TOC-derived sections (authoritative).
    Otherwise falls back to the multi-signal heuristic detector.
    Returns [] if both yield no sections.
    """
    doc = pymupdf.open(pdf_path)
    try:
        if doc.get_toc():
            return extract_toc_sections(doc)
    finally:
        doc.close()
    return detect_boundaries(pdf_path)
