"""Table context attached to pdf_search matches."""

import tempfile

import pymupdf
import pytest

from pdf_mcp.extractor import TABLE_EXTRACTION_VERSION
from pdf_mcp.extractor import extract_tables_for_pages
from tests.tmpfiles import unlink_quietly


@pytest.fixture
def ruled_table_pdf():
    """A bordered 2x3 table with decimal values in a MIN/MAX layout."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        # Close the handle before anything writes to this path: Windows
        # refuses to write or replace a file that is still open, which
        # turned into 639 errors the first time CI ran there. delete=False
        # means closing early does not remove the file.
        f.close()
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
    unlink_quietly(f.name)


@pytest.fixture
def header_ruled_table_pdf():
    """A table ruled in the header band only.

    Vertical rules subdivide Parameter|Min|Max|Unit in the header row but
    NOT in the body, so pdfplumber recovers the 4-column header grid yet
    packs the body's Min and Max into one cell ("4.5 16"). This is the TI
    LM555 shape a synthetic PDF can express.
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.close()
        doc = pymupdf.open()
        page = doc.new_page()
        # Outer border: full-height left/right verticals, top/bottom rules.
        page.draw_rect(pymupdf.Rect(50, 50, 300, 150), color=(0, 0, 0))
        # Body verticals: Parameter|rest and rest|Unit span header->bottom,
        # so Parameter and Unit stay separate; NO Min|Max divider in body.
        for x in (130, 245):
            page.draw_line(pymupdf.Point(x, 50), pymupdf.Point(x, 150), color=(0, 0, 0))
        # Header-only divider between Min and Max (y 50..83 only).
        page.draw_line(pymupdf.Point(190, 50), pymupdf.Point(190, 83), color=(0, 0, 0))
        # Horizontal rules: header/body and between the two body rows.
        for y in (83, 116):
            page.draw_line(pymupdf.Point(50, y), pymupdf.Point(300, y), color=(0, 0, 0))
        page.insert_text((55, 75), "Parameter")
        page.insert_text((135, 75), "Min")
        page.insert_text((200, 75), "Max")
        page.insert_text((250, 75), "Unit")
        # Body row 1: 4.5 under Min (centre ~145), 16 under Max (centre ~207).
        page.insert_text((55, 108), "Supply Voltage")
        page.insert_text((135, 108), "4.5")
        page.insert_text((200, 108), "16")
        page.insert_text((250, 108), "V")
        # Body row 2.
        page.insert_text((55, 141), "Reset Voltage")
        page.insert_text((135, 141), "0.4")
        page.insert_text((200, 141), "1")
        page.insert_text((250, 141), "V")
        doc.save(f.name)
        doc.close()
    yield f.name
    unlink_quietly(f.name)


def test_packed_header_ruled_table_splits_and_turns_reliable(header_ruled_table_pdf):
    out = extract_tables_for_pages(header_ruled_table_pdf, [0])
    tables = out["tables"]["0"]
    assert tables, "fixture must yield a table"
    t = tables[0]
    # Header recovered all four columns.
    assert [c.strip() for c in t["header"]] == ["Parameter", "Min", "Max", "Unit"]
    # The packed body cell was rewritten: 4.5 -> Min, 16 -> Max.
    first = t["rows"][0]
    assert "4.5" in first and "16" in first
    assert "4.5 16" not in "".join(str(c) for c in first)
    assert t["split_cells"] >= 1
    assert t["columns_reliable"] is True


def test_extraction_emits_one_bbox_per_row(ruled_table_pdf):
    """Geometric row selection needs per-row geometry, which was not cached."""
    out = extract_tables_for_pages(ruled_table_pdf, [0])
    table = out["tables"]["0"][0]
    assert len(table["row_bboxes"]) == len(table["rows"])
    for bbox in table["row_bboxes"]:
        assert len(bbox) == 4
        assert bbox[3] > bbox[1]  # y1 below y0
    # Rows are ordered top to bottom, so each row starts below the previous.
    tops = [b[1] for b in table["row_bboxes"]]
    assert tops == sorted(tops)


def test_table_extraction_version_is_5():
    """Packed-cell split changes the cached table shape (columns_reliable)."""
    assert TABLE_EXTRACTION_VERSION == 5


def test_every_table_carries_columns_reliable_and_split_cells(ruled_table_pdf):
    out = extract_tables_for_pages(ruled_table_pdf, [0])
    tables = out["tables"]["0"]
    assert tables, "fixture must yield at least one table"
    for t in tables:
        assert isinstance(t["columns_reliable"], bool)
        assert isinstance(t["split_cells"], int)
    # A cleanly ruled table is reliable and needs no split.
    assert tables[0]["columns_reliable"] is True
    assert tables[0]["split_cells"] == 0


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


