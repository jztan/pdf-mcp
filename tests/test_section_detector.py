"""Unit tests for src/pdf_mcp/section_detector.py."""

from pdf_mcp import section_detector as sd


class TestSectionDataclass:
    def test_section_holds_title_pages_and_text(self):
        s = sd.Section(title="Intro", start_page=1, end_page=3, text="hello world")
        assert s.title == "Intro"
        assert s.start_page == 1
        assert s.end_page == 3
        assert s.text == "hello world"

    def test_section_is_immutable_friendly_for_dict_keys_via_title_pages(self):
        # Sanity: two Sections with same fields compare equal
        a = sd.Section(title="X", start_page=1, end_page=2, text="t")
        b = sd.Section(title="X", start_page=1, end_page=2, text="t")
        assert a == b


class TestFilterToLeaves:
    """Tests _filter_to_leaves — drops parent containers in nested TOC."""

    def test_drops_parent_with_children(self):
        # 'Intro' (p1-4) contains '1.1' (p2-2) and '1.2' (p3-4) — drop Intro
        sections = [
            sd.Section("Intro", 1, 4, ""),
            sd.Section("1.1 Bg", 2, 2, ""),
            sd.Section("1.2 Mot", 3, 4, ""),
            sd.Section("Body", 5, 10, ""),
        ]
        leaves = sd._filter_to_leaves(sections)
        titles = [s.title for s in leaves]
        assert titles == ["1.1 Bg", "1.2 Mot", "Body"]

    def test_flat_partition_passes_through(self):
        # Already-flat sections — no parents to remove
        sections = [
            sd.Section("A", 1, 5, ""),
            sd.Section("B", 6, 10, ""),
            sd.Section("C", 11, 15, ""),
        ]
        leaves = sd._filter_to_leaves(sections)
        assert leaves == sections

    def test_deeply_nested_keeps_only_innermost(self):
        # 1 (p1-10) > 1.1 (p1-5) > 1.1.1 (p1-3) — only 1.1.1 is a leaf
        sections = [
            sd.Section("1", 1, 10, ""),
            sd.Section("1.1", 1, 5, ""),
            sd.Section("1.1.1", 1, 3, ""),
            sd.Section("1.1.2", 4, 5, ""),
            sd.Section("1.2", 6, 10, ""),
        ]
        leaves = sd._filter_to_leaves(sections)
        titles = [s.title for s in leaves]
        assert titles == ["1.1.1", "1.1.2", "1.2"]

    def test_empty_input(self):
        assert sd._filter_to_leaves([]) == []


