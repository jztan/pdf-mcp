"""Table context attached to pdf_search matches."""

import os
import tempfile

import pymupdf
import pytest

from pdf_mcp.extractor import TABLE_EXTRACTION_VERSION
from pdf_mcp.parallel import run_module_json


@pytest.fixture
def ruled_table_pdf():
    """A bordered 2x3 table with decimal values in a MIN/MAX layout."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        doc = pymupdf.open()
        page = doc.new_page()
        page.draw_rect(pymupdf.Rect(50, 50, 300, 150), color=(0, 0, 0))
        for x in (130, 190, 245):
            page.draw_line(pymupdf.Point(x, 50), pymupdf.Point(x, 150), color=(0, 0, 0))
        for y in (83, 116):
            page.draw_line(pymupdf.Point(50, y), pymupdf.Point(300, y), color=(0, 0, 0))
        page.insert_text((55, 75), "Parameter")
        page.insert_text((135, 75), "Min")
        page.insert_text((195, 75), "Max")
        page.insert_text((250, 75), "Unit")
        page.insert_text((55, 108), "Supply Voltage")
        page.insert_text((135, 108), "4.5")
        page.insert_text((195, 108), "16")
        page.insert_text((250, 108), "V")
        page.insert_text((55, 141), "Reset Voltage")
        page.insert_text((135, 141), "0.4")
        page.insert_text((195, 141), "1.0")
        page.insert_text((250, 141), "V")
        doc.save(f.name)
        doc.close()
    yield f.name
    os.unlink(f.name)


def test_extraction_emits_one_bbox_per_row(ruled_table_pdf):
    """Geometric row selection needs per-row geometry, which was not cached."""
    out = run_module_json(
        "pdf_mcp._table_worker", {"path": ruled_table_pdf, "pages": [0]}
    )
    table = out["tables"]["0"][0]
    assert len(table["row_bboxes"]) == len(table["rows"])
    for bbox in table["row_bboxes"]:
        assert len(bbox) == 4
        assert bbox[3] > bbox[1]  # y1 below y0
    # Rows are ordered top to bottom, so each row starts below the previous.
    tops = [b[1] for b in table["row_bboxes"]]
    assert tops == sorted(tops)


def test_table_extraction_version_is_3():
    """Row geometry changes the cached page_tables shape."""
    assert TABLE_EXTRACTION_VERSION == 3


def test_ambiguity_trigger():
    """Only excerpts a caller cannot resolve are worth a subprocess."""
    from pdf_mcp.server import _excerpt_is_ambiguous

    # Several numbers, nothing saying which is which.
    assert _excerpt_is_ambiguous("Reset Voltage | 0.4 | 0.5 | 1 | V") is True
    # A column word resolves it, so no context is needed.
    assert _excerpt_is_ambiguous("Maximum forward voltage 1.1 V") is False
    # One number cannot be confused with anything.
    assert _excerpt_is_ambiguous("Total Capacitance CT 2.0 pF") is False
    # Prose with no numbers must never trigger a subprocess.
    assert _excerpt_is_ambiguous("we vary the number of attention heads") is False
