"""Gated integration test: the column-count ceiling kills glyph-soup on a real
over-segmented page (Sodegaura 広報 p4). Skips when the local corpus is absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_PDF = (
    Path(__file__).parent.parent
    / "docs_internal/sample_pdfs/vertical-jp"
    / "sodegaura_koho_2025-11_vertical-jp_p3-89pct.pdf"
)
_PAGE_INDEX = 3  # 0-indexed page 4


def _soup_lines(text: str) -> int:
    """Count lines that are mostly isolated single characters (glyph-soup)."""
    n = 0
    for line in text.splitlines():
        toks = line.split()
        if len(toks) >= 4 and sum(1 for t in toks if len(t) == 1) / len(toks) > 0.6:
            n += 1
    return n


@pytest.mark.skipif(not _PDF.exists(), reason="local vertical-jp corpus absent")
def test_sodegaura_p4_no_glyph_soup():
    """Over-segmented mixed page: ceiling routes to positional sort -> no soup,
    no ~3.5x duplication. NOT a coherence claim (vertical order needs region-
    based extraction); only asserts the glyph-soup/duplication bug is gone."""
    import pymupdf

    from pdf_mcp.extractor import extract_text_from_page

    doc = pymupdf.open(_PDF)
    try:
        text = extract_text_from_page(doc[_PAGE_INDEX])
    finally:
        doc.close()

    assert (
        _soup_lines(text) == 0
    ), f"glyph-soup still present: {_soup_lines(text)} lines"
    # Clip-path duplication inflated this to ~8094 chars; positional sort ~2330.
    assert len(text) < 4000, f"unexpected duplication: {len(text)} chars"
