"""Search hit context: outline section path and table lead-in (issue #66)."""

from pdf_mcp.hit_context import build_outline, lead_in, section_path


def _block(y0, y1, text, x0=72.0, x1=540.0):
    return (x0, float(y0), x1, float(y1), text, 0, 0)


# --- build_outline ---------------------------------------------------------


def test_outline_drops_unusable_entries():
    toc = [
        [1, "Intro", 1],
        [1, "", 2],  # empty title
        [1, "Ghost", -1],  # no destination
        [1, "Past the end", 99],
        [1, "Results", 3],
    ]
    titles = [e.title for e in build_outline(toc, page_count=10)]
    assert titles == ["Intro", "Results"]


def test_outline_end_is_next_entry_at_same_or_higher_level():
    toc = [[1, "A", 1], [2, "A.1", 2], [2, "A.2", 4], [1, "B", 6], [1, "C", 8]]
    ends = {e.title: (e.start, e.end) for e in build_outline(toc, page_count=9)}
    assert ends["A"] == (1, 5)
    assert ends["A.1"] == (2, 3)
    assert ends["A.2"] == (4, 5)


def test_outline_same_page_siblings_cover_one_page():
    # JPM FY2023 p116: two level-4 entries start on one page, so the first
    # gets end = 116 - 1 = 115 < start. It still covers its own page.
    toc = [[3, "Banking", 116], [4, "Selected data", 116], [4, "Compared", 116]]
    e = {x.title: (x.start, x.end) for x in build_outline(toc, page_count=200)}
    assert e["Selected data"] == (116, 116)


def test_outline_open_tail_is_capped():
    # The last entry at a level runs to the end only because nothing follows
    # it; unlisted back matter (References) must not inherit it.
    toc = [[1, "1 Intro", 1], [1, "5 Conclusion", 9]]
    e = {x.title: (x.start, x.end) for x in build_outline(toc, page_count=30)}
    assert e["1 Intro"] == (1, 8)
    assert e["5 Conclusion"] == (9, 11)


# --- section_path ----------------------------------------------------------


def _jpm_like():
    toc = [
        [1, "Annual Report 2023", 1],
        [2, "Management's discussion and analysis", 86],
        [3, "CONSUMER & COMMUNITY BANKING", 106],
        [4, "Selected income statement data", 106],
        [3, "COMMERCIAL BANKING", 116],
        [4, "Selected income statement data (CB)", 116],
        [4, "2023 compared with 2022", 116],
        [2, "Consolidated statements of income", 204],
        [2, "Notes to consolidated financial statements", 210],
        [3, "Note 34 - Business combinations", 340],
        [2, "Glossary", 360],
        [2, "Back cover", 364],
    ]
    return build_outline(toc, page_count=364)


def test_section_path_nests_and_drops_a_whole_document_root():
    blocks = [
        _block(40, 55, "CONSUMER & COMMUNITY BANKING"),
        _block(60, 70, "Selected income statement data"),
        _block(80, 300, "Total net revenue 70,148 54,814"),
    ]
    assert section_path(_jpm_like(), 364, 106, 80.0, blocks) == [
        "Management's discussion and analysis",
        "CONSUMER & COMMUNITY BANKING",
        "Selected income statement data",
    ]


def test_section_path_distinguishes_the_consolidated_statement():
    blocks = [_block(40, 55, "Consolidated statements of income")]
    assert section_path(_jpm_like(), 364, 204, 100.0, blocks) == [
        "Consolidated statements of income"
    ]


def test_section_path_picks_the_heading_printed_nearest_above_the_hit():
    # Outline order lists the table's heading first, but on the page the
    # prose heading is printed above the table heading.
    blocks = [
        _block(40, 55, "COMMERCIAL BANKING"),
        _block(60, 70, "2023 compared with 2022"),
        _block(80, 240, "Net income was $6.1 billion, up 46%."),
        _block(250, 262, "Selected income statement data (CB)"),
        _block(270, 400, "Total net revenue 15,546"),
    ]
    outline = _jpm_like()
    assert section_path(outline, 364, 116, 100.0, blocks)[-1] == (
        "2023 compared with 2022"
    )
    assert section_path(outline, 364, 116, 300.0, blocks)[-1] == (
        "Selected income statement data (CB)"
    )


def test_section_path_ignores_a_heading_that_starts_below_the_hit():
    # Note 34 starts mid-page 340; a hit above it is still in the prior note.
    toc = [[1, "Notes", 300], [2, "Note 33 - Parent", 330], [2, "Note 34", 340]]
    outline = build_outline(toc + [[1, "Appendix", 400]], page_count=500)
    blocks = [
        _block(40, 200, "Parent company balance sheet 1,000"),
        _block(300, 312, "Note 34"),
    ]
    assert section_path(outline, 500, 340, 50.0, blocks) == [
        "Notes",
        "Note 33 - Parent",
    ]
    assert section_path(outline, 500, 340, 320.0, blocks) == ["Notes", "Note 34"]


def test_a_deeper_heading_printed_before_a_shallower_one_is_closed():
    toc = [
        [1, "B Algorithm Details", 17],
        [2, "B.4 Backward Pass", 20],
        [2, "B.5 Comparison", 22],
        [1, "C Proofs", 22],
        [1, "D Experiments", 30],
    ]
    outline = build_outline(toc, page_count=40)
    blocks = [
        _block(40, 52, "B.5 Comparison"),
        _block(60, 200, "We compare with prior work in detail here."),
        _block(210, 222, "C Proofs"),
        _block(230, 400, "Proof of Theorem 1."),
    ]
    assert section_path(outline, 40, 22, 100.0, blocks) == [
        "B Algorithm Details",
        "B.5 Comparison",
    ]
    assert section_path(outline, 40, 22, 300.0, blocks) == ["C Proofs"]


