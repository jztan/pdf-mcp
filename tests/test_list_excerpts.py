"""Paragraph excerpts on list pages: a lead-in plus its bullets is one unit.

The answer to a question is often one item of a bulleted list, while the
lead-in sentence that introduces the list (or the list as a whole) is what
shares the query's words. These tests pin the list detection and the
widening rule in `_upgrade_excerpts_to_paragraphs`.
"""

from typing import Any

from pdf_mcp.extractor import find_list_groups, list_item_kind
from pdf_mcp.tools._search_common import _upgrade_excerpts_to_paragraphs

FOOTER = "This publication is available free of charge from: https://example.org"


class _Rect:
    x0, y0, x1, y1 = 0.0, 0.0, 612.0, 792.0


class _Page:
    """Serves the (x0, y0, x1, y1, text, block_no, type) blocks shape."""

    def __init__(self, texts: list[str]) -> None:
        self.rect = _Rect()
        self._blocks = [
            (50.0, 40.0 + 60 * i, 400.0, 90.0 + 60 * i, t, i, 0)
            for i, t in enumerate(texts)
        ]

    def get_text(self, kind: str = "text", **_kw: Any) -> Any:
        assert kind == "blocks"
        return self._blocks


class _Doc:
    """No `name`, so `_layout_page` serves these pages directly."""

    name = None

    def __init__(self, pages: list[list[str]]) -> None:
        self._pages = [_Page(p) for p in pages]

    def __len__(self) -> int:
        return len(self._pages)

    def __getitem__(self, i: int) -> _Page:
        return self._pages[i]


def _upgrade(pages: list[list[str]], query: str, page: int = 1) -> dict:
    doc = _Doc(pages)
    matches = [{"page": page, "excerpt": "x", "score": 1.0}]
    (out,) = _upgrade_excerpts_to_paragraphs(matches, doc, query)
    return out


class TestListItemKind:
    def test_glyph_and_numbered(self):
        assert list_item_kind("• item") == "glyph"
        assert list_item_kind("•\nitem on the next line") == "glyph"
        assert list_item_kind("1. first") == "numbered"
        assert list_item_kind("(iv) fourth") == "numbered"
        assert list_item_kind("a) option") == "numbered"

    def test_prose_is_not_an_item(self):
        assert list_item_kind("Lenders vary significantly.") is None
        assert list_item_kind("2023 was a year of change.") is None


class TestFindListGroups:
    def test_colon_lead_in_with_items(self):
        texts = ["Heading", "Factors to consider:", "• one", "• two", "Prose after."]
        assert find_list_groups(texts) == [[1, 2, 3]]

    def test_glyph_items_need_no_colon(self):
        texts = ["Three kinds of code exist.", "• firmware", "• mobile apps"]
        assert find_list_groups(texts) == [[0, 1, 2]]

    def test_numbered_items_need_a_colon_lead_in(self):
        """Page-bottom footnotes open like a numbered list."""
        texts = ["Body paragraph ends here.", "7. Note that the sites", "8. See"]
        assert find_list_groups(texts) == []
        texts[0] = "The tenets are:"
        assert find_list_groups(texts) == [[0, 1, 2]]

    def test_running_footer_inside_list_is_skipped(self):
        texts = ["Consider:", "• one", FOOTER, "• two", "Prose."]
        # Unrecognised, the footer splits the list and poses as a lead-in.
        assert find_list_groups(texts) == [[0, 1], [2, 3]]
        skippable = {" ".join(FOOTER.split())}
        assert find_list_groups(texts, skippable) == [[0, 1, 3]]

    def test_footer_after_last_item_ends_the_list(self):
        texts = ["Consider:", "• one", FOOTER, "Prose."]
        assert find_list_groups(texts, {FOOTER}) == [[0, 1]]

    def test_two_lists_on_a_page(self):
        texts = ["First list:", "• a", "• b", "Second list:", "• c"]
        assert find_list_groups(texts) == [[0, 1, 2], [3, 4]]


LEAD = (
    "Once a list of candidate business processes has been developed,"
    " enterprise architects can compose a list of candidate solutions."
    " These are some factors to consider:"
)
ITEMS = [
    "• Does the solution require that components be installed on the client?",
    "• Does the solution provide a means to log interactions for analysis?",
    "• Does the solution require changes to subject behavior?",
]


class TestUpgradeWidensToList:
    def test_lead_in_winner_returns_the_whole_list(self):
        """z08 shape: the lead-in shares the query's words, the item holds
        the answer; the excerpt must reach the item."""
        out = _upgrade(
            [[LEAD, *ITEMS, "Unrelated closing prose about pilots."]],
            "candidate solutions to consider for logging",
        )
        assert "log interactions for analysis" in out["excerpt"]
        assert out["excerpt"].startswith("Once a list")
        # bbox is the union of lead-in and items
        assert out["bbox"][1] == 40.0
        assert out["bbox"][3] == 90.0 + 60 * 3

    def test_list_beats_outside_block_only_on_strictly_more_tokens(self):
        """f02 shape: a prose block above wins a single item, but the list
        as a unit covers more of the query."""
        prose = (
            "Three of the websites convey information about product costs"
            " using nonstandard terminology such as a factor rate."
        )
        lead = "Comments from the focus group participants:"
        quotes = [
            "• “It is difficult to compare when they use different models.”",
            "• “They don't like the word interest, and they dress it up.”",
        ]
        out = _upgrade(
            [[prose, lead, *quotes]],
            "focus group quote about lenders avoiding the word interest",
        )
        assert "dress it up" in out["excerpt"]

    def test_outside_block_is_kept_on_a_tie(self):
        prose = "The focus group discussed the word choice at length here."
        lead = "Participants said:"
        quotes = ["• “The word is avoided.”", "• “Hard to compare.”"]
        out = _upgrade([[prose, lead, *quotes]], "focus group word")
        assert out["excerpt"] == prose

    def test_chosen_item_is_not_widened(self):
        """A picked list item already is the specific answer."""
        items = [
            "• A grapevine insurance program available in select counties;",
            "• A kiwifruit insurance program available in 12 counties;",
        ]
        out = _upgrade(
            [["New product lines included:", *items]],
            "where is the kiwifruit insurance program available",
        )
        assert out["excerpt"] == items[1].strip()

    def test_running_footer_mid_list_is_not_in_the_excerpt(self):
        page = [LEAD, ITEMS[0], FOOTER, ITEMS[1], ITEMS[2]]
        neighbour = ["Other page body text.", FOOTER]
        out = _upgrade(
            [neighbour, page, neighbour],
            "candidate solutions to consider for logging",
            page=2,
        )
        assert "log interactions for analysis" in out["excerpt"]
        assert "publication is available" not in out["excerpt"]

    def test_list_over_the_cap_is_not_returned(self):
        long_items = [f"• item {i} " + "word " * 120 for i in range(4)]
        out = _upgrade([["Factors to consider:", *long_items]], "factors to consider")
        assert out["excerpt"] == "Factors to consider:"

    def test_page_without_lists_is_unchanged(self):
        out = _upgrade(
            [["Alpha beta gamma delta epsilon.", "Unrelated cooking text."]],
            "alpha gamma",
        )
        assert out["excerpt"] == "Alpha beta gamma delta epsilon."
