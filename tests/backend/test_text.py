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
