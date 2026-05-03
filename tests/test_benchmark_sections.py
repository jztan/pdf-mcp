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


class TestBoundaryF1:
    def test_perfect_match_scores_one(self):
        gold = [bs.Section("A", 1, 5, ""), bs.Section("B", 6, 10, "")]
        detected = [bs.Section("A", 1, 0, ""), bs.Section("B", 6, 0, "")]
        m = bs._compute_boundary_f1(gold, detected, tolerance=1)
        assert m == {
            "precision": 1.0,
            "recall": 1.0,
            "f1": 1.0,
            "tp": 2,
            "fp": 0,
            "fn": 0,
            "n_gold": 2,
            "n_detected": 2,
        }

    def test_off_by_one_within_tolerance(self):
        gold = [bs.Section("A", 5, 0, "")]
        detected = [bs.Section("A", 6, 0, "")]
        m = bs._compute_boundary_f1(gold, detected, tolerance=1)
        assert m["f1"] == 1.0

    def test_off_by_two_outside_tolerance(self):
        gold = [bs.Section("A", 5, 0, "")]
        detected = [bs.Section("A", 7, 0, "")]
        m = bs._compute_boundary_f1(gold, detected, tolerance=1)
        assert m["precision"] == 0.0
        assert m["recall"] == 0.0
        assert m["f1"] == 0.0

    def test_duplicate_starts_dedupe_to_set(self):
        # Two TOC entries on the same page count as one gold boundary
        gold = [bs.Section("A", 5, 0, ""), bs.Section("B", 5, 0, "")]
        detected = [bs.Section("X", 5, 0, "")]
        m = bs._compute_boundary_f1(gold, detected, tolerance=0)
        # n_gold should be 1 (deduped), not 2
        assert m["n_gold"] == 1
        assert m["recall"] == 1.0
        assert m["precision"] == 1.0

    def test_extra_detection_lowers_precision(self):
        gold = [bs.Section("A", 5, 0, "")]
        detected = [bs.Section("A", 5, 0, ""), bs.Section("B", 50, 0, "")]
        m = bs._compute_boundary_f1(gold, detected, tolerance=1)
        assert m["recall"] == 1.0
        assert m["precision"] == 0.5
        assert abs(m["f1"] - (2 * 0.5 * 1.0 / 1.5)) < 1e-9

    def test_missing_detection_lowers_recall(self):
        gold = [bs.Section("A", 5, 0, ""), bs.Section("B", 50, 0, "")]
        detected = [bs.Section("A", 5, 0, "")]
        m = bs._compute_boundary_f1(gold, detected, tolerance=1)
        assert m["recall"] == 0.5
        assert m["precision"] == 1.0

    def test_empty_detected_returns_zero_f1(self):
        gold = [bs.Section("A", 5, 0, "")]
        m = bs._compute_boundary_f1(gold, [], tolerance=1)
        assert m == {
            "precision": 0.0,
            "recall": 0.0,
            "f1": 0.0,
            "tp": 0,
            "fp": 0,
            "fn": 1,
            "n_gold": 1,
            "n_detected": 0,
        }

    def test_empty_gold_returns_zero_recall(self):
        # Defensive: real callers should never pass empty gold (validated upstream).
        m = bs._compute_boundary_f1([], [bs.Section("A", 5, 0, "")], tolerance=1)
        assert m["recall"] == 0.0
        assert m["precision"] == 0.0
        assert m["f1"] == 0.0


