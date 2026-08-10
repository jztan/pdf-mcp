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


def test_header_fallback_promotes_row_zero():
    """PyMuPDF sometimes reports a section title as the header."""
    from pdf_mcp.server import _resolve_header

    title_header = ["Electrical Characteristics (@ TA = +25C)", "", ""]
    rows = [["Characteristic", "Min", "Max"], ["Vf", "0.7", "1.1"]]
    header, body = _resolve_header(title_header, rows)
    assert header == ["Characteristic", "Min", "Max"]
    assert body == [["Vf", "0.7", "1.1"]]


def test_header_fallback_does_not_fire_on_a_real_header():
    from pdf_mcp.server import _resolve_header

    real = ["PARAMETER", "MIN", "MAX"]
    rows = [["Vf", "0.7", "1.1"]]
    header, body = _resolve_header(real, rows)
    assert header == real
    assert body == rows


def test_columns_reliable_false_when_a_cell_holds_two_numbers():
    from pdf_mcp.server import _columns_reliable

    assert _columns_reliable([["Reset Voltage", "0.4 0.5 1", "V"]]) is False
    assert _columns_reliable([["Reset Voltage", "0.4", "V"]]) is True


def test_attach_adds_context_to_an_ambiguous_match(ruled_table_pdf):
    """End to end: an ambiguous excerpt gains a header and its row."""
    from pdf_mcp.cache import PDFCache
    from pdf_mcp.server import _attach_table_context
    import tempfile as _tf

    with _tf.TemporaryDirectory() as tmp:
        cache = PDFCache(cache_dir=__import__("pathlib").Path(tmp))
        doc = pymupdf.open(ruled_table_pdf)
        rows = doc[0].get_text("blocks")
        doc.close()
        # Use the Supply Voltage row's own geometry as the match bbox.
        target = next(b for b in rows if "Supply Voltage" in b[4])
        match = {
            "page": 1,
            "excerpt": "Supply Voltage 4.5 16 V",
            "bbox": list(target[:4]),
        }
        out = _attach_table_context([match], ruled_table_pdf, cache)
        ctx = out[0]["table_context"]
        assert "Min" in ctx["header"]
        assert any("4.5" in c for c in ctx["row"])
        assert ctx["columns_reliable"] is True


def test_no_subprocess_for_unambiguous_matches(monkeypatch, ruled_table_pdf):
    """A prose search must cost nothing."""
    from pdf_mcp import server as srv
    from pdf_mcp.cache import PDFCache
    import pathlib
    import tempfile as _tf

    called = []
    monkeypatch.setattr(
        srv, "run_module_json", lambda *a, **k: called.append(a) or {"tables": {}}
    )
    with _tf.TemporaryDirectory() as tmp:
        cache = PDFCache(cache_dir=pathlib.Path(tmp))
        match = {"page": 1, "excerpt": "no numbers here at all", "bbox": [0, 0, 9, 9]}
        srv._attach_table_context([match], ruled_table_pdf, cache)
    assert called == []


def test_no_context_when_the_match_has_no_bbox(ruled_table_pdf):
    """49 of 51 gate matches carry a bbox; without one there is no row."""
    from pdf_mcp.cache import PDFCache
    from pdf_mcp.server import _attach_table_context
    import pathlib
    import tempfile as _tf

    with _tf.TemporaryDirectory() as tmp:
        cache = PDFCache(cache_dir=pathlib.Path(tmp))
        match = {"page": 1, "excerpt": "Supply Voltage 4.5 16 V"}
        out = _attach_table_context([match], ruled_table_pdf, cache)
    assert "table_context" not in out[0]


def test_no_context_when_the_block_spans_the_whole_table(ruled_table_pdf):
    """A text block covering every row identifies no row at all.

    PyMuPDF returns some tables as ONE text block (Berkshire 2024 p134: a
    202pt block over 16 rows of ~12pt). The centre-point test then lands on
    whichever row sits in the vertical middle and returns it confidently.
    Wrong-and-confident is worse than absent, so this must yield nothing.
    """
    from pdf_mcp.cache import PDFCache
    from pdf_mcp.server import _attach_table_context
    import pathlib
    import tempfile as _tf

    with _tf.TemporaryDirectory() as tmp:
        cache = PDFCache(cache_dir=pathlib.Path(tmp))
        # The full ruled table, not one of its rows.
        # No column-identity word, or the ambiguity trigger would reject it
        # before geometry is ever consulted and the test would pass vacuously.
        match = {
            "page": 1,
            "excerpt": "Supply Voltage 4.5 16 V Reset Voltage 0.4 1.0 V",
            "bbox": [50.0, 50.0, 300.0, 150.0],
        }
        out = _attach_table_context([match], ruled_table_pdf, cache)
    assert "table_context" not in out[0]