class TestDetectBoundariesPure:
    """Tests _detect_boundaries_from_lines — the pure function that takes
    a list of (page_num, line_text) tuples and applies the heading regex,
    bypassing PDF I/O. The PDF-opening wrapper detect_boundaries is
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
        sections = sd._detect_boundaries_from_lines(lines, total_pages=3)
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
        sections = sd._detect_boundaries_from_lines(lines, total_pages=8)
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
        sections = sd._detect_boundaries_from_lines(lines, total_pages=1)
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
        sections = sd._detect_boundaries_from_lines(lines, total_pages=5)
        # First section ends at start_page_of_next - 1 = 0 (malformed); set
        # dedup in F1 collapses both starts on page 1 to a single boundary.
        assert [s.title for s in sections] == ["1 Intro", "1.1 Background", "2 Body"]
        assert sections[0].start_page == 1
        assert sections[1].start_page == 1
        assert sections[2].start_page == 3
        assert sections[2].end_page == 5

    def test_empty_input_returns_empty_list(self):
        assert sd._detect_boundaries_from_lines([], total_pages=10) == []

    def test_no_headings_returns_empty_list(self):
        lines = [(1, "Pure prose with no numbered headings"), (2, "More prose")]
        assert sd._detect_boundaries_from_lines(lines, total_pages=2) == []

    def test_unnumbered_academic_headings_fire(self):
        # First calibration showed the detector missed Abstract / References /
        # Acknowledgments / Appendix headings, dragging recall down to 0.22 on
        # the LLM survey. These standalone forms must now match.
        lines = [
            (1, "Abstract"),
            (1, "abstract concepts are useful"),  # body sentence — no match
            (5, "References"),
            (6, "Acknowledgments"),
            (7, "Acknowledgements"),  # British spelling
            (8, "Bibliography"),
            (9, "Appendix A"),
            (10, "Appendix B Theorems and Proofs"),
        ]
        sections = sd._detect_boundaries_from_lines(lines, total_pages=12)
        titles = [s.title for s in sections]
        assert "Abstract" in titles
        assert "References" in titles
        assert "Acknowledgments" in titles
        assert "Acknowledgements" in titles
        assert "Bibliography" in titles
        assert "Appendix A" in titles
        assert "Appendix B Theorems and Proofs" in titles
        # Body sentence containing "abstract" must NOT match — \s*$ anchor
        # rejects anything other than the bare standalone word.
        assert "abstract concepts are useful" not in titles

    def test_lowercase_appendix_does_not_fire(self):
        # "Appendix" without an uppercase letter following is not a heading
        # ("the appendix discusses" — body prose with leading "the").
        # Also "appendix a" (all lowercase) shouldn't fire because the [A-Z]
        # guard requires uppercase after the keyword.
        lines = [
            (1, "the appendix discusses"),
            (1, "appendix a is small"),  # lowercase — body prose
        ]
        sections = sd._detect_boundaries_from_lines(lines, total_pages=1)
        assert sections == []


class TestDetectBoundariesIntegration:
    """End-to-end test: build a synthetic two-column PDF, run the real
    detect_boundaries (which opens the PDF and uses PyMuPDF), and assert
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
        sections = sd.detect_boundaries(path)
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
            f"Verify detect_boundaries uses get_text('blocks', sort=True)."
        )

    def test_section_text_spans_correct_page_range(self, tmp_path):
        path = self._build_two_column_pdf(tmp_path)
        sections = sd.detect_boundaries(path)
        intro = next(s for s in sections if s.title == "1 Introduction")
        # Intro is on page 1; next heading is on page 2 → end_page = 1
        assert intro.end_page == 1
        # Section text should contain page 1's content
        assert "Introduction" in intro.text or "intro" in intro.text.lower()


class TestBodyFingerprint:
    """Tests _compute_body_fingerprint — finds the most common
    (font_name, is_bold) tuple across all lines in a document."""

    def test_picks_most_common_font_face(self):
        lines = [
            {"spans": [{"font": "Body", "flags": 0, "text": "x"}]},
            {"spans": [{"font": "Body", "flags": 0, "text": "y"}]},
            {"spans": [{"font": "Body", "flags": 0, "text": "z"}]},
            {"spans": [{"font": "Heading", "flags": 16, "text": "h"}]},
        ]
        assert sd._compute_body_fingerprint(lines) == ("Body", False)

    def test_uses_dominant_span_per_line(self):
        # Line with mixed spans — dominant = longest text
        lines = [
            {
                "spans": [
                    {"font": "Body", "flags": 0, "text": "x" * 100},
                    {"font": "Italic", "flags": 2, "text": "i"},
                ]
            },
            {"spans": [{"font": "Body", "flags": 0, "text": "y"}]},
        ]
        assert sd._compute_body_fingerprint(lines) == ("Body", False)

    def test_skips_empty_lines(self):
        lines = [
            {"spans": []},
            {"spans": [{"font": "Body", "flags": 0, "text": ""}]},
            {"spans": [{"font": "Body", "flags": 0, "text": "real"}]},
        ]
        assert sd._compute_body_fingerprint(lines) == ("Body", False)

    def test_empty_input_returns_none(self):
        assert sd._compute_body_fingerprint([]) is None

    def test_recognizes_bold_via_flag(self):
        # When body is itself bold (rare), fingerprint should reflect that
        lines = [
            {"spans": [{"font": "X", "flags": 16, "text": "a"}]},
            {"spans": [{"font": "X", "flags": 16, "text": "b"}]},
            {"spans": [{"font": "Y", "flags": 0, "text": "c"}]},
        ]
        assert sd._compute_body_fingerprint(lines) == ("X", True)