class TestBoilerplateStripping:
    def test_strips_lines_appearing_on_majority_of_pages(self):
        pages = [
            "GPT-3 Technical Report\nPage 1 content here\nFooter line",
            "GPT-3 Technical Report\nPage 2 content here\nFooter line",
            "GPT-3 Technical Report\nPage 3 content here\nFooter line",
            "GPT-3 Technical Report\nPage 4 content here\nFooter line",
        ]
        boilerplate = bs._detect_boilerplate(pages, threshold=0.5)
        assert "GPT-3 Technical Report" in boilerplate
        assert "Footer line" in boilerplate
        assert "Page 1 content here" not in boilerplate

    def test_keeps_lines_below_threshold(self):
        pages = ["Header\nA", "Header\nB", "C\nD"]  # Header is on 2/3 pages
        boilerplate = bs._detect_boilerplate(pages, threshold=0.7)
        # 2/3 = 0.667 < 0.7 → not stripped
        assert "Header" not in boilerplate

    def test_normalizes_whitespace_before_counting(self):
        # Trailing whitespace should not split otherwise-identical headers
        pages = ["Header   \nA", "Header\nB", "Header\t\nC"]
        boilerplate = bs._detect_boilerplate(pages, threshold=0.5)
        assert "Header" in boilerplate

    def test_strips_paginated_page_numbers_with_changing_digits(self):
        # "Page 1 of 4", "Page 2 of 4", ... never match each other under exact
        # equality. The detector must collapse them into a page-number family.
        pages = [f"Title\nReal content {i}\nPage {i} of 4" for i in range(1, 5)]
        boilerplate = bs._detect_boilerplate(pages, threshold=0.5)
        # All four raw forms should land in the boilerplate set
        assert "Page 1 of 4" in boilerplate
        assert "Page 2 of 4" in boilerplate
        assert "Page 4 of 4" in boilerplate
        # Real content (which also contains a digit) is NOT swept up
        assert "Real content 1" not in boilerplate

    def test_strips_bare_page_numbers(self):
        # arxiv-style: bare "1", "2", "3" as page numbers in the footer
        pages = [f"Body text {i}\n{i}" for i in range(1, 6)]
        boilerplate = bs._detect_boilerplate(pages, threshold=0.5)
        assert "1" in boilerplate
        assert "5" in boilerplate
        # Body lines vary by digit but each appears only once → not boilerplate
        assert "Body text 3" not in boilerplate

    def test_does_not_strip_content_with_embedded_digits(self):
        # Lines like "GPT-3 Technical Report" appear on every page and should
        # be caught by the EXACT-match path, not the page-number family
        # (the line is not a pure "Page N" / "N of M" / bare "N" form).
        pages = ["GPT-3 Technical Report\nA"] * 4
        boilerplate = bs._detect_boilerplate(pages, threshold=0.5)
        assert "GPT-3 Technical Report" in boilerplate

    def test_strip_boilerplate_removes_lines(self):
        text = "Header\nReal content\nFooter"
        boilerplate = {"Header", "Footer"}
        assert bs._strip_boilerplate(text, boilerplate) == "Real content"

    def test_strip_boilerplate_idempotent_on_clean_text(self):
        text = "Real content only"
        assert bs._strip_boilerplate(text, set()) == "Real content only"


class TestTokenize:
    def test_lowercases_and_strips_punctuation(self):
        assert bs._tokenize("Hello, World!") == ["hello", "world"]

    def test_splits_on_whitespace(self):
        assert bs._tokenize("a  b\tc\nd") == ["a", "b", "c", "d"]

    def test_keeps_alphanumeric(self):
        assert bs._tokenize("GPT-3 has 175B params.") == [
            "gpt-3",
            "has",
            "175b",
            "params",
        ]


class TestNgrams:
    def test_5gram_set(self):
        tokens = ["a", "b", "c", "d", "e", "f"]
        # Two contiguous 5-grams: (a,b,c,d,e) and (b,c,d,e,f)
        result = bs._ngram_set(tokens, n=5)
        assert result == {("a", "b", "c", "d", "e"), ("b", "c", "d", "e", "f")}

    def test_dedup_repeats(self):
        tokens = ["x"] * 7
        # All 5-grams identical → set of size 1
        assert bs._ngram_set(tokens, n=5) == {("x", "x", "x", "x", "x")}

    def test_too_short_returns_empty(self):
        assert bs._ngram_set(["a", "b", "c"], n=5) == set()


