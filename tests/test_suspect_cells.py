"""Column-format check for OCR-layer table cells (suspect_cells)."""

from pdf_mcp.suspect_cells import decimal_key, find_suspect_cells


def _column(values, x=100.0, y0=100.0, pitch=12.0, width=12.0):
    """Word tuples (x0, y0, x1, y1, text) stacked as one column."""
    return [
        (x, y0 + i * pitch, x + width, y0 + i * pitch + 8.0, v)
        for i, v in enumerate(values)
    ]


class TestDecimalKey:
    def test_separator_and_fraction_digits(self):
        assert decimal_key("5.4") == (".", 1)
        assert decimal_key("15.25") == (".", 2)
        assert decimal_key("5-4") == ("-", 1)
        assert decimal_key("5·4") == ("·", 1)

    def test_integer_part_length_is_not_part_of_the_key(self):
        assert decimal_key("2.8") == decimal_key("15.3")

    def test_thousands_grouping_is_not_a_decimal(self):
        assert decimal_key("52,903") is None
        assert decimal_key("0,25") == (",", 2)

    def test_non_decimals(self):
        for text in ("54", "O.1", "5.4.1", "(1,835)", "5 4", ""):
            assert decimal_key(text) is None


class TestFindSuspectCells:
    def test_flags_cells_that_break_a_decimal_column(self):
        values = ["5.1", "4.9", "4-7", "4.6", "50", "5.4", "4.6", "O.4", "4.4"]
        flagged = [c["text"] for c in find_suspect_cells(_column(values))]
        assert flagged == ["4-7", "50", "O.4"]

    def test_reports_expected_format_and_bbox(self):
        values = ["5.1", "4.9", "4.7", "4.6", "5.0", "5.4", "4.6", "5-0"]
        (cell,) = find_suspect_cells(_column(values))
        assert cell["expected"] == "9.9"
        assert cell["bbox"] == [100.0, 184.0, 112.0, 192.0]

    def test_well_formed_wrong_digit_is_not_seen(self):
        # 6.4 printed as 5.4: the format is right, so the column check is
        # blind to it. Documented, not a bug.
        values = ["5.1", "4.9", "4.7", "4.6", "5.0", "6.4", "4.6", "5.0"]
        assert find_suspect_cells(_column(values)) == []

    def test_rejoins_a_cell_split_at_the_bad_mark(self):
        words = _column(["5.1", "4.9", "4.7", "4.6", "5.0", "5.4", "4.6", "5.0"])
        # "4-" and "6" on one line, a space apart: one misread cell.
        words += [(100.0, 196.0, 106.0, 204.0, "4-"), (108.0, 196.0, 112.0, 204.0, "6")]
        assert [c["text"] for c in find_suspect_cells(words)] == ["4-6"]

    def test_neighbouring_complete_cells_are_not_joined(self):
        left = _column(["5.1", "4.9", "4.7", "4.6", "5.0", "5.4", "4.6", "5.0"])
        right = _column(
            ["3.5", "3.0", "3.2", "3.1", "3.6", "3.9", "3.4", "3.4"], x=114.0
        )
        assert find_suspect_cells(left + right) == []

    def test_short_column_is_not_judged(self):
        assert find_suspect_cells(_column(["5.1", "4.9", "4-7", "4.6"])) == []

    def test_column_without_a_dominant_format_is_not_judged(self):
        values = ["5.1", "4-9", "4:7", "4.6", "5-0", "54", "4:6", "5.0"]
        assert find_suspect_cells(_column(values)) == []

    def test_financial_column_is_not_judged(self):
        values = ["45,273", "(1,835)", "2,368", "12,004", "9,870", "—", "310"]
        values += ["7,451", "61,002"]
        assert find_suspect_cells(_column(values, width=30.0)) == []