class TestLineFeatures:
    """Tests _line_features — extracts the 7 weak signals from a line."""

    def _make_line(self, text, font="Body", flags=0, y0=100, y1=110):
        return {
            "spans": [{"font": font, "flags": flags, "text": text, "size": 10}],
            "bbox": [50, y0, 500, y1],
        }

    def test_face_delta_fires_on_different_face(self):
        body_fp = ("Body", False)
        line = self._make_line("Heading", font="Heading", flags=16)
        f = sd._line_features(line, body_fp, prev_line=None, page_height=800)
        assert f["face_delta"] is True
        assert f["bold_marker"] is True

    def test_bold_via_font_name_marker(self):
        # Some PDFs encode bold in the font name (e.g., AdvTTc9617e0c.B)
        # without setting the flag bit
        body_fp = ("Body", False)
        line = self._make_line("X", font="AdvTTc9617e0c.B", flags=0)
        f = sd._line_features(line, body_fp, prev_line=None, page_height=800)
        assert f["bold_marker"] is True

    def test_bold_via_name_marker_for_dash_b(self):
        body_fp = ("Body", False)
        line = self._make_line("X", font="NimbusSanL-Bold", flags=0)
        f = sd._line_features(line, body_fp, prev_line=None, page_height=800)
        assert f["bold_marker"] is True

    def test_no_bold_when_face_matches_body_and_no_flag(self):
        body_fp = ("Body", False)
        line = self._make_line("body text", font="Body", flags=0)
        f = sd._line_features(line, body_fp, prev_line=None, page_height=800)
        assert f["face_delta"] is False
        assert f["bold_marker"] is False

    def test_whitespace_above_first_line(self):
        # First line of a page (prev_line=None) gets whitespace_above=True
        body_fp = ("Body", False)
        line = self._make_line("X")
        f = sd._line_features(line, body_fp, prev_line=None, page_height=800)
        assert f["whitespace_above"] is True

    def test_whitespace_above_threshold(self):
        body_fp = ("Body", False)
        prev = self._make_line("prev", y0=100, y1=110)  # baseline at y=110
        # Gap: current y0=145; line_height ≈ 10; gap = 35 ≥ 1.5*10=15 → fires
        far = self._make_line("X", y0=145, y1=155)
        f = sd._line_features(far, body_fp, prev_line=prev, page_height=800)
        assert f["whitespace_above"] is True

    def test_no_whitespace_above_when_close(self):
        body_fp = ("Body", False)
        prev = self._make_line("prev", y0=100, y1=110)
        # Gap: y0=115; line_height=10; gap=5 < 15 → does not fire
        near = self._make_line("X", y0=115, y1=125)
        f = sd._line_features(near, body_fp, prev_line=prev, page_height=800)
        assert f["whitespace_above"] is False

    def test_top_of_page_within_first_15_percent(self):
        body_fp = ("Body", False)
        # page_height=800, top 15% = y < 120
        top = self._make_line("X", y0=50, y1=60)
        bottom = self._make_line("X", y0=400, y1=410)
        ft = sd._line_features(top, body_fp, prev_line=None, page_height=800)
        fb = sd._line_features(bottom, body_fp, prev_line=None, page_height=800)
        assert ft["top_of_page"] is True
        assert fb["top_of_page"] is False

    def test_regex_match_numbered(self):
        body_fp = ("Body", False)
        line = self._make_line("1.1 Background")
        f = sd._line_features(line, body_fp, prev_line=None, page_height=800)
        assert f["regex_match"] is True

    def test_regex_match_chapter_keyword(self):
        body_fp = ("Body", False)
        line = self._make_line("Chapter 3")
        f = sd._line_features(line, body_fp, prev_line=None, page_height=800)
        assert f["regex_match"] is True

    def test_title_case_recognition(self):
        body_fp = ("Body", False)
        title = self._make_line("Background for LLMs")
        f = sd._line_features(title, body_fp, prev_line=None, page_height=800)
        assert f["title_case_or_caps"] is True

    def test_all_caps_recognition(self):
        body_fp = ("Body", False)
        caps = self._make_line("INTRODUCTION")
        f = sd._line_features(caps, body_fp, prev_line=None, page_height=800)
        assert f["title_case_or_caps"] is True

    def test_lowercase_prose_not_title_case(self):
        body_fp = ("Body", False)
        prose = self._make_line("the cat sat on the mat")
        f = sd._line_features(prose, body_fp, prev_line=None, page_height=800)
        assert f["title_case_or_caps"] is False

    def test_short_line_threshold(self):
        body_fp = ("Body", False)
        short = self._make_line("Short heading")
        long_line = self._make_line("x" * 200)
        fs = sd._line_features(short, body_fp, prev_line=None, page_height=800)
        fl = sd._line_features(long_line, body_fp, prev_line=None, page_height=800)
        assert fs["short_line"] is True
        assert fl["short_line"] is False


