"""Differential tests for the five get_text shapes.

Tokenization differs between engines, so text is compared as a token
multiset (bag_f1), never by string equality. Structural fields that feed
a classifier (line dir, span flags) are compared exactly, because drift
there changes behaviour rather than wording.
"""

import pymupdf
import pytest

from pdf_mcp.backend.text import get_text
from tests.backend.differential import assert_non_empty

_FIXTURE = "pages/corpus/gao-cloud.pdf"
_PAGE = 2


def bag_f1(expected: str, actual: str) -> float:
    """Token-multiset F1. See module docstring for why not equality."""
    exp, act = expected.split(), actual.split()
    if not exp or not act:
        return 0.0
    want: dict[str, int] = {}
    for tok in exp:
        want[tok] = want.get(tok, 0) + 1
    overlap = 0
    got: dict[str, int] = {}
    for tok in act:
        got[tok] = got.get(tok, 0) + 1
    for tok, n in got.items():
        overlap += min(want.get(tok, 0), n)
    precision = overlap / len(act)
    recall = overlap / len(exp)
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def test_text_recovers_the_same_tokens():
    ref_doc = pymupdf.open(_FIXTURE)
    ref = ref_doc[_PAGE].get_text()
    ref_doc.close()
    got = get_text(_FIXTURE, _PAGE, "text")
    assert_non_empty(got, "text")
    assert bag_f1(ref, got) >= 0.95, f"bag_f1={bag_f1(ref, got):.3f}"


def test_blocks_shape_and_type_flag():
    """extractor filters on block[6] == 0 to drop image blocks, and reads
    block[4] as the text. A 7-tuple is the contract."""
    blocks = get_text(_FIXTURE, _PAGE, "blocks")
    assert_non_empty(blocks, "blocks")
    for block in blocks:
        assert len(block) == 7, f"expected 7-tuple, got {len(block)}"
        assert isinstance(block[4], str)
        assert block[6] in (0, 1)
        assert block[0] <= block[2] and block[1] <= block[3]
    assert any(b[6] == 0 for b in blocks)


def test_blocks_sort_true_is_reading_order():
    """extractor calls get_text('blocks', sort=True) in four places."""
    blocks = get_text(_FIXTURE, _PAGE, "blocks", sort=True)
    assert_non_empty(blocks, "sorted blocks")
    tops = [round(b[1], 1) for b in blocks]
    assert tops == sorted(tops), "sort=True must order blocks top to bottom"


def test_words_shape():
    words = get_text(_FIXTURE, _PAGE, "words")
    assert_non_empty(words, "words")
    for word in words[:20]:
        assert len(word) == 8
        assert isinstance(word[4], str)
        assert " " not in word[4], "a word must not contain a space"


def test_clip_restricts_to_the_region():
    """server and the excerpt harness read text from a bbox; a clip that
    is ignored silently returns the whole page and every containment
    check passes."""
    ref_doc = pymupdf.open(_FIXTURE)
    page = ref_doc[_PAGE]
    full_ref = page.get_text()
    half = pymupdf.Rect(0, 0, page.rect.width, page.rect.height / 2)
    ref_clipped = page.get_text(clip=half)
    ref_doc.close()

    got = get_text(_FIXTURE, _PAGE, "text", clip=(0, 0, 612.0, 396.0))
    assert_non_empty(got, "clipped text")
    assert len(got) < len(full_ref), "clip was ignored"
    assert bag_f1(ref_clipped, got) >= 0.90


def test_unknown_shape_is_rejected():
    """A typo must not silently return plain text."""
    with pytest.raises(ValueError):
        get_text(_FIXTURE, _PAGE, "raw_dict")


def test_rotated_sidebar_does_not_swallow_the_page():
    """A rotated run spans the page height, so it vertically overlaps
    every horizontal line. Merging on overlap alone let the 355pt-tall
    sidebar on nist-zero-trust p8 absorb its neighbours, taking the page
    from 46 lines to 24 and losing whole table-of-contents entries."""
    path = "pages/corpus/nist-zero-trust.pdf"
    page_no = 7

    ref_doc = pymupdf.open(path)
    ref_lines = [ln for ln in ref_doc[page_no].get_text().split("\n") if ln.strip()]
    ref_doc.close()

    got = get_text(path, page_no, "text")
    got_lines = [ln for ln in got.split("\n") if ln.strip()]
    assert_non_empty(ref_lines, "pymupdf lines")
    assert len(got_lines) >= 0.8 * len(ref_lines), (
        f"{len(got_lines)} lines vs PyMuPDF's {len(ref_lines)}: "
        "a tall rotated row is absorbing its neighbours"
    )
    # The sidebar itself must survive as its own line, not be dropped.
    assert any("available free of charge" in ln for ln in got_lines)
    # And it must not be glued onto a body line.
    for line in got_lines:
        if "available free of charge" in line:
            assert "Architecture" not in line, "sidebar merged into a body line"