def test_section_path_without_geometry_stops_above_an_ambiguous_level():
    blocks = [
        _block(40, 55, "COMMERCIAL BANKING"),
        _block(60, 70, "2023 compared with 2022"),
        _block(250, 262, "Selected income statement data (CB)"),
    ]
    assert section_path(_jpm_like(), 364, 116, None, blocks) == [
        "Management's discussion and analysis",
        "COMMERCIAL BANKING",
    ]


def test_section_path_keeps_a_heading_it_cannot_locate_on_the_page():
    blocks = [_block(40, 200, "Total net revenue 70,148")]
    path = section_path(_jpm_like(), 364, 106, 100.0, blocks)
    assert path[-1] == "Selected income statement data"


def test_section_path_keeps_at_most_three_levels():
    toc = [[1, "A", 1], [2, "B", 1], [3, "C", 1], [4, "D", 1], [1, "Z", 50]]
    outline = build_outline(toc, page_count=60)
    assert section_path(outline, 60, 1, None, []) == ["B", "C", "D"]


def test_section_path_clips_long_titles():
    long_title = "Segment results (a) " + "footnote text " * 20
    outline = build_outline([[1, long_title, 1], [1, "Next", 5]], page_count=40)
    (title,) = section_path(outline, 40, 2, None, [])
    assert len(title) <= 103 and title.endswith("...")


def test_section_path_absent_without_outline_or_outside_it():
    assert section_path([], 10, 3, 50.0, []) is None
    outline = build_outline([[1, "Intro", 5], [1, "End", 6]], page_count=20)
    assert section_path(outline, 20, 2, 50.0, []) is None


# --- lead_in ---------------------------------------------------------------

_PRO_FORMA = (
    "Following are the supplemental consolidated financial results on an"
    " unaudited pro forma basis, as if the acquisition had been consummated:"
)


def _table_page(between):
    return (
        [
            _block(
                40, 70, "Following is the net impact of the acquisition since the date:"
            )
        ]
        + [_block(80, 120, "Revenue $ 5,729 Operating loss (1,362)")]
        + [_block(130, 150, _PRO_FORMA)]
        + between
        + [_block(260, 320, "Revenue $ 247,442 $ 219,790 Net income 88,308 71,383")]
    )


def test_lead_in_fires_over_unit_and_year_bands():
    blocks = _table_page(
        [
            _block(160, 170, "(In millions, except per share amounts)"),
            _block(175, 185, "Year Ended June 30,"),
            _block(190, 200, "2024 2023"),
        ]
    )
    assert lead_in(blocks, [72.0, 262.0, 540.0, 318.0], "Revenue $ 247,442") == (
        _PRO_FORMA
    )


def test_a_short_lead_in_is_not_mistaken_for_furniture():
    lead = "The following table presents revenue by reportable segment:"
    blocks = [
        _block(40, 60, lead),
        _block(70, 80, "(In millions)"),
        _block(90, 140, "Cloud revenue 4,210 3,905"),
    ]
    assert lead_in(blocks, [72.0, 90.0, 540.0, 140.0], "Cloud revenue") == lead


def test_lead_in_does_not_cross_a_data_row():
    blocks = _table_page([_block(160, 170, "Revenue $ 5,729 Operating loss (1,362)")])
    assert lead_in(blocks, [72.0, 262.0, 540.0, 318.0], "Revenue $ 247,442") is None


def test_lead_in_skips_at_most_four_furniture_blocks():
    bands = [_block(155 + 10 * i, 160 + 10 * i, "(In millions)") for i in range(5)]
    assert lead_in(_table_page(bands), [72.0, 262.0, 540.0, 318.0], "x") is None


def test_lead_in_absent_for_a_prose_hit():
    prose = (
        "The change of content from third-party to first-party is reflected"
        " in the net impact shown above for the year."
    )
    blocks = [_block(40, 60, _PRO_FORMA), _block(70, 90, prose)]
    assert lead_in(blocks, [72.0, 70.0, 540.0, 90.0], prose) is None


def test_lead_in_rejects_footnotes_bullets_and_rules():
    table = _block(100, 160, "Due in one year $ 1,250 Due after 2,250")
    for intro in (
        "(1) The minimum payments table above excludes rent that was due:",
        "• In 2022, we committed $10 million over five years to programs:",
        "____ (1) North America segment assets primarily consist of these:",
    ):
        assert (
            lead_in([_block(40, 60, intro), table], [72.0, 100.0, 540.0, 160.0], "")
            is None
        )


def test_lead_in_absent_when_the_excerpt_already_holds_it():
    blocks = [_block(40, 60, _PRO_FORMA), _block(70, 120, "Revenue $ 247,442")]
    excerpt = _PRO_FORMA + "\nRevenue $ 247,442"
    assert lead_in(blocks, [72.0, 70.0, 540.0, 120.0], excerpt) is None


def test_lead_in_absent_without_geometry_or_colon():
    blocks = [
        _block(40, 60, "The following table presents revenue by segment."),
        _block(70, 120, "Revenue $ 247,442"),
    ]
    assert lead_in(blocks, [72.0, 70.0, 540.0, 120.0], "") is None
    assert lead_in(blocks, None, "") is None