class TestCoverageMetrics:
    def test_perfect_overlap(self):
        text = "the quick brown fox jumps over the lazy dog and runs away"
        m = bs._coverage_metrics(returned=text, gold=text)
        assert m["recall"] == 1.0
        assert m["precision"] == 1.0

    def test_returned_subset_of_gold(self):
        gold = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
        # First 6 words → 2 unique 5-grams; gold has 6 unique 5-grams
        returned = "alpha beta gamma delta epsilon zeta"
        m = bs._coverage_metrics(returned=returned, gold=gold)
        # recall = |intersection| / |gold ngrams| = 2/6
        assert abs(m["recall"] - 2 / 6) < 1e-9
        # precision = |intersection| / |returned ngrams| = 2/2
        assert m["precision"] == 1.0

    def test_disjoint_strings(self):
        m = bs._coverage_metrics(
            returned="aa bb cc dd ee ff",
            gold="zz yy xx ww vv uu",
        )
        assert m["recall"] == 0.0
        assert m["precision"] == 0.0

    def test_empty_returned_yields_zero(self):
        m = bs._coverage_metrics(returned="", gold="alpha beta gamma delta epsilon")
        assert m["recall"] == 0.0
        assert m["precision"] == 0.0

    def test_empty_gold_yields_zero(self):
        m = bs._coverage_metrics(returned="alpha beta gamma delta epsilon", gold="")
        assert m["recall"] == 0.0
        assert m["precision"] == 0.0


class TestSimulateAgentReads:
    """Tests _simulate_agent_reads with injectable page text — no PDF I/O."""

    def _page_provider(self, pages: dict[int, str]):
        """Return a callable(page_num)->str that errors on out-of-range."""

        def get_page(p: int) -> str:
            if p not in pages:
                raise IndexError(f"page {p} out of range")
            return pages[p]

        return get_page

    def test_zero_extra_reads_when_initial_page_covers(self):
        gold = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
        pages = {3: gold}  # initial hit covers everything
        get_page = self._page_provider(pages)
        gold_section = bs.Section("X", 1, 5, gold)
        reads = bs._simulate_agent_reads(
            initial_page=3,
            gold_section=gold_section,
            get_page=get_page,
            doc_total_pages=5,
        )
        assert reads == 0

    def test_walks_outward_alternating_forward_first(self):
        # Verifies the walk visits N+1 before N-1 (forward-first) and stops
        # as soon as token coverage clears the 95% target.
        # Gold has 3 unique tokens placed on consecutive pages 3, 4, 5 — so
        # the agent reads page 4 (forward) → 67% then page 2 (backward, no
        # new tokens) → 67% then page 5 (forward) → 100%. Stops at 3 reads.
        gold = "w1 w2 w3"
        pages = {
            1: "filler-a",
            2: "filler-b",
            3: "w1",  # initial hit
            4: "w2",  # forward neighbour, contributes w2
            5: "w3",  # second-forward, contributes w3 → 100%
        }
        get_page = self._page_provider(pages)
        gold_section = bs.Section("X", 1, 5, gold)
        reads = bs._simulate_agent_reads(
            initial_page=3,
            gold_section=gold_section,
            get_page=get_page,
            doc_total_pages=5,
        )
        # Walk order [4, 2, 5, 1]:
        #   start: {w1} → 33%
        #   +4: {w1,w2} → 67%
        #   +2: {w1,w2} → 67% (filler-b adds nothing relevant)
        #   +5: {w1,w2,w3} → 100% ≥ 95% → stop
        assert reads == 3

    def test_walk_order_traversal_when_coverage_never_reaches_target(self):
        # Pages contain only filler; coverage never reaches the target and the
        # agent walks the entire bounded order before the for-loop exhausts.
        # Walk from page 3 with doc_total=5 is [4, 2, 5, 1] → reads = 4.
        gold = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
        pages = {p: "filler " * 50 for p in range(1, 6)}
        pages[3] = "alpha beta"  # initial hit, 2 of 10 tokens = 20%
        get_page = self._page_provider(pages)
        gold_section = bs.Section("X", 1, 5, gold)
        reads = bs._simulate_agent_reads(
            initial_page=3,
            gold_section=gold_section,
            get_page=get_page,
            doc_total_pages=5,
        )
        assert reads == 4

    def test_caps_at_max_extra_reads(self):
        # Gold needs many pages but get_page never gets coverage to 95%
        gold = " ".join(f"w{i}" for i in range(200))
        # All pages return one irrelevant word
        pages = {i: "noise" for i in range(1, 30)}
        pages[5] = gold[:50]  # initial page has tiny fragment
        get_page = self._page_provider(pages)
        gold_section = bs.Section("X", 1, 30, gold)
        reads = bs._simulate_agent_reads(
            initial_page=5,
            gold_section=gold_section,
            get_page=get_page,
            doc_total_pages=30,
        )
        assert reads == bs.MAX_EXTRA_READS  # capped at 10

    def test_skips_out_of_bounds_pages_without_counting(self):
        # initial_page=1 means N-1=0 (out of bounds) — must be skipped silently
        gold_words = " ".join(f"w{i}" for i in range(1, 21))  # 20 unique tokens
        pages = {
            1: " ".join(f"w{i}" for i in range(1, 11)),  # w1..w10
            2: " ".join(f"w{i}" for i in range(11, 21)),  # w11..w20
        }
        get_page = self._page_provider(pages)
        gold_section = bs.Section("X", 1, 2, gold_words)
        reads = bs._simulate_agent_reads(
            initial_page=1,
            gold_section=gold_section,
            get_page=get_page,
            doc_total_pages=2,
        )
        # Page 1: 10/20 = 50%. Walks to N+1=2 → 20/20 = 100%. N-1=0 skipped.
        assert reads == 1