def test_header_promotion_is_not_bound_to_datasheet_vocabulary():
    """Real column labels are arbitrary noun phrases, not min/typ/max.

    Verified across three document families: the Federal Reserve consumer
    report keeps 'Table 2. Estimated APRs ...' as its header while the real
    labels sit in row 0, and Berkshire 2024 p134 keeps 'Interest expense'
    while the fiscal years sit in row 0. Only Vishay promoted, and only
    because its labels happen to read Min/Max.
    """
    from pdf_mcp.server import _resolve_header

    fed_header = ["Table 2. Estimated APRs for select online products", "", ""]
    fed_rows = [
        ["Rate advertised on website", "Product details", "Estimated APR equivalent"],
        ["1.15 factor rate", "Total repayment", "Approximately 70% APR"],
    ]
    header, body = _resolve_header(fed_header, fed_rows)
    assert header == fed_rows[0]
    assert body == fed_rows[1:]

    # Verbatim from Berkshire 2024 p134: two sub-tables side by side, so
    # the caption band holds TWO filled cells across 20 columns. Taken from
    # the extractor, not from truncated console output.
    brk_header = [""] * 20
    brk_header[1] = "Interest expense"
    brk_header[11] = "Income tax expense (benefit)"
    brk_row0 = [""] * 20
    for i, y in (
        (1, "2024"),
        (4, "2023"),
        (7, "2022"),
        (11, "2024"),
        (14, "2023"),
        (17, "2022"),
    ):
        brk_row0[i] = y
    brk_rows = [brk_row0, ["McLane"] + [""] * 19]
    header, body = _resolve_header(brk_header, brk_rows)
    assert header == brk_rows[0]
    assert body == brk_rows[1:]


def test_header_promotion_needs_three_columns():
    """A 2-column table's first data cell must not become a header.

    With only two columns, 'one filled header cell, two filled row cells'
    is the shape of ordinary data, so the structural signal cannot
    distinguish it from a caption and must not fire.
    """
    from pdf_mcp.server import _resolve_header

    header = ["Name", ""]
    rows = [["Alice", "30"], ["Bob", "41"]]
    got, body = _resolve_header(header, rows)
    assert got == header
    assert body == rows


def test_header_promotion_leaves_a_fully_populated_header_alone():
    from pdf_mcp.server import _resolve_header

    real = ["Parameter", "Description", "Min", "Max", "Unit"]
    rows = [["Ioutput1", "Cumulative IO", "-", "1200", "mA"]]
    got, body = _resolve_header(real, rows)
    assert got == real
    assert body == rows


def test_header_promotion_refuses_a_row_carrying_money():
    """A sparse but genuine header must survive a money-carrying row 0.

    Berkshire 2024 p55 has a real year header spread across 12 columns
    (3 filled), and row 0 is data: '$', '9,020'. The sparse-caption
    allowance alone promoted that data row over a correct header. A header
    names columns; it does not carry currency or thousands-grouped amounts.
    """
    from pdf_mcp.server import _resolve_header

    header = ["", "2024", "", "", "", "2023", "", "", "", "2022", "", ""]
    rows = [
        [
            "Insurance – underwriting",
            "$",
            "9,020",
            "",
            "",
            "$",
            "5,428",
            "",
            "",
            "$",
            "(30",
            ")",
        ],
        [
            "Insurance – investment income",
            "",
            "13,670",
            "",
            "",
            "",
            "9,567",
            "",
            "",
            "",
            "6,484",
            "",
        ],
    ]
    got, body = _resolve_header(header, rows)
    assert got == header
    assert body == rows


def test_header_promotion_still_fires_on_a_year_row_without_money():
    """Berkshire p61: caption header, row 0 is bare years, so promote."""
    from pdf_mcp.server import _resolve_header

    header = [""] * 18
    header[0] = "Percentage change"
    row0 = [""] * 18
    for i, v in (
        (1, "2024"),
        (4, "2023"),
        (7, "2022"),
        (11, "2024 vs 2023"),
        (14, "2023 vs 2022"),
    ):
        row0[i] = v
    rows = [row0, ["Interest and other income", "$", "11,550"] + [""] * 15]
    got, body = _resolve_header(header, rows)
    assert got == row0
    assert body == rows[1:]
