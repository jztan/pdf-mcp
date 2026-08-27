"""Header-anchored packed-cell split: the five rules, in isolation."""

from pdf_mcp.backend.tables import _fragment_value_shaped, _reassign_packed_cell

# Param, Min, Max, Unit header x-ranges (pdfplumber raw page space).
R4 = [(50.0, 130.0), (130.0, 190.0), (190.0, 245.0), (245.0, 300.0)]
L4 = ["Parameter", "Min", "Max", "Unit"]

# Param, Min, Typ, Max, Unit.
R5 = [(50.0, 120.0), (120.0, 160.0), (160.0, 200.0), (200.0, 245.0), (245.0, 300.0)]
L5 = ["Parameter", "Min", "Typ", "Max", "Unit"]


def test_clean_split_min_max():
    row = ["Supply Voltage", "4.5 16", None, "V"]
    words = [(145.0, "4.5"), (207.0, "16")]  # 4.5 -> Min, 16 -> Max
    assert _reassign_packed_cell(words, R4, L4, row, src_col=1) == {1: "4.5", 2: "16"}


def test_assignment_follows_geometry_not_order():
    # "3 6" belongs under TYP and MAX; MIN (the packed source column) empties.
    row = ["Supply Current", "3 6", None, None, "mA"]
    words = [(180.0, "3"), (222.0, "6")]  # 3 -> Typ(160-200), 6 -> Max(200-245)
    assert _reassign_packed_cell(words, R5, L5, row, src_col=1) == {2: "3", 3: "6"}


def test_rule2_word_straddling_two_columns_refuses():
    overlapping = [(50.0, 150.0), (140.0, 250.0)]  # 140-150 shared
    words = [(145.0, "4.5"), (200.0, "16")]  # 4.5 centre lands in both boxes
    assert (
        _reassign_packed_cell(words, overlapping, ["A", "B"], ["", "4.5 16"], 1) is None
    )


def test_rule3_stacked_value_under_one_header_refuses():
    words = [(145.0, "2.0"), (145.0, "1.0")]  # both -> Min, single target
    assert (
        _reassign_packed_cell(words, R4, L4, ["cond", "2.0 1.0", None, "V"], 1) is None
    )


def test_rule4_unnamed_target_refuses():
    labels = ["Parameter", "Min", "", "Unit"]  # Max header is a colspan blank
    words = [(145.0, "4.5"), (207.0, "16")]
    assert (
        _reassign_packed_cell(words, R4, labels, ["p", "4.5 16", None, "V"], 1) is None
    )


def test_rule4_occupied_target_refuses():
    row = ["p", "4.5 16", "99", "V"]  # Max already holds a value
    words = [(145.0, "4.5"), (207.0, "16")]
    assert _reassign_packed_cell(words, R4, L4, row, src_col=1) is None


def test_rule5_prose_fragment_refuses():
    # Two numbers present, but one target fragment carries prose.
    words = [(145.0, "2.3"), (207.0, "supply")]
    assert (
        _reassign_packed_cell(words, R4, L4, ["p", "2.3 supply", None, "V"], 1) is None
    )


def test_fragment_value_shaped():
    assert _fragment_value_shaped("4.5") is True
    assert _fragment_value_shaped("-65") is True
    assert _fragment_value_shaped("0.75 x VDD") is True  # VDD is a <=4-char unit
    assert _fragment_value_shaped("power supply (2.3") is False


class _FakeRow:
    def __init__(self, cells):
        self.cells = cells


class _FakeTable:
    def __init__(self, rows):
        self.rows = [_FakeRow(r) for r in rows]


class _FakePage:
    def __init__(self, words):
        self._words = words

    def extract_words(self, x_tolerance=1.5):
        return self._words


def test_rollback_on_mid_loop_exception_leaves_cells_untouched():
    from pdf_mcp.backend.tables import _split_packed_cells

    # Header row boxes (Parameter, Min, Max, Unit) matching R4/L4.
    header_boxes = [
        (50.0, 0.0, 130.0, 10.0),
        (130.0, 0.0, 190.0, 10.0),
        (190.0, 0.0, 245.0, 10.0),
        (245.0, 0.0, 300.0, 10.0),
    ]
    # Row 1's Min cell box is WIDE (130-245): it visually merges Min+Max,
    # which is why pdfplumber packed two numbers into one cell. Clean,
    # splittable packed cell.
    row1_boxes = [
        (50.0, 20.0, 130.0, 30.0),
        (130.0, 20.0, 245.0, 30.0),
        None,
        (245.0, 20.0, 300.0, 30.0),
    ]
    # Row 2's Min cell box is likewise wide (so its packed words are
    # gathered, not skipped by the "box missing" guard), but its TEXT row
    # is ragged (fewer cells than the header). _reassign_packed_cell
    # indexes row_cells[col] for every target column, so once assignment
    # reaches a target column beyond the short text row it raises
    # IndexError, inside the try/except of _split_packed_cells -- AFTER
    # row 1 has already been rewritten.
    row2_boxes = [
        (50.0, 40.0, 130.0, 50.0),
        (130.0, 40.0, 245.0, 50.0),
        None,
        (245.0, 40.0, 300.0, 50.0),
    ]

    table = _FakeTable([header_boxes, row1_boxes, row2_boxes])

    original_cells = [
        ["Parameter", "Min", "Max", "Unit"],
        ["Supply Voltage", "4.5 16", None, "V"],
        ["Supply Current", "3 6"],  # ragged: only 2 cells, header has 4
    ]
    cells = [list(row) for row in original_cells]

    words = [
        {"x0": 144.0, "x1": 146.0, "top": 21.0, "bottom": 29.0, "text": "4.5"},
        {"x0": 206.0, "x1": 208.0, "top": 21.0, "bottom": 29.0, "text": "16"},
        {"x0": 179.0, "x1": 181.0, "top": 41.0, "bottom": 49.0, "text": "3"},
        {"x0": 221.0, "x1": 223.0, "top": 41.0, "bottom": 49.0, "text": "6"},
    ]
    page = _FakePage(words)

    result_cells, split_count = _split_packed_cells(page, table, cells)

    assert split_count == 0
    assert result_cells == original_cells
    # And the caller's own list object must not have been mutated either.
    assert cells == original_cells
