"""Tests for byte-cap behavior on pdf_read_all and section-search."""

from pdf_mcp.server import _apply_byte_cap


class TestApplyByteCap:
    def test_returns_all_parts_when_under_cap(self):
        text, included, returned, available = _apply_byte_cap(
            ["abc", "def", "ghi"], cap=100
        )
        assert text == "abc\n\ndef\n\nghi"
        assert included == 3
        assert returned == len("abc\n\ndef\n\nghi".encode("utf-8"))
        assert available == returned

    def test_stops_at_part_boundary_when_over_cap(self):
        text, included, returned, available = _apply_byte_cap(
            ["aaaa", "bbbb", "cccc"], cap=6
        )
        # only first part fits: 4 bytes < 6, adding "\n\nbbbb" would push to 10
        assert text == "aaaa"
        assert included == 1
        assert returned == 4
        assert available == len("aaaa\n\nbbbb\n\ncccc".encode("utf-8"))

    def test_zero_parts(self):
        text, included, returned, available = _apply_byte_cap([], cap=100)
        assert text == ""
        assert included == 0
        assert returned == 0
        assert available == 0

    def test_multibyte_utf8_counted_in_bytes(self):
        # "日" is 3 UTF-8 bytes
        text, included, returned, available = _apply_byte_cap(
            ["日日日", "日日日"], cap=10
        )
        # First part: 9 bytes. Adding separator + second: would be 9+2+9=20.
        assert text == "日日日"
        assert included == 1
        assert returned == 9
        assert available == 20
