"""Gated integration: vertical reorder produces non-soup, ordered text on real
vertical pages. Skips when the local vertical-jp corpus is absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_DIR = Path(__file__).parent.parent / "docs_internal/sample_pdfs/vertical-jp"
_IBK = _DIR / "ibk_72-102_vertical-jp-academic_99pct.pdf"
_YAMATO = _DIR / "yamato_koho_2025-02_vertical-jp_p10-95pct.pdf"


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


@pytest.mark.skipif(not _YAMATO.exists(), reason="local vertical-jp corpus absent")
def test_yamato_p10_dense_layout_segmented_and_clean():
    """Dense multi-article page: segmentation + mojibake filter -> no soup, no
    leftover mojibake, award-winner names present (articles ordered)."""
    import pymupdf

    from pdf_mcp.extractor import extract_text_from_page

    doc = pymupdf.open(_YAMATO)
    try:
        text = extract_text_from_page(doc[9])  # page 10, 0-indexed
    finally:
        doc.close()

    assert _soup_lines(text) == 0
    # no glyph remains in the mojibake band
    assert not any(0x0590 <= ord(c) <= 0x1CFF for c in text)
    # an in-order winner-list fragment from the human-rights article
    assert "入賞作品" in text
