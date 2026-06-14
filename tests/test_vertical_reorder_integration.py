"""Gated integration: vertical reorder produces non-soup, ordered text on real
vertical pages. Skips when the local vertical-jp corpus is absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_DIR = Path(__file__).parent.parent / "docs_internal/sample_pdfs/vertical-jp"
_IBK = _DIR / "ibk_72-102_vertical-jp-academic_99pct.pdf"


def _soup_lines(text: str) -> int:
    n = 0
    for line in text.splitlines():
        toks = line.split()
        if len(toks) >= 4 and sum(1 for t in toks if len(t) == 1) / len(toks) > 0.6:
            n += 1
    return n


@pytest.mark.skipif(not _IBK.exists(), reason="local vertical-jp corpus absent")
def test_ibk_vertical_reorder_is_ordered_prose():
    """ibk p2 (dense 2-col vertical academic) reorders to continuous prose."""
    import pymupdf

    from pdf_mcp.extractor import extract_text_from_page

    doc = pymupdf.open(_IBK)
    try:
        text = extract_text_from_page(doc[1])  # page 2, 0-indexed
    finally:
        doc.close()

    assert _soup_lines(text) == 0, f"glyph-soup present: {_soup_lines(text)}"
    # The reordered argument contains this contiguous phrase (validated in spike):
    assert "四十八願" in text
    # A coherent run, not glyph-per-line: at least one long unbroken segment.
    assert max(len(seg) for seg in text.splitlines()) > 30