class TestDetectBoundariesPure:
    """Tests _detect_boundaries_from_lines — the pure function that takes
    a list of (page_num, line_text) tuples and applies the heading regex,
    bypassing PDF I/O. The PDF-opening wrapper _detect_boundaries is
    integration-tested separately at calibration time."""

    def test_numbered_heading_fires(self):
        lines = [
            (1, "Some intro paragraph here"),
            (1, "1 Introduction"),
            (1, "More intro text"),
            (2, "1.1 Background"),
            (2, "Background paragraph"),
            (3, "2 Methods"),
            (3, "Methods text"),
        ]
        sections = bs._detect_boundaries_from_lines(lines, total_pages=3)
        starts = [(s.title, s.start_page, s.end_page) for s in sections]
        assert starts == [
            ("1 Introduction", 1, 1),
            ("1.1 Background", 2, 2),
            ("2 Methods", 3, 3),
        ]

    def test_chapter_section_keyword_fires(self):
        lines = [
            (1, "Chapter 1 Introduction"),
            (1, "intro text"),
            (5, "Section 2 Body"),
            (5, "body text"),
        ]
        sections = bs._detect_boundaries_from_lines(lines, total_pages=8)
        starts = [(s.title, s.start_page, s.end_page) for s in sections]
        assert starts == [
            ("Chapter 1 Introduction", 1, 4),
            ("Section 2 Body", 5, 8),
        ]

    def test_non_heading_text_with_leading_digit_does_not_fire(self):
        # "1km of cable" or "100 widgets" — leading digit but no section structure
        lines = [
            (1, "Real heading"),  # not a heading per the regex
            (
                1,
                "1km of cable was used",
            ),  # leading digit but no period and not section-like
            (1, "100 widgets total"),
        ]
        sections = bs._detect_boundaries_from_lines(lines, total_pages=1)
        # No heading detected → empty list (caller must handle)
        assert sections == []

    def test_dedupes_consecutive_same_page_headings(self):
        # Two headings on the same page should produce two sections, but the
        # earlier section gets a malformed end_page that the F1 set-dedup
        # path collapses. Pure detector reports them faithfully.
        lines = [
            (1, "1 Intro"),
            (1, "1.1 Background"),
            (3, "2 Body"),
        ]
        sections = bs._detect_boundaries_from_lines(lines, total_pages=5)
        # First section ends at start_page_of_next - 1 = 0 (malformed); set
        # dedup in F1 collapses both starts on page 1 to a single boundary.
        assert [s.title for s in sections] == ["1 Intro", "1.1 Background", "2 Body"]
        assert sections[0].start_page == 1
        assert sections[1].start_page == 1
        assert sections[2].start_page == 3
        assert sections[2].end_page == 5

    def test_empty_input_returns_empty_list(self):
        assert bs._detect_boundaries_from_lines([], total_pages=10) == []

    def test_no_headings_returns_empty_list(self):
        lines = [(1, "Pure prose with no numbered headings"), (2, "More prose")]
        assert bs._detect_boundaries_from_lines(lines, total_pages=2) == []


