"""The rawdict/dict tree, the widest contract in the migration.

extractor, section_detector, chart_extractor and content_trust all walk
blocks -> lines -> spans -> chars. Each assertion here corresponds to a
field some consumer indexes directly, where a wrong value changes
behaviour rather than wording.
"""

import pymupdf
import pytest

from pdf_mcp.backend.text import get_text
from tests.backend.differential import assert_non_empty

_FIXTURE = "pages/corpus/gao-cloud.pdf"
_PAGE = 2
_TWO_COL = "benchmark_data/.reading_order_pdfs/1406.4582.pdf"


def spans_of(tree):
    return [
        span
        for block in tree["blocks"]
        if block.get("type") == 0
        for line in block.get("lines", [])
        for span in line.get("spans", [])
    ]


def lines_of(tree):
    return [
        line
        for block in tree["blocks"]
        if block.get("type") == 0
        for line in block.get("lines", [])
    ]


def test_rawdict_tree_is_navigable():
    tree = get_text(_FIXTURE, _PAGE, "rawdict")
    assert_non_empty(tree.get("blocks"), "blocks")
    spans = spans_of(tree)
    assert_non_empty(spans, "spans")
    for span in spans[:25]:
        assert isinstance(span["font"], str) and span["font"]
        assert isinstance(span["size"], float) and span["size"] > 0
        assert isinstance(span["flags"], int)
        assert len(span["bbox"]) == 4
        assert_non_empty(span["chars"], "chars")
        for ch in span["chars"][:5]:
            assert isinstance(ch["c"], str) and len(ch["c"]) >= 1
            assert len(ch["bbox"]) == 4


def test_dict_has_span_text_but_no_chars():
    """extractor.detect_writing_mode uses 'dict' precisely because it does
    not need per-glyph data; carrying chars there wastes memory on every
    page of every document."""
    tree = get_text(_FIXTURE, _PAGE, "dict")
    spans = spans_of(tree)
    assert_non_empty(spans, "spans")
    for span in spans[:25]:
        assert isinstance(span["text"], str)
        assert "chars" not in span


def test_char_bboxes_sit_inside_their_span():
    """_page_glyph_boxes builds column detection out of char bboxes, so a
    wrong glyph box moves a column gutter."""
    tree = get_text(_FIXTURE, _PAGE, "rawdict")
    for span in spans_of(tree)[:20]:
        sx0, sy0, sx1, sy1 = span["bbox"]
        for ch in span["chars"]:
            cx0, cy0, cx1, cy1 = ch["bbox"]
            assert cx0 >= sx0 - 1.0 and cx1 <= sx1 + 1.0
            assert cy0 >= sy0 - 1.0 and cy1 <= sy1 + 1.0


def test_rawdict_spans_carry_chars_not_text():
    """Parity with PyMuPDF, which puts 'text' on dict spans and 'chars'
    on rawdict spans and never both. chart_extractor._power_pairs joins
    the chars precisely because there is no 'text' to read; a shim that
    supplied one would let a consumer depend on a field the real engine
    does not provide."""
    tree = get_text(_FIXTURE, _PAGE, "rawdict")
    spans = spans_of(tree)
    assert_non_empty(spans, "spans")
    for span in spans[:30]:
        assert "text" not in span
        rebuilt = "".join(c["c"] for c in span["chars"])
        assert rebuilt.strip(), "joined chars must reconstruct the span text"


def test_span_size_matches_pymupdf():
    """section_detector compares span size against the page's body size to
    vote on headings, so a size in the wrong unit silently disables that
    signal rather than raising."""
    ref_doc = pymupdf.open(_FIXTURE)
    ref = ref_doc[_PAGE].get_text("rawdict")
    ref_doc.close()
    ref_sizes = sorted(
        round(s["size"], 1)
        for b in ref["blocks"]
        if b.get("type") == 0
        for ln in b.get("lines", [])
        for s in ln.get("spans", [])
    )
    got_sizes = sorted(
        round(s["size"], 1) for s in spans_of(get_text(_FIXTURE, _PAGE, "rawdict"))
    )
    assert_non_empty(ref_sizes, "pymupdf sizes")
    assert_non_empty(got_sizes, "shim sizes")
    import statistics

    assert abs(statistics.median(got_sizes) - statistics.median(ref_sizes)) < 0.6


def test_line_dir_is_horizontal_on_a_latin_page():
    """detect_writing_mode classifies a page from line['dir'] alone. A
    missing or wrong dir silently routes Latin pages into the vertical
    reorder path."""
    tree = get_text(_FIXTURE, _PAGE, "dict")
    lines = lines_of(tree)
    assert_non_empty(lines, "lines")
    for line in lines[:25]:
        assert len(line["dir"]) == 2
    horizontal = sum(1 for ln in lines if abs(ln["dir"][0]) > abs(ln["dir"][1]))
    assert horizontal == len(lines), "every line on a Latin page is horizontal"


def test_bold_is_detected_where_pymupdf_detects_it():
    """section_detector votes on flags & 16. pdfium exposes no flags
    bitfield, so this is derived from the font name; it must still fire."""
    ref_doc = pymupdf.open(_FIXTURE)
    ref = ref_doc[_PAGE].get_text("rawdict")
    ref_doc.close()
    ref_bold = sum(
        1
        for b in ref["blocks"]
        if b.get("type") == 0
        for ln in b.get("lines", [])
        for s in ln.get("spans", [])
        if s["flags"] & 16
    )
    if ref_bold == 0:
        pytest.skip("no bold spans on this page")
    got_bold = sum(
        1 for s in spans_of(get_text(_FIXTURE, _PAGE, "rawdict")) if s["flags"] & 16
    )
    assert got_bold > 0, "bold detection lost entirely"


def test_block_bboxes_enclose_their_lines():
    tree = get_text(_FIXTURE, _PAGE, "rawdict")
    for block in tree["blocks"]:
        if block.get("type") != 0:
            continue
        bx0, by0, bx1, by1 = block["bbox"]
        for line in block["lines"]:
            lx0, ly0, lx1, ly1 = line["bbox"]
            assert lx0 >= bx0 - 1.0 and lx1 <= bx1 + 1.0
            assert ly0 >= by0 - 1.0 and ly1 <= by1 + 1.0


def test_glyph_boxes_support_column_detection():
    """The real consumer test: extractor._page_glyph_boxes must find two
    columns on a two-column page using only this tree."""
    import os

    if not os.path.exists(_TWO_COL):
        pytest.skip("local reading-order corpus not present")
    tree = get_text(_TWO_COL, 2, "rawdict")
    boxes = [
        ch["bbox"]
        for span in spans_of(tree)
        for ch in span["chars"]
        if str(ch["c"]).strip()
    ]
    assert_non_empty(boxes, "glyph boxes")
    xs = sorted((b[0] + b[2]) / 2 for b in boxes)
    left = sum(1 for x in xs if x < 306)
    right = len(xs) - left
    assert left > 50 and right > 50, "glyphs must populate both columns"