def test_content_recovery_is_near_total():
    """Word-level recall against PyMuPDF, which is the honest content
    measure: bag_f1 over raw tokens penalises dot-leader tokenization on
    contents pages where nothing is actually lost."""
    import re

    path = "pages/corpus/gao-cloud.pdf"
    ref_doc = pymupdf.open(path)
    total = hit = 0
    for page_no in range(ref_doc.page_count):
        ref_words = re.findall(r"[A-Za-z0-9]+", ref_doc[page_no].get_text())
        if len(ref_words) < 30:
            continue
        counts: dict[str, int] = {}
        for word in re.findall(r"[A-Za-z0-9]+", get_text(path, page_no, "text")):
            counts[word] = counts.get(word, 0) + 1
        for word in ref_words:
            total += 1
            if counts.get(word, 0) > 0:
                counts[word] -= 1
                hit += 1
    ref_doc.close()
    assert total > 500, "fixture too small to be meaningful"
    assert hit / total >= 0.99, f"content recall {hit / total:.4f}"


def test_lines_do_not_splice_across_a_column_gutter():
    """Baseline grouping spans the whole page width, so a left-column and
    a right-column line sharing a baseline become one line, and the page
    reads as interleaved nonsense ("...the more sensitive the IV.
    SIMULATION RESULTS").

    Recall cannot see this: every word is still present, just in the
    wrong order. It cost 0.21 of two-column reading order against
    PyMuPDF on the READoc corpus while recall stayed at 0.874.
    """
    import os

    path = "benchmark_data/.reading_order_pdfs/0709.4466.pdf"
    if not os.path.exists(path):
        pytest.skip("local reading-order corpus not present")

    tree = get_text(path, 2, "dict")
    lines = [
        line
        for block in tree["blocks"]
        if block.get("type") == 0
        for line in block.get("lines", [])
    ]
    assert_non_empty(lines, "lines")

    page_width = 612.0
    spanning = [
        line
        for line in lines
        if (line["bbox"][2] - line["bbox"][0]) > 0.62 * page_width
    ]
    assert not spanning, (
        f"{len(spanning)} line(s) span both columns, e.g. "
        f"{spanning[0]['bbox']}: baseline grouping is splicing the gutter"
    )


def test_block_bbox_recovers_height_from_flat_char_boxes():
    """A block whose glyph boxes are all zero-height still reports a
    usable rect.

    pdfium returns flat char boxes when it cannot resolve a font's
    vertical metrics: a CJK font referenced but not embedded, on a host
    with no substitute. Widths survive (they come from the font's widths
    array) so only the height collapses, and pdf_search's bbox evidence
    became (72, 200, 450, 200). That is a rect a caller can neither draw
    nor crop with, and it reached CI as a real failure on Linux while
    passing on a machine that happened to have the font.
    """
    from pdf_mcp.backend.text import _Char, _block_bbox

    flat = [
        _Char(i, "厚", 72.0 + i * 14, 200.0, 86.0 + i * 14, 200.0, "japan-s", 14.0, 0)
        for i in range(4)
    ]
    block = [((72.0, 200.0, 128.0, 200.0), "厚木基地", flat)]

    x0, y0, x1, y1 = _block_bbox(block)
    assert x1 > x0, "width should be unaffected"
    assert y1 > y0, "height must be recovered, not left flat"
    assert y1 - y0 == 14.0, "height should be the em box above the baseline"


def test_block_bbox_leaves_normal_blocks_untouched():
    """The flat-box fallback must not perturb ordinary geometry, which
    chart calibration reads."""
    from pdf_mcp.backend.text import _Char, _block_bbox

    chars = [_Char(0, "A", 72.0, 190.0, 86.0, 204.0, "Helvetica", 14.0, 0)]
    block = [((72.0, 190.0, 86.0, 204.0), "A", chars)]
    assert _block_bbox(block) == (72.0, 190.0, 86.0, 204.0)


def test_block_bbox_recovers_height_when_font_size_is_also_zero():
    """The real CI case: pdfium reports size 0 alongside the flat boxes.

    The first fix guarded on size > 0 and therefore did nothing, and the
    bbox reached CI flat a second time. Widths are still trustworthy, so
    the median advance carries the height for the full-width CJK glyphs
    this occurs on.
    """
    from pdf_mcp.backend.text import _Char, _block_bbox

    flat = [
        _Char(i, "厚", 72.0 + i * 14, 200.0, 86.0 + i * 14, 200.0, "japan-s", 0.0, 0)
        for i in range(4)
    ]
    block = [((72.0, 200.0, 128.0, 200.0), "厚木基地", flat)]
    x0, y0, x1, y1 = _block_bbox(block)
    assert y1 > y0, "height must be recovered even with no font size"
    assert y1 - y0 == 14.0