class TestDetectBoundariesIntegration:
    """End-to-end test: build a synthetic two-column PDF, run the real
    _detect_boundaries (which opens the PDF and uses PyMuPDF), and assert
    the detected starts are in monotonic page order. This is the canary
    that catches column-interleaving surprises before calibration."""

    def _build_two_column_pdf(self, tmp_path):
        import pymupdf

        doc = pymupdf.open()
        # Page 1: left column = body prose, right column = section heading
        # then body. If get_text() reads naively top-to-bottom across both
        # columns, the heading "1 Introduction" appears AFTER body text.
        page1 = doc.new_page(width=600, height=800)
        # Left column body (x=50)
        page1.insert_text((50, 100), "This is left column prose.", fontsize=11)
        page1.insert_text((50, 130), "More left column body text here.", fontsize=11)
        # Right column heading (x=320)
        page1.insert_text((320, 100), "1 Introduction", fontsize=14)
        page1.insert_text(
            (320, 130), "Right column body for intro section.", fontsize=11
        )

        # Page 2: top-of-page heading, then body
        page2 = doc.new_page(width=600, height=800)
        page2.insert_text((50, 100), "2 Methods", fontsize=14)
        page2.insert_text((50, 130), "Methods body text.", fontsize=11)

        path = tmp_path / "two_column.pdf"
        doc.save(str(path))
        doc.close()
        return str(path)

    def test_detects_headings_in_monotonic_page_order(self, tmp_path):
        path = self._build_two_column_pdf(tmp_path)
        sections = bs._detect_boundaries(path)
        titles_pages = [(s.title, s.start_page) for s in sections]
        # Both headings detected, in correct page order
        assert ("1 Introduction", 1) in titles_pages
        assert ("2 Methods", 2) in titles_pages
        # Page order must be monotonic — Introduction (page 1) before Methods (page 2)
        intro_idx = next(
            i
            for i, (_, p) in enumerate(titles_pages)
            if titles_pages[i][0] == "1 Introduction"
        )
        methods_idx = next(
            i
            for i, (_, p) in enumerate(titles_pages)
            if titles_pages[i][0] == "2 Methods"
        )
        assert intro_idx < methods_idx, (
            f"Detected headings are out of page order — got {titles_pages}. "
            f"Likely cause: get_text() is not respecting column reading order. "
            f"Verify _detect_boundaries uses get_text('blocks', sort=True)."
        )

    def test_section_text_spans_correct_page_range(self, tmp_path):
        path = self._build_two_column_pdf(tmp_path)
        sections = bs._detect_boundaries(path)
        intro = next(s for s in sections if s.title == "1 Introduction")
        # Intro is on page 1; next heading is on page 2 → end_page = 1
        assert intro.end_page == 1
        # Section text should contain page 1's content
        assert "Introduction" in intro.text or "intro" in intro.text.lower()