def test_empty_header_does_not_promote_a_one_cell_section_row():
    """A section label is not a column header, however sparse the real one.

    Starbucks p36's real column header (the fiscal-year dates) sits above
    the detected table bbox, so `find_tables` returns an all-empty header
    and 'Net revenues:' as row 0. With an empty header, `2 * filled(header)`
    is zero, so the sparse-caption rule promoted that single section cell as
    the column header -- a wrong claim that 'Net revenues:' labels a column.
    A header names two or more columns; one filled cell is a section band.
    """
    from pdf_mcp.server import _resolve_header

    empty = ["", "", "", "", "", "", "", ""]
    rows = [
        ["Net revenues:", "", "", "", "", "", "", ""],
        ["Licensed stores", "2,575.6", "", "2,747.4", "", "9.4", "", "10.2"],
    ]
    header, body = _resolve_header(empty, rows)
    assert header == empty, "a one-cell section row must not become the header"
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

    with _tf.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
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
        assert any("4.5" in c for row in ctx["rows"] for c in row)
        assert ctx["columns_reliable"] is True


@pytest.fixture
def wrapped_label_table_pdf():
    """A datasheet-shaped table whose row LABEL is its own text block.

    The 2x3 `ruled_table_pdf` cannot express this: its rows are so narrow
    that PyMuPDF returns each whole row as one block, so a label-only match
    does not exist there. Here the label wraps inside a narrow first column
    and the values sit far right, which is what splits them apart -- the
    real shape of the MCP1700 rows behind p01 and p02.
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.close()  # see above: Windows open-handle rule
        doc = pymupdf.open()
        page = doc.new_page()
        page.draw_rect(pymupdf.Rect(50, 50, 520, 182), color=(0, 0, 0))
        for x in (200, 270, 340, 410, 460):
            page.draw_line(pymupdf.Point(x, 50), pymupdf.Point(x, 182), color=(0, 0, 0))
        for y in (83, 116, 149):
            page.draw_line(pymupdf.Point(50, y), pymupdf.Point(520, y), color=(0, 0, 0))
        for x, label in (
            (55, "Parameters"),
            (205, "Sym."),
            (275, "Min."),
            (345, "Typ."),
            (415, "Max."),
            (465, "Units"),
        ):
            page.insert_text((x, 70), label)
        page.insert_textbox(
            pymupdf.Rect(53, 86, 198, 114),
            "Power Supply Ripple Rejection Ratio",
            fontsize=9,
        )
        for x, cell in (
            (205, "PSRR"),
            (275, "-"),
            (345, "44"),
            (415, "-"),
            (465, "dB"),
        ):
            page.insert_text((x, 105), cell)
        page.insert_textbox(
            pymupdf.Rect(53, 119, 198, 147),
            "Thermal Shutdown Protection",
            fontsize=9,
        )
        for x, cell in ((205, "TSD"), (275, "-"), (345, "140"), (415, "-"), (465, "C")):
            page.insert_text((x, 138), cell)
        doc.save(f.name)
        doc.close()
    yield f.name
    unlink_quietly(f.name)


def test_a_bare_row_label_inside_the_rules_earns_extraction(wrapped_label_table_pdf):
    """A label carrying no number is invisible to every textual route.

    'Thermal Shutdown Protection' names no table and holds no value, so the
    excerpt says nothing; only the rules above and below the block say it is
    a table row. The removed textual route died trying to read this from the
    words.
    """
    import pathlib
    import tempfile as _tf

    from pdf_mcp.cache import PDFCache
    from pdf_mcp.server import _attach_table_context, _match_may_touch_a_table

    with _tf.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cache = PDFCache(cache_dir=pathlib.Path(tmp))
        doc = pymupdf.open(wrapped_label_table_pdf)
        blocks = doc[0].get_text("blocks")
        label = next(b for b in blocks if b[4].strip() == "Thermal Shutdown Protection")
        excerpt = label[4].strip()
        # The point of the test: no textual route can see this block.
        assert _match_may_touch_a_table(excerpt) is False
        match = {"page": 1, "excerpt": excerpt, "bbox": list(label[:4])}
        out = _attach_table_context([match], wrapped_label_table_pdf, cache, doc)
        doc.close()

    ctx = out[0].get("table_context")
    assert ctx is not None, "a ruled block must reach extraction"
    assert "Min." in ctx["header"]
    assert any("140" in c for row in ctx["rows"] for c in row)


def test_prose_beside_a_ruled_table_still_spawns_nothing(monkeypatch, ruled_table_pdf):
    """The guarantee is per match, so the ruled page must not condemn it.

    The block sits on the same page as a fully ruled table and below it,
    outside the rules. Reading the signal off the PAGE rather than the
    BLOCK would spawn here.
    """
    import pathlib
    import tempfile as _tf

    from pdf_mcp import server as srv
    from pdf_mcp.cache import PDFCache

    called = []
    monkeypatch.setattr(
        srv,
        "extract_tables_for_pages",
        lambda *a, **k: called.append(a) or {"tables": {}},
    )
    with _tf.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cache = PDFCache(cache_dir=pathlib.Path(tmp))
        doc = pymupdf.open(ruled_table_pdf)
        match = {
            "page": 1,
            "excerpt": "no numbers here at all",
            "bbox": [50.0, 400.0, 300.0, 420.0],
        }
        srv._attach_table_context([match], ruled_table_pdf, cache, doc)
        doc.close()
    assert called == []


def test_no_subprocess_for_unambiguous_matches(monkeypatch, ruled_table_pdf):
    """A prose search must cost nothing."""
    from pdf_mcp import server as srv
    from pdf_mcp.cache import PDFCache
    import pathlib
    import tempfile as _tf

    called = []
    monkeypatch.setattr(
        srv,
        "extract_tables_for_pages",
        lambda *a, **k: called.append(a) or {"tables": {}},
    )
    with _tf.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
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

    with _tf.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cache = PDFCache(cache_dir=pathlib.Path(tmp))
        match = {"page": 1, "excerpt": "Supply Voltage 4.5 16 V"}
        out = _attach_table_context([match], ruled_table_pdf, cache)
    assert "table_context" not in out[0]


def test_whole_table_block_returns_every_row_not_one_guess(ruled_table_pdf):
    """A block covering the table yields the header and ALL its rows.

    PyMuPDF returns some tables as ONE text block (Berkshire 2024 p134: a
    202pt block over 16 rows). Picking the row at the bbox centre returned
    an arbitrary wrong row; demanding exactly one row returned nothing on
    any document whose blocks are not per-row. The rows are already in the
    excerpt, so the header plus every covered row resolves the ambiguity
    without guessing which row was meant.
    """
    from pdf_mcp.cache import PDFCache
    from pdf_mcp.server import _attach_table_context
    import pathlib
    import tempfile as _tf

    with _tf.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cache = PDFCache(cache_dir=pathlib.Path(tmp))
        # No column-identity word, or the ambiguity trigger would reject it
        # before geometry is ever consulted and the test would pass vacuously.
        match = {
            "page": 1,
            "excerpt": "Supply Voltage 4.5 16 V Reset Voltage 0.4 1.0 V",
            "bbox": [50.0, 50.0, 300.0, 150.0],
        }
        out = _attach_table_context([match], ruled_table_pdf, cache)
    ctx = out[0]["table_context"]
    # Both rows come back: no single row is presented as THE answer.
    assert len(ctx["rows"]) == 2
    flat = [c for row in ctx["rows"] for c in row]
    assert "4.5" in flat and "0.4" in flat


def test_context_rows_are_capped(ruled_table_pdf):
    """A match covering a huge table must not bloat the response."""
    from pdf_mcp.server import _MAX_CONTEXT_ROWS

    assert _MAX_CONTEXT_ROWS == 20


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


def test_thousands_grouped_numbers_are_one_token():
    """ "4,350.4" is one value, not two.

    The tokeniser was \\d+(?:\\.\\d+)? so a comma-grouped figure read as
    ['4', '350.4']. That made a clean financial cell look merged and a
    single-value excerpt look ambiguous, on every financial or government
    table that groups thousands.
    """
    from pdf_mcp.server import _columns_reliable, _excerpt_is_ambiguous

    # One clean value per cell: the columns are fine.
    assert _columns_reliable([["Licensed stores", "4,350.4", "4,505.1"]]) is True
    # One value, so nothing to disambiguate.
    assert _excerpt_is_ambiguous("Licensed stores 4,350.4") is False
    # The datasheet contract must survive unchanged.
    assert _columns_reliable([["Reset Voltage", "0.4 0.5 1", "V"]]) is False
    assert _excerpt_is_ambiguous("Reset Voltage | 0.4 | 0.5 | 1 | V") is True


def test_overlapping_rows_reports_every_covered_row():
    """Row selection reports all covered rows, not a single choice."""
    from pdf_mcp.server import _rows_overlapping

    rows = [
        [43.0, 100.0, 551.0, 113.0],
        [43.0, 113.0, 551.0, 126.0],
        [43.0, 126.0, 551.0, 139.0],
    ]
    # A tall match covering all three reports all three.
    assert _rows_overlapping([46.0, 99.0, 551.0, 140.0], rows) == [0, 1, 2]
    # A match the size of one row reports just that row.
    assert _rows_overlapping([46.0, 114.0, 551.0, 125.0], rows) == [1]


def test_single_row_detections_merge_back_into_one_table():
    """find_tables splits some tables into one detection per row.

    Starbucks 2025 p34: eight detections, one carrying the real header
    plus a section row, the rest a single data row each. Because
    row_count is 1, extracted[0] was filed as `header` and `rows` came
    back empty, so a caller saw 8 tables whose data was in the wrong
    field. They belong to one table and must be reassembled.
    """
    from pdf_mcp.extractor import _merge_single_row_detections

    raw = [
        {
            "bbox": [43.0, 99.0, 551.0, 132.6],
            "extracted": [
                ["", "Sep 28,\n2025", "", "Sep 29,\n2024", "", "%\nChange"],
                ["Net revenues:", "", "", "", "", ""],
            ],
            "row_bboxes": [[43.0, 99.0, 551.0, 119.6], [43.0, 119.6, 551.0, 132.6]],
        },
        {
            "bbox": [43.0, 145.6, 551.0, 158.6],
            "extracted": [["Licensed stores", "4,350.4", "", "4,505.1", "", "(3.4)"]],
            "row_bboxes": [[43.0, 145.6, 551.0, 158.6]],
        },
        {
            "bbox": [43.0, 171.6, 551.0, 184.6],
            "extracted": [
                ["Total net revenues", "37,184.4", "", "36,176.2", "", "2.8%"]
            ],
            "row_bboxes": [[43.0, 171.6, 551.0, 184.6]],
        },
    ]
    merged = _merge_single_row_detections(raw)
    assert len(merged) == 1
    t = merged[0]
    assert t["extracted"][0][1] == "Sep 28,\n2025"  # real header kept
    body = t["extracted"][1:]
    assert ["Licensed stores", "4,350.4", "", "4,505.1", "", "(3.4)"] in body
    assert ["Total net revenues", "37,184.4", "", "36,176.2", "", "2.8%"] in body
    assert len(t["row_bboxes"]) == len(t["extracted"])  # stays index-aligned


def test_unfragmented_tables_are_left_alone():
    """A single well-formed detection must pass through untouched."""
    from pdf_mcp.extractor import _merge_single_row_detections

    raw = [
        {
            "bbox": [50.0, 50.0, 300.0, 150.0],
            "extracted": [["Parameter", "Min", "Max"], ["Supply Voltage", "4.5", "16"]],
            "row_bboxes": [[50.0, 50.0, 300.0, 83.0], [50.0, 83.0, 300.0, 116.0]],
        }
    ]
    assert _merge_single_row_detections(raw) == raw


def test_packed_table_context_carries_a_render_clip():
    """When the columns are unreadable in text, point at the picture.

    TI LM555 renders MIN and MAX in visibly separate columns while both
    collapse into one cell as "4.5 16". The page has the answer; the text
    layer does not. Same call the chart extractor makes when it declines
    and returns a render instead of guessing.
    """
    from pdf_mcp.server import _context_for_match

    packed = [
        {
            "bbox": [54.1, 101.1, 557.9, 300.0],
            "header": ["PARAMETER", "MIN", "TYP", "MAX", "UNIT"],
            "rows": [["Supply Voltage", "4.5 16", "", "", "V"]],
            "row_bboxes": [[54.1, 114.0, 557.9, 127.0]],
        }
    ]
    match = {"bbox": [54.1, 115.0, 300.0, 126.0]}
    ctx = _context_for_match(match, packed, page_rect=[0.0, 0.0, 612.0, 792.0])
    assert ctx["columns_reliable"] is False
    # The caller is handed the region to render, not left guessing.
    assert ctx["bbox"] == [54.1, 101.1, 557.9, 300.0]
    assert len(ctx["clip"]) == 4
    assert all(0.0 <= v <= 1.0 for v in ctx["clip"])


def test_clean_table_context_carries_no_clip():
    """A readable table needs no picture, so the field stays absent."""
    from pdf_mcp.server import _context_for_match

    clean = [
        {
            "bbox": [54.1, 101.1, 557.9, 300.0],
            "header": ["PARAMETER", "MIN", "MAX", "UNIT"],
            "rows": [["Supply Voltage", "4.5", "16", "V"]],
            "row_bboxes": [[54.1, 114.0, 557.9, 127.0]],
        }
    ]
    match = {"bbox": [54.1, 115.0, 300.0, 126.0]}
    ctx = _context_for_match(match, clean, page_rect=[0.0, 0.0, 612.0, 792.0])
    assert ctx["columns_reliable"] is True
    assert "clip" not in ctx and "bbox" not in ctx


def test_match_beside_a_table_is_associated_with_it():
    """A caption or nearby block associates with the table it labels.

    Forensics on all 19 retrieval failures: the answer sits in a VALUE
    block scoring 0-4 query tokens while the winner is a label, caption
    or prose block scoring 3-6. No token-overlap scorer can prefer the
    value block, which is why three re-ranking designs failed. Geometry
    can: 12 of the 19 have the winner inside, or within 60pt of, the
    table whose rows hold the answer.
    """
    from pdf_mcp.server import _table_near_match

    table = {"bbox": [50.0, 200.0, 500.0, 400.0]}
    # Caption sitting just above the table.
    assert _table_near_match([50.0, 170.0, 400.0, 190.0], table) is True
    # A block inside the table.
    assert _table_near_match([60.0, 250.0, 300.0, 262.0], table) is True
    # Just below it.
    assert _table_near_match([50.0, 410.0, 400.0, 430.0], table) is True
    # Far above: a different part of the page, not this table.
    assert _table_near_match([50.0, 60.0, 400.0, 80.0], table) is False


def test_trigger_fires_for_a_caption_with_no_numbers():
    """A caption never has two numbers, so the old trigger never fired.

    'Table 3: Variations on the Transformer architecture' is the single
    largest failure bucket (7 of 19) and carries no value at all.
    """
    from pdf_mcp.server import _excerpt_wants_table_context

    cap = "Table 3: Variations on the Transformer architecture."
    assert _excerpt_wants_table_context(cap, near_table=True) is True
    # Prose far from any table must still cost nothing.
    assert (
        _excerpt_wants_table_context(
            "we vary the number of attention heads", near_table=False
        )
        is False
    )
    # The original ambiguity route is unchanged.
    assert (
        _excerpt_wants_table_context(
            "Reset Voltage | 0.4 | 0.5 | 1 | V", near_table=False
        )
        is True
    )


def test_cached_tables_allow_attachment_without_re_extracting(
    monkeypatch, ruled_table_pdf
):
    """A page whose tables are already cached costs nothing to associate.

    The pre-filter exists only to avoid SPAWNING on prose searches. When
    a page's tables are already in the cache there is no spawn to avoid,
    so a bare row label like 'Timing Error, Monostable' can be
    associated with its table for free. Five of the fifteen remaining
    failures are blocked by the pre-filter alone, not by geometry.
    """
    import pathlib
    import tempfile as _tf

    from pdf_mcp import server as srv
    from pdf_mcp.cache import PDFCache

    with _tf.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cache = PDFCache(cache_dir=pathlib.Path(tmp))
        # Warm the cache the way a prior read would.
        tables = extract_tables_for_pages(ruled_table_pdf, [0])["tables"]["0"]
        cache.save_page_tables(ruled_table_pdf, 0, tables)

        doc = pymupdf.open(ruled_table_pdf)
        re_extracted = []
        monkeypatch.setattr(
            srv,
            "extract_tables_for_pages",
            lambda *a, **k: re_extracted.append(a) or {"tables": {}},
        )
        # A bare label: no 2+ numbers, no "Table N". The old pre-filter
        # dropped it outright.
        match = {
            "page": 1,
            "excerpt": "Supply Voltage",
            "bbox": [55.0, 100.0, 128.0, 112.0],
        }
        out = srv._attach_table_context([match], ruled_table_pdf, cache, doc)
        doc.close()
    assert re_extracted == [], "must not spawn: the tables were already cached"
    assert "table_context" in out[0]


def test_number_token_and_columns_reliable_live_in_extractor():
    from pdf_mcp import extractor, server

    assert extractor._NUMBER_TOKEN.findall("4,350.4 and 16") == ["4,350.4", "16"]
    assert extractor._columns_reliable([["ok", "1"]]) is True
    assert extractor._columns_reliable([["4.5 16"]]) is False
    # server must re-use the extractor definitions, not keep its own copies.
    assert server._NUMBER_TOKEN is extractor._NUMBER_TOKEN
    assert server._columns_reliable is extractor._columns_reliable
