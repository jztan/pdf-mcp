import pymupdf

from pdf_mcp.backend.tables import find_tables
from tests.backend.differential import assert_non_empty

_FIXTURE = "pages/corpus/fed-consumer-context.pdf"
_PAGE = 5  # 0-indexed


def test_cell_text_matches_pymupdf_exactly():
    """pdfplumber matched PyMuPDF cell-for-cell in the spike. Cells are
    the payload agents read, so any drift is a real defect."""
    ref_doc = pymupdf.open(_FIXTURE)
    ref = [t.extract() for t in ref_doc[_PAGE].find_tables().tables]
    ref_doc.close()

    got = [t.extract() for t in find_tables(_FIXTURE, _PAGE)]

    assert_non_empty(ref, "pymupdf tables")
    assert_non_empty(got, "shim tables")
    assert len(got) == len(ref), f"table count {len(got)} != {len(ref)}"
    for ref_tbl, got_tbl in zip(ref, got):
        assert got_tbl == ref_tbl


def test_row_bboxes_present_for_every_row():
    """extract_tables_from_page emits one bbox per row; a missing row bbox
    silently drops table_context evidence."""
    tables = find_tables(_FIXTURE, _PAGE)
    assert_non_empty(tables, "tables")
    for table in tables:
        assert len(table.rows) == len(table.extract())
        for row in table.rows:
            assert row.bbox.width > 0


def test_table_bbox_matches_pymupdf():
    ref_doc = pymupdf.open(_FIXTURE)
    ref = [t.bbox for t in ref_doc[_PAGE].find_tables().tables]
    ref_doc.close()
    got = [t.bbox for t in find_tables(_FIXTURE, _PAGE)]
    assert_non_empty(ref, "pymupdf bboxes")
    assert len(got) == len(ref)
    for ref_box, got_box in zip(ref, got):
        for ref_v, got_v in zip(
            ref_box, (got_box.x0, got_box.y0, got_box.x1, got_box.y1)
        ):
            assert abs(ref_v - got_v) < 2.0, f"{ref_box} vs {got_box}"


def test_cell_text_keeps_inter_word_spaces():
    """At pdfplumber's default x tolerance, cells come back as
    'Table1.Selectdetailsfromonlinelenderwebsites'."""
    tables = find_tables(_FIXTURE, _PAGE)
    assert_non_empty(tables, "tables")
    cells = [c for t in tables for row in t.extract() for c in row if c]
    assert_non_empty(cells, "cells")
    multiword = [c for c in cells if " " in c]
    assert multiword, "no cell contains a space; x tolerance is wrong"


def test_one_row_detection_is_not_a_table():
    """extract_tables_from_page treats row 0 as the header and rows[1:]
    as data, so a one-row detection carries no data at all. pdfplumber
    reported section headings this way ("1 Introduct" | "ion")."""
    from pdf_mcp.backend.tables import _is_a_table

    assert not _is_a_table([["1 Introduct", "ion"]])
    assert _is_a_table([["h1", "h2"], ["a", "b"]])


def test_single_column_detection_is_not_a_table():
    from pdf_mcp.backend.tables import _is_a_table

    assert not _is_a_table([["some prose"], ["more prose"]])


def test_numeric_fraction_separates_data_tables_from_prose():
    """The fallback's failure mode is shredding prose into pseudo-columns.
    Measured, the real tables it recovers run 0.50 to 0.83 numeric and the
    invented ones 0.00 to 0.42."""
    from pdf_mcp.backend.tables import _numeric_fraction

    prose = [["outcomes de", "fined in this pr"], ["ofile are", "valuabl"]]
    # Shaped like the real fallback output for Transformer Table 3, where
    # the aggressive column split puts digits in nearly every cell.
    data = [
        ["base 6", "512 2048", "8 64", "0.1 0.", "1 100K 4", ".92 25.8"],
        ["big 6", "1024 4096", "16 64", "0.3 0.", "3 300K 4", ".33 26.4"],
    ]
    assert _numeric_fraction(prose) < 0.5
    assert _numeric_fraction(data) >= 0.5


def test_borderless_academic_table_is_found_via_fallback():
    """Transformer Table 3 is booktabs-style: horizontal rules only. The
    ruled-line finder sees nothing, so this exercises the fallback."""
    import os

    path = "docs_internal/sample_pdfs/academic/1706.03762v7.pdf"
    if not os.path.exists(path):
        import pytest

        pytest.skip("local academic sample not present")
    tables = find_tables(path, 8)
    assert_non_empty(tables, "transformer p9 tables")
    flat = " ".join(str(c) for t in tables for row in t.extract() for c in row if c)
    assert "25.8" in flat
