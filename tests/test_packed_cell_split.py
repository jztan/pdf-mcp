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
