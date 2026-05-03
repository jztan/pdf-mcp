# tests/test_benchmark_sections.py
"""Unit tests for scripts/benchmark_sections.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import benchmark_sections as bs  # noqa: E402


class TestSectionDataclass:
    def test_section_holds_title_pages_and_text(self):
        s = bs.Section(title="Intro", start_page=1, end_page=3, text="hello world")
        assert s.title == "Intro"
        assert s.start_page == 1
        assert s.end_page == 3
        assert s.text == "hello world"

    def test_section_is_immutable_friendly_for_dict_keys_via_title_pages(self):
        # Sanity: two Sections with same fields compare equal
        a = bs.Section(title="X", start_page=1, end_page=2, text="t")
        b = bs.Section(title="X", start_page=1, end_page=2, text="t")
        assert a == b


class TestStripAnsi:
    def test_strips_color_codes(self):
        assert bs._strip_ansi("\x1b[31mred\x1b[0m") == "red"

    def test_passthrough_plain_text(self):
        assert bs._strip_ansi("plain") == "plain"
