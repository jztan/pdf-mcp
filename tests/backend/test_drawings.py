"""Vector drawings, which back chart_extractor.

chart_extractor carries a zero wrong-emit gate, so geometry here is not
"close enough" territory: path_pts() reads it[1].x / it[1].y for "l",
four control points for "c", a Rect for "re" and a Quad for "qu", and
rects_of() pattern-matches the "re" verb directly. A miscounted verb
changes which chart is detected.
"""

import pymupdf
import pytest

from pdf_mcp.backend.drawings_router import get_drawings, page_uses_tiling_pattern
from tests.backend.differential import assert_non_empty

_SYN = "benchmark_data/chart_extraction/syn_corpus"
_FIXTURES = [
    "bar_simple",
    "hist_mono",
    "scatter_simple",
    "line_two_legend_dashed",
    "line_color_linear",
    "line_drawn_minus",
]


def verb_counts(drawings):
    counts: dict[str, int] = {}
    for drawing in drawings:
        for item in drawing["items"]:
            counts[item[0]] = counts.get(item[0], 0) + 1
    return counts


@pytest.mark.parametrize("name", _FIXTURES)
def test_verb_counts_match_pymupdf(name):
    path = f"{_SYN}/{name}.pdf"
    ref_doc = pymupdf.open(path)
    ref = ref_doc[0].get_drawings()
    ref_doc.close()

    got = get_drawings(path, 0)
    assert_non_empty(ref, f"{name} pymupdf drawings")
    assert_non_empty(got, f"{name} shim drawings")
    assert verb_counts(got) == verb_counts(ref), name


@pytest.mark.parametrize("name", _FIXTURES)
def test_drawing_count_matches_pymupdf(name):
    path = f"{_SYN}/{name}.pdf"
    ref_doc = pymupdf.open(path)
    ref = ref_doc[0].get_drawings()
    ref_doc.close()
    got = get_drawings(path, 0)
    assert len(got) == len(ref), f"{name}: {len(got)} drawings != {len(ref)}"


def test_rect_verb_carries_a_rect_object():
    """rects_of() does `it[1]` and reads .x0/.y0/.x1/.y1."""
    drawings = get_drawings(f"{_SYN}/bar_simple.pdf", 0)
    rects = [it[1] for d in drawings for it in d["items"] if it[0] == "re"]
    assert_non_empty(rects, "rects")
    for rect in rects[:10]:
        assert rect.x1 >= rect.x0 and rect.y1 >= rect.y0
        assert rect.width >= 0


def test_line_verb_carries_two_points():
    """path_pts does it[1].x, it[1].y, it[2].x, it[2].y."""
    drawings = get_drawings(f"{_SYN}/line_two_legend_dashed.pdf", 0)
    segments = [it for d in drawings for it in d["items"] if it[0] == "l"]
    assert_non_empty(segments, "line segments")
    for item in segments[:10]:
        assert len(item) == 3
        for point in (item[1], item[2]):
            assert isinstance(point.x, float) and isinstance(point.y, float)


def test_geometry_matches_pymupdf_coordinates():
    """Coordinates are the guaranteed half of the chart trust contract.
    float32 from pdfium's C API against PyMuPDF's float64 is the only
    difference allowed."""
    path = f"{_SYN}/bar_simple.pdf"
    ref_doc = pymupdf.open(path)
    ref = [
        (round(it[1].x0, 2), round(it[1].y0, 2), round(it[1].x1, 2), round(it[1].y1, 2))
        for d in ref_doc[0].get_drawings()
        for it in d["items"]
        if it[0] == "re"
    ]
    ref_doc.close()
    got = [
        (round(it[1].x0, 2), round(it[1].y0, 2), round(it[1].x1, 2), round(it[1].y1, 2))
        for d in get_drawings(path, 0)
        for it in d["items"]
        if it[0] == "re"
    ]
    assert_non_empty(ref, "pymupdf rects")
    assert len(got) == len(ref)
    for ref_rect, got_rect in zip(sorted(ref), sorted(got)):
        for ref_v, got_v in zip(ref_rect, got_rect):
            assert abs(ref_v - got_v) <= 0.05, f"{got_rect} != {ref_rect}"


def test_colours_match_pymupdf():
    """chart_extractor maps series colour to legend label, so a wrong
    colour silently mislabels a series."""
    path = f"{_SYN}/line_two_legend_dashed.pdf"
    ref_doc = pymupdf.open(path)
    ref = sorted(
        tuple(round(c, 2) for c in d["color"])
        for d in ref_doc[0].get_drawings()
        if d.get("color")
    )
    ref_doc.close()
    got = sorted(
        tuple(round(c, 2) for c in d["color"])
        for d in get_drawings(path, 0)
        if d.get("color")
    )
    assert_non_empty(ref, "pymupdf colours")
    assert len(got) == len(ref)
    for ref_c, got_c in zip(ref, got):
        for ref_v, got_v in zip(ref_c, got_c):
            assert abs(ref_v - got_v) <= 1 / 255


def test_dashes_are_reported():
    """_dash_key reads d.get('dashes'); a dashed series is how two
    same-colour lines are told apart."""
    drawings = get_drawings(f"{_SYN}/line_two_legend_dashed.pdf", 0)
    assert_non_empty(drawings, "drawings")
    assert any(str(d.get("dashes") or "").strip("[] ") for d in drawings)


def test_tiling_pattern_detection_is_negative_on_a_plain_chart():
    assert page_uses_tiling_pattern(f"{_SYN}/bar_simple.pdf", 0) is False
