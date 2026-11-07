"""OCR on corpus warm (issue #103).

Pure logic, no MCP dependency. Decides from cached state which pages of a
warmed document need OCR, and how far OCR has got. A page needs OCR when
extraction found no text on it but it carries a raster image; a page with
any text, or a blank page, is never OCR'd.

Completion is always computed from the cache, never from a thread, so a
server restart cannot make a corpus look finished.
"""

from __future__ import annotations

from typing import Any


def ocr_candidates(coverage: list[dict[str, int]]) -> list[int]:
    """0-indexed pages with no extracted text and at least one raster image."""
    return [
        pn
        for pn, c in enumerate(coverage)
        if c.get("text_chars", 0) == 0 and c.get("raster_images", 0) > 0
    ]


def _coverage(path: str, cache: Any) -> list[dict[str, int]]:
    meta = cache.get_metadata(path) or {}
    return list(meta.get("text_coverage") or [])


def ocr_progress(path: str, cache: Any, ocr_lang: str) -> dict[str, int]:
    """Counts over the candidate pages: still pending, OCR'd with text,
    and OCR'd with no text (a failed or empty read, never retried)."""
    candidates = ocr_candidates(_coverage(path, cache))
    rows = cache.get_ocr_rows(path, candidates, ocr_lang)
    done = sum(1 for t in rows.values() if t.strip())
    return {
        "pending": len(candidates) - len(rows),
        "done": done,
        "no_text": len(rows) - done,
    }


def ocr_pending_pages(path: str, cache: Any, ocr_lang: str) -> list[int]:
    """Candidate pages with no OCR row for ``ocr_lang`` (empty or not).
    A document with no metadata row has not been warmed: nothing pending."""
    candidates = ocr_candidates(_coverage(path, cache))
    have = cache.get_ocr_rows(path, candidates, ocr_lang)
    return [pn for pn in candidates if pn not in have]


def effective_coverage(path: str, cache: Any) -> list[dict[str, int]]:
    """The cached coverage map, with OCR text (any language) counted as
    text, so a fully OCR'd scan reads as covered."""
    coverage = [dict(c) for c in _coverage(path, cache)]
    empty = [pn for pn, c in enumerate(coverage) if c.get("text_chars", 0) == 0]
    for pn, text in cache.get_ocr_rows(path, empty, None).items():
        if text.strip():
            coverage[pn]["text_chars"] = len(text.strip())
    return coverage