class TestHeadingScore:
    """Tests _heading_score and _is_heading."""

    def _features(self, **overrides):
        defaults = {
            "face_delta": False,
            "bold_marker": False,
            "whitespace_above": False,
            "top_of_page": False,
            "regex_match": False,
            "title_case_or_caps": False,
            "short_line": False,
        }
        defaults.update(overrides)
        return defaults

    def test_zero_signals(self):
        assert sd._heading_score(self._features()) == 0
        assert sd._is_heading(self._features()) is False

    def test_regex_alone_insufficient(self):
        # regex (3) alone is not enough — needs at least one supporting signal
        f = self._features(regex_match=True)
        assert sd._heading_score(f) == 3
        assert sd._is_heading(f) is False

    def test_regex_plus_short_line_clears_threshold(self):
        f = self._features(regex_match=True, short_line=True)
        assert sd._heading_score(f) == 4
        assert sd._is_heading(f) is True

    def test_face_delta_plus_bold_marker_clears_threshold(self):
        # 2 + 2 = 4
        f = self._features(face_delta=True, bold_marker=True)
        assert sd._heading_score(f) == 4
        assert sd._is_heading(f) is True

    def test_inline_bold_label_below_threshold(self):
        # Body face + bold flag (e.g. "**Note:**") → bold_marker(2) + short_line(1) = 3
        f = self._features(bold_marker=True, short_line=True)
        assert sd._heading_score(f) == 3
        assert sd._is_heading(f) is False

    def test_strong_combo_top_of_page_heading(self):
        # face_delta + bold_marker + whitespace + top_of_page + title_case + short
        f = self._features(
            face_delta=True,
            bold_marker=True,
            whitespace_above=True,
            top_of_page=True,
            title_case_or_caps=True,
            short_line=True,
        )
        assert sd._heading_score(f) == 8
        assert sd._is_heading(f) is True

    def test_threshold_constant_is_at_module_level(self):
        # Assert the threshold is exposed for tuning/calibration
        assert hasattr(sd, "HEADING_SCORE_THRESHOLD")
        assert sd.HEADING_SCORE_THRESHOLD == 4


class TestMultilineHeadingMerge:
    """Tests _merge_split_headings — joins a number-only line with the
    immediately-following title-text line into one heading candidate."""

    def test_number_line_followed_by_title_merges(self):
        candidates = [
            (1, "1", 100),  # (page, text, y_position)
            (1, "Introduction", 130),
            (3, "2", 100),
            (3, "Methods", 130),
        ]
        merged = sd._merge_split_headings(candidates, max_y_gap=50)
        assert merged == [
            (1, "1 Introduction", 100),
            (3, "2 Methods", 100),
        ]

    def test_far_apart_lines_do_not_merge(self):
        # Y gap > max means they're not part of the same heading
        candidates = [
            (1, "1", 100),
            (1, "Some other thing", 500),  # far below
        ]
        merged = sd._merge_split_headings(candidates, max_y_gap=50)
        assert merged == [
            (1, "1", 100),
            (1, "Some other thing", 500),
        ]

    def test_different_pages_do_not_merge(self):
        # Number line on page 1, title-like line on page 2 → no merge
        candidates = [
            (1, "1", 100),
            (2, "Introduction", 100),
        ]
        merged = sd._merge_split_headings(candidates, max_y_gap=50)
        assert merged == [
            (1, "1", 100),
            (2, "Introduction", 100),
        ]

    def test_non_number_lines_pass_through(self):
        # Already-complete heading lines aren't touched
        candidates = [
            (1, "1.1 Background", 100),
            (5, "References", 100),
        ]
        merged = sd._merge_split_headings(candidates, max_y_gap=50)
        assert merged == [
            (1, "1.1 Background", 100),
            (5, "References", 100),
        ]
