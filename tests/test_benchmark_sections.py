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


class TestExtractTocBoundariesPure:
    """Tests _toc_entries_to_sections — the pure function that takes
    a TOC list (no PDF I/O) and returns Sections without text filled in."""

    def test_flat_toc_two_entries(self):
        # Two top-level entries; first ends one before the second
        toc = [
            (1, "Intro", 1),
            (1, "Body", 5),
        ]
        result = bs._toc_entries_to_sections(toc, total_pages=10)
        assert result == [
            bs.Section(title="Intro", start_page=1, end_page=4, text=""),
            bs.Section(title="Body", start_page=5, end_page=10, text=""),
        ]

    def test_last_entry_extends_to_final_page(self):
        toc = [(1, "Only", 3)]
        result = bs._toc_entries_to_sections(toc, total_pages=10)
        assert result == [bs.Section(title="Only", start_page=3, end_page=10, text="")]

    def test_nested_subsection_does_not_close_parent(self):
        # 1 Intro (p1) > 1.1 Background (p2) > 1.2 Motivation (p3) > 2 Body (p5)
        # Intro should end at p4 (before Body), NOT at p1 (before its child).
        # 1.1 ends at p2 (before 1.2). 1.2 ends at p4 (before sibling at level<=2).
        toc = [
            (1, "Intro", 1),
            (2, "Background", 2),
            (2, "Motivation", 3),
            (1, "Body", 5),
        ]
        result = bs._toc_entries_to_sections(toc, total_pages=10)
        assert result == [
            bs.Section(title="Intro", start_page=1, end_page=4, text=""),
            bs.Section(title="Background", start_page=2, end_page=2, text=""),
            bs.Section(title="Motivation", start_page=3, end_page=4, text=""),
            bs.Section(title="Body", start_page=5, end_page=10, text=""),
        ]

    def test_four_level_hierarchy(self):
        # Critical case for the GNN review (4 levels).
        # 1 (p1) > 1.1 (p2) > 1.1.1 (p3) > 1.1.1.1 (p4) > 1.2 (p6) > 2 (p10)
        toc = [
            (1, "L1", 1),
            (2, "L2", 2),
            (3, "L3", 3),
            (4, "L4", 4),
            (2, "L2b", 6),
            (1, "L1b", 10),
        ]
        result = bs._toc_entries_to_sections(toc, total_pages=12)
        starts_ends = [(s.title, s.start_page, s.end_page) for s in result]
        assert starts_ends == [
            ("L1", 1, 9),
            ("L2", 2, 5),
            ("L3", 3, 5),
            ("L4", 4, 5),
            ("L2b", 6, 9),
            ("L1b", 10, 12),
        ]

    def test_consecutive_entries_on_same_page(self):
        # When two entries point at the same page, the earlier one has
        # end_page = start_page. Tests don't enforce 'duplicate detection' —
        # caller may dedupe boundary set later.
        toc = [
            (1, "A", 5),
            (1, "B", 5),
            (1, "C", 9),
        ]
        result = bs._toc_entries_to_sections(toc, total_pages=12)
        starts_ends = [(s.title, s.start_page, s.end_page) for s in result]
        assert starts_ends == [
            (
                "A",
                5,
                4,
            ),  # malformed (start>end); caller's job to filter;
            # helper reports faithfully
            ("B", 5, 8),
            ("C", 9, 12),
        ]

    def test_empty_toc_raises(self):
        import pytest

        with pytest.raises(ValueError, match="empty TOC"):
            bs._toc_entries_to_sections([], total_pages=10)