class TestSectionSearch:
    def test_returns_section_containing_top_page_hit(self, monkeypatch):
        sections = [
            bs.Section("Intro", 1, 5, "intro text"),
            bs.Section("Methods", 6, 10, "methods text"),
            bs.Section("Results", 11, 15, "results text"),
        ]

        # Stub the keyword search to return page 8 as rank-1
        def fake_search(path, query, mode, max_results):
            return {"matches": [{"page": 8, "excerpt": ""}]}

        monkeypatch.setattr(bs, "_PDF_SEARCH_FN", fake_search)

        result = bs._section_search("p.pdf", "method", sections=sections, top_k=1)
        assert len(result["sections"]) == 1
        assert result["sections"][0]["title"] == "Methods"
        assert result["sections"][0]["start_page"] == 6
        assert result["sections"][0]["end_page"] == 10
        assert result["sections"][0]["text"] == "methods text"

    def test_returns_empty_when_no_keyword_hit(self, monkeypatch):
        sections = [bs.Section("Intro", 1, 5, "intro text")]
        monkeypatch.setattr(
            bs,
            "_PDF_SEARCH_FN",
            lambda path, query, mode, max_results: {"matches": []},
        )
        result = bs._section_search("p.pdf", "x", sections=sections, top_k=1)
        assert result["sections"] == []

    def test_returns_empty_when_hit_falls_outside_any_section(self, monkeypatch):
        # Detected section covers pages 6..10; keyword hit is on page 3.
        # No section contains page 3 → empty result.
        sections = [bs.Section("Methods", 6, 10, "methods text")]
        monkeypatch.setattr(
            bs,
            "_PDF_SEARCH_FN",
            lambda path, query, mode, max_results: {"matches": [{"page": 3}]},
        )
        result = bs._section_search("p.pdf", "x", sections=sections, top_k=1)
        assert result["sections"] == []

    def test_top_k_collects_distinct_sections(self, monkeypatch):
        # Three keyword hits on pages 2, 7, 8 → distinct sections Intro and Methods.
        # Methods appears twice (pages 7 and 8) but should be deduped.
        sections = [
            bs.Section("Intro", 1, 5, "intro text"),
            bs.Section("Methods", 6, 10, "methods text"),
        ]
        monkeypatch.setattr(
            bs,
            "_PDF_SEARCH_FN",
            lambda path, query, mode, max_results: {
                "matches": [
                    {"page": 2},
                    {"page": 7},
                    {"page": 8},
                ]
            },
        )
        result = bs._section_search("p.pdf", "x", sections=sections, top_k=2)
        titles = [s["title"] for s in result["sections"]]
        assert titles == ["Intro", "Methods"]


class TestRunBoundaryGroup:
    def test_returns_per_pdf_metrics(self, monkeypatch):
        # Mock _extract_toc_boundaries and _detect_boundaries to return controlled sections
        gold_pdf_a = [bs.Section("A", 1, 5, ""), bs.Section("B", 6, 10, "")]
        gold_pdf_b = [bs.Section("X", 1, 3, ""), bs.Section("Y", 4, 8, "")]

        def fake_extract(p):
            return gold_pdf_a if "a.pdf" in p else gold_pdf_b

        # Detector returns same as gold for PDF A, off-by-1 for PDF B (still passes ±1)
        def fake_detect(p):
            return (
                gold_pdf_a
                if "a.pdf" in p
                else [
                    bs.Section("X", 2, 0, ""),
                    bs.Section("Y", 5, 0, ""),
                ]
            )

        monkeypatch.setattr(bs, "_extract_toc_boundaries", fake_extract)
        monkeypatch.setattr(bs, "_detect_boundaries", fake_detect)

        pdfs = [
            {"key": "a", "title": "PDF A", "url": "a.pdf", "_local_path": "a.pdf"},
            {"key": "b", "title": "PDF B", "url": "b.pdf", "_local_path": "b.pdf"},
        ]
        result = bs.run_boundary_group(pdfs)
        assert result["per_pdf"]["a"]["f1"] == 1.0
        assert result["per_pdf"]["b"]["f1"] == 1.0  # within ±1 tolerance
        assert "min_f1" in result
        assert result["min_f1"] == 1.0

    def test_detects_off_by_two_failure(self, monkeypatch):
        gold = [bs.Section("A", 5, 0, "")]
        monkeypatch.setattr(bs, "_extract_toc_boundaries", lambda p: gold)
        monkeypatch.setattr(
            bs,
            "_detect_boundaries",
            lambda p: [bs.Section("A", 7, 0, "")],  # off by 2
        )
        pdfs = [{"key": "x", "title": "X", "url": "x.pdf", "_local_path": "x.pdf"}]
        result = bs.run_boundary_group(pdfs)
        assert result["per_pdf"]["x"]["f1"] == 0.0
        assert result["min_f1"] == 0.0
