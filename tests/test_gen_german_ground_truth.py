# tests/test_gen_german_ground_truth.py
"""Unit tests for scripts/gen_german_ground_truth.py.

All synthetic -- no PDF download, no real BGB.pdf. Mirrors the style of
tests/test_benchmark_cjk_keyword.py: pure functions over hand-built toc
lists and page-text dicts.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import gen_german_ground_truth as ggt  # noqa: E402


class TestPageBoilerplate:
    def test_strips_multiline_service_header(self):
        raw = (
            "Ein Service des Bundesministerium der Justiz und für "
            "Verbraucherschutz\nsowie des Bundesamts für Justiz "
            "‒ www.gesetze-im-internet.de\n(1) Der Vertrag kommt "
            "zustande."
        )
        cleaned = ggt._PAGE_BOILERPLATE_RE.sub(" ", raw)
        assert "gesetze-im-internet" not in cleaned
        assert "Der Vertrag kommt zustande." in cleaned

    def test_strips_page_footer(self):
        raw = "Text hier.\n- Seite 205 von 490 -"
        cleaned = ggt._PAGE_BOILERPLATE_RE.sub(" ", raw)
        assert "Seite" not in cleaned


class TestParseAnchors:
    def test_parses_simple_norm(self):
        toc = [[1, "§ 1\xa0Beginn der Rechtsfähigkeit", 26]]
        anchors = ggt.parse_anchors(toc)
        assert anchors == {"1": (26, "Beginn der Rechtsfähigkeit")}

    def test_parses_letter_suffixed_norm(self):
        toc = [[1, "§ 611a\xa0Arbeitsvertrag", 200]]
        anchors = ggt.parse_anchors(toc)
        assert anchors == {"611a": (200, "Arbeitsvertrag")}

    def test_drops_weggefallen(self):
        toc = [
            [1, "§ 2\xa0Eintritt der Volljährigkeit", 26],
            [1, "§ 88\xa0(weggefallen)", 30],
        ]
        skips = ggt.SkipLog()
        anchors = ggt.parse_anchors(toc, skips)
        assert "88" not in anchors
        assert skips.counts["weggefallen"] == 1

    def test_drops_range_entries(self):
        toc = [[1, "§§ 3 bis 6\xa0(weggefallen)", 26]]
        skips = ggt.SkipLog()
        anchors = ggt.parse_anchors(toc, skips)
        assert anchors == {}
        assert skips.counts["range_entry"] == 1

    def test_ignores_structural_entries(self):
        toc = [
            [1, "Buch 1", 25],
            [2, "Abschnitt 1", 25],
            [1, "§ 1\xa0Test", 26],
        ]
        anchors = ggt.parse_anchors(toc)
        assert list(anchors.keys()) == ["1"]

    def test_last_entry_wins_on_duplicate_number(self):
        toc = [[1, "§ 1\xa0Old rubric", 26], [1, "§ 1\xa0New rubric", 30]]
        anchors = ggt.parse_anchors(toc)
        assert anchors["1"] == (30, "New rubric")


class TestDeriveExtents:
    def test_end_page_from_immediately_following_entry(self):
        # §1 and §2 both on page 26 (common -- several norms per page);
        # §1's *next* entry (positionally) is §2, also on page 26.
        toc = [[1, "§ 1\xa0A", 26], [1, "§ 2\xa0B", 26], [1, "§ 3\xa0C", 27]]
        anchors = ggt.parse_anchors(toc)
        norms = ggt.derive_extents(toc, anchors)
        assert norms["1"].start_page == 26
        assert norms["1"].end_page == 26  # next entry same page -> no extra page
        assert norms["2"].end_page == 27  # next entry (§3) one page later

    def test_one_page_gap_yields_two_page_extent(self):
        toc = [[1, "§ 1\xa0A", 26], [1, "§ 2\xa0B", 27]]
        anchors = ggt.parse_anchors(toc)
        norms = ggt.derive_extents(toc, anchors)
        assert norms["1"].start_page == 26
        assert norms["1"].end_page == 27

    def test_large_gap_to_next_norm_is_capped_not_assumed(self):
        # Gap of 14 pages likely means intervening entries were filtered
        # out (e.g. weggefallen norms). The true end is unknown, so the
        # extent is capped at MAX_EXTENT_PAGES rather than claiming the
        # full gap.
        toc = [[1, "§ 1\xa0A", 26], [1, "§ 2\xa0B", 40]]
        anchors = ggt.parse_anchors(toc)
        norms = ggt.derive_extents(toc, anchors)
        assert norms["1"].end_page == 27  # capped, not 39
        assert norms["2"].end_page == 40  # last entry: no next, stays 1-page

    def test_structural_headings_are_skipped_when_finding_next_norm(self):
        # §1's positionally-next TOC entry is a structural "Buch 2"
        # heading far away; the next *norm* entry (§300) determines the
        # extent instead, so §1 isn't penalized for being near a Buch
        # boundary.
        toc = [
            [1, "§ 1\xa0A", 26],
            [1, "Buch 2 Recht der Schuldverhältnisse", 100],
            [1, "§ 300\xa0B", 105],
        ]
        anchors = ggt.parse_anchors(toc)
        norms = ggt.derive_extents(toc, anchors)
        assert norms["1"].end_page == 27  # capped, not 99

    def test_last_norm_uses_own_start_as_end(self):
        toc = [[1, "§ 1\xa0A", 26]]
        anchors = ggt.parse_anchors(toc)
        norms = ggt.derive_extents(toc, anchors)
        assert norms["1"].end_page == 26

    def test_duplicate_number_winning_occurrence_out_of_toc_order(self):
        # §5's *winning* (last) occurrence sits at TOC index 2, appearing
        # AFTER §6 (index 1) in outline order but at an earlier page (52
        # vs 55). Naive TOC-order "next entry" would make §6's next
        # entry §5 (page 52 < 55), wrongly shrinking §6's extent. Sorting
        # by page fixes this: §6's true next-by-page is §7 (page 60).
        toc = [
            [1, "§ 5\xa0Old", 50],
            [1, "§ 6\xa0B", 55],
            [1, "§ 5\xa0New", 52],  # duplicate: this occurrence wins
            [1, "§ 7\xa0C", 60],
        ]
        anchors = ggt.parse_anchors(toc)
        assert anchors["5"] == (52, "New")
        norms = ggt.derive_extents(toc, anchors)
        # §6's next-by-page is §7 (60): gap 5, capped at MAX_EXTENT_PAGES.
        assert norms["6"].end_page == 56
        # §5 (winning occurrence, page 52): next-by-page is §6 (55).
        assert norms["5"].end_page == 53


class TestValidateAnchor:
    def test_valid_when_citation_and_rubric_present(self):
        norm = ggt.Norm(
            number="1", rubric="Beginn der Rechtsfähigkeit", start_page=26, end_page=26
        )
        text = "§ 1 Beginn der Rechtsfähigkeit des Menschen beginnt mit ..."
        assert ggt.validate_anchor(norm, text) is True

    def test_rejects_missing_citation(self):
        norm = ggt.Norm(
            number="1", rubric="Beginn der Rechtsfähigkeit", start_page=26, end_page=26
        )
        text = "Beginn der Rechtsfähigkeit des Menschen beginnt mit ..."
        skips = ggt.SkipLog()
        assert ggt.validate_anchor(norm, text, skips) is False
        assert skips.counts["citation_not_on_own_page"] == 1

    def test_rejects_missing_rubric_word(self):
        norm = ggt.Norm(
            number="1", rubric="Beginn der Rechtsfähigkeit", start_page=26, end_page=26
        )
        text = "§ 1 hat nichts mit dem Titel zu tun."
        skips = ggt.SkipLog()
        assert ggt.validate_anchor(norm, text, skips) is False
        assert skips.counts["rubric_not_on_own_page"] == 1

    def test_rejects_prefix_collision_with_a_longer_number(self):
        # A plain substring check would let "§ 9" pass on a page whose
        # only citation is "§ 90" -- a different norm entirely.
        norm = ggt.Norm(number="9", rubric="Test", start_page=1, end_page=1)
        text = "§ 90 regelt etwas anderes. Test steht hier auch."
        skips = ggt.SkipLog()
        assert ggt.validate_anchor(norm, text, skips) is False
        assert skips.counts["citation_not_on_own_page"] == 1

    def test_rejects_prefix_collision_with_a_letter_suffixed_number(self):
        # "§ 611" should not validate against a page whose only citation
        # is "§ 611a" -- a different norm.
        norm = ggt.Norm(number="611", rubric="Test", start_page=1, end_page=1)
        text = "§ 611a regelt etwas anderes. Test steht hier auch."
        skips = ggt.SkipLog()
        assert ggt.validate_anchor(norm, text, skips) is False

    def test_accepts_exact_number_not_followed_by_more_digits(self):
        norm = ggt.Norm(number="9", rubric="Test", start_page=1, end_page=1)
        text = "§ 9 Test regelt etwas."
        assert ggt.validate_anchor(norm, text) is True

    def test_accepts_letter_suffixed_number_exactly(self):
        norm = ggt.Norm(number="611a", rubric="Test", start_page=1, end_page=1)
        text = "§ 611a Test regelt etwas."
        assert ggt.validate_anchor(norm, text) is True


class TestHarvestReferrers:
    def test_finds_citation_on_other_page(self):
        page_texts = {1: "hier steht nichts", 2: "hierzu vergleiche § 1 Absatz 2"}
        anchors = {"1": (1, "Test")}
        referrers = ggt.harvest_referrers(page_texts, anchors)
        assert len(referrers["1"]) == 1
        assert referrers["1"][0].page == 2

    def test_excludes_citation_on_own_anchor_page(self):
        page_texts = {1: "§ 1 Test regelt etwas und verweist auf § 1 erneut"}
        anchors = {"1": (1, "Test")}
        referrers = ggt.harvest_referrers(page_texts, anchors)
        assert "1" not in referrers

    def test_ignores_citation_to_unknown_norm(self):
        page_texts = {2: "verweist auf § 999"}
        anchors = {"1": (1, "Test")}
        referrers = ggt.harvest_referrers(page_texts, anchors)
        assert referrers == {}

    def test_skips_citation_to_a_different_statute(self):
        # These would resolve against BGB § 109 by number alone, but the
        # citation is actually to the Zivilprozessordnung (ZPO), a
        # different code entirely.
        page_texts = {5: "das folgt aus § 109 der Zivilprozessordnung"}
        anchors = {"109": (1, "Test")}
        skips = ggt.SkipLog()
        referrers = ggt.harvest_referrers(page_texts, anchors, skips)
        assert "109" not in referrers
        assert skips.counts["external_statute_citation"] == 1

    def test_skips_citation_with_absatz_before_external_statute_name(self):
        page_texts = {
            5: "gemäß § 25 Absatz 1 Satz 2 des Schwangerschaftskonfliktgesetzes"
        }
        anchors = {"25": (1, "Test")}
        referrers = ggt.harvest_referrers(page_texts, anchors)
        assert "25" not in referrers

    def test_keeps_plain_bgb_citation_with_absatz(self):
        page_texts = {5: "gemäß § 355 Absatz 1 des Bürgerlichen Gesetzbuchs"}
        anchors = {"355": (1, "Test")}
        # A spelled-out self-reference ("des Bürgerlichen Gesetzbuchs")
        # is correctly KEPT: the statute-name check only matches the
        # single capitalized word immediately after des/der ("Bürgerlichen",
        # an adjective, matches neither -gesetz/-ordnung/-buch/...), so a
        # two-word "Adjektiv Nomen" statute name doesn't false-positive.
        referrers = ggt.harvest_referrers(page_texts, anchors)
        assert "355" in referrers

    def test_keeps_bare_citation_without_a_following_statute_name(self):
        page_texts = {5: "gemäß § 355 kann der Verbraucher widerrufen"}
        anchors = {"355": (1, "Test")}
        referrers = ggt.harvest_referrers(page_texts, anchors)
        assert "355" in referrers


class TestExtractClause:
    def test_takes_last_sentence_before_citation(self):
        text = "Erster Satz hier. Der Mieter hat ein Widerrufsrecht gemäß § 355."
        idx = text.index("§ 355")
        clause = ggt.extract_clause(text, idx)
        assert clause is not None
        assert "Erster Satz" not in clause
        assert "Widerrufsrecht" in clause

    def test_caps_to_context_window(self):
        long_prefix = " ".join(f"wort{i}" for i in range(40))
        text = f"{long_prefix} § 5"
        idx = text.index("§ 5")
        clause = ggt.extract_clause(text, idx)
        assert clause is not None
        assert len(clause.split()) <= ggt.CONTEXT_WINDOW_TOKENS

    def test_does_not_split_after_abs_abbreviation(self):
        # A plain (?<=[.!?])\s+ split would treat "Abs." as a sentence
        # end, truncating the clause right before the citation.
        text = "Der Mieter hat ein Widerrufsrecht gemäß Abs. 2 § 355."
        idx = text.index("§ 355")
        clause = ggt.extract_clause(text, idx)
        assert clause is not None
        assert "Widerrufsrecht" in clause

    def test_does_not_split_after_nr_abbreviation(self):
        text = "Es gilt die Ausnahme nach Nr. 3 § 611a."
        idx = text.index("§ 611a")
        clause = ggt.extract_clause(text, idx)
        assert clause is not None
        assert "Ausnahme" in clause

    def test_still_splits_on_a_real_sentence_boundary(self):
        text = "Erster Satz endet hier. Zweiter Satz nennt § 5."
        idx = text.index("§ 5")
        clause = ggt.extract_clause(text, idx)
        assert clause is not None
        assert "Erster Satz" not in clause
        assert "Zweiter Satz" in clause


class TestStripCitationTokens:
    def test_strips_section_and_digits(self):
        result = ggt.strip_citation_tokens(
            "das Widerrufsrecht gemäß § 355 Absatz 1 Nummer 2"
        )
        assert "§" not in result
        assert not any(ch.isdigit() for ch in result)
        assert "Widerrufsrecht" in result

    def test_removes_empty_parenthetical_shell(self):
        # "(§ 143)" leaves an empty "()" once the citation inside is gone.
        result = ggt.strip_citation_tokens("der Anfechtungsgegner (§ 143) ist")
        assert "(" not in result
        assert ")" not in result
        assert result == "der Anfechtungsgegner ist"

    def test_peels_trailing_dangling_preposition(self):
        # The citation was the object of "nach"; once it's gone, "nach"
        # dangles at the end and should be peeled off too.
        result = ggt.strip_citation_tokens("die Vorschrift des Kaufrechts nach § 445")
        assert result == "die Vorschrift des Kaufrechts"

    def test_peels_multi_word_trailing_phrase(self):
        # "im Sinne des § 312" -> "im Sinne des" all dangle once § 312 is
        # gone and should be peeled iteratively.
        result = ggt.strip_citation_tokens("sind Drittmittel im Sinne des § 312")
        assert result == "sind Drittmittel"


class TestBuildNaturalQuery:
    def test_rejects_clause_that_mostly_restates_the_rubric(self):
        rng = __import__("random").Random(1)
        clause = (
            "das Widerrufsrecht bei außerhalb von Geschäftsräumen "
            "geschlossenen Verträgen"
        )
        result = ggt.build_natural_query(clause, rng, rubric=clause)
        # High token overlap with the target rubric -> rejected as trivial
        assert result is None

    def test_accepts_distinct_clause(self):
        rng = __import__("random").Random(1)
        result = ggt.build_natural_query(
            "der Mieter kann die Wohnung fristlos kündigen wenn Mängel bestehen",
            rng,
            rubric="Kündigung des Mietverhältnisses",
        )
        assert result is not None
        assert "§" not in result
        assert not any(ch.isdigit() for ch in result)

    def test_rejects_too_short_clause(self):
        rng = __import__("random").Random(1)
        result = ggt.build_natural_query("kurzer Satz", rng, rubric="Anderes Thema")
        assert result is None


class TestBuildKeywordQuery:
    def test_strips_trailing_clause_and_caps_tokens(self):
        q = ggt.build_keyword_query("Wohnsitz; Begründung und Aufhebung")
        assert q == "Wohnsitz"

    def test_caps_at_six_tokens(self):
        rubric = "eins zwei drei vier fünf sechs sieben acht"
        q = ggt.build_keyword_query(rubric)
        assert len(q.split()) == 6


class TestStratifyByBuch:
    def test_maps_norms_to_enclosing_buch(self):
        toc = [
            [1, "Buch 1 Allgemeiner Teil", 25],
            [1, "§ 1\xa0A", 26],
            [1, "Buch 2 Recht der Schuldverhältnisse", 100],
            [1, "§ 300\xa0B", 105],
        ]
        anchors = ggt.parse_anchors(toc)
        norms = ggt.derive_extents(toc, anchors)
        mapping = ggt.stratify_by_buch(toc, norms)
        assert mapping["1"] == "Buch 1 Allgemeiner Teil"
        assert mapping["300"] == "Buch 2 Recht der Schuldverhältnisse"


class TestSampleNorms:
    def test_deterministic_for_fixed_seed(self):
        buch_of = {str(i): f"buch{i % 3}" for i in range(30)}
        eligible = [str(i) for i in range(30)]
        s1 = ggt.sample_norms(eligible, buch_of, n=10, seed=42)
        s2 = ggt.sample_norms(eligible, buch_of, n=10, seed=42)
        assert s1 == s2

    def test_different_seeds_differ(self):
        buch_of = {str(i): f"buch{i % 3}" for i in range(30)}
        eligible = [str(i) for i in range(30)]
        s1 = ggt.sample_norms(eligible, buch_of, n=10, seed=1)
        s2 = ggt.sample_norms(eligible, buch_of, n=10, seed=2)
        assert s1 != s2

    def test_force_include_always_present(self):
        buch_of = {str(i): "buch0" for i in range(20)}
        eligible = [str(i) for i in range(20)]
        sampled = ggt.sample_norms(
            eligible, buch_of, n=5, seed=1, force_include=["611a"]
        )
        # force_include not in eligible pool -> filtered out, no crash
        assert "611a" not in sampled
        eligible_with_target = eligible + ["611a"]
        sampled2 = ggt.sample_norms(
            eligible_with_target, buch_of, n=5, seed=1, force_include=["611a"]
        )
        assert "611a" in sampled2

    def test_does_not_exceed_n(self):
        buch_of = {str(i): f"buch{i % 4}" for i in range(50)}
        eligible = [str(i) for i in range(50)]
        sampled = ggt.sample_norms(eligible, buch_of, n=12, seed=7)
        assert len(sampled) == 12


class TestBuildScenarios:
    def test_produces_matched_k_and_n_pair(self):
        norms = {
            "355": ggt.Norm(
                number="355", rubric="Widerrufsrecht", start_page=82, end_page=82
            )
        }
        referrers = {
            "355": [
                ggt.Referrer(page=88, norm="355", char_start=40, char_end=45),
            ]
        }
        page_texts = {
            88: (
                "Der Verbraucher hat bei außerhalb von Geschäftsräumen "
                "geschlossenen Verträgen ein Widerrufsrecht gemäß § 355."
            )
        }
        skips = ggt.SkipLog()
        scenarios = ggt.build_scenarios(
            ["355"], norms, referrers, page_texts, seed=1, skips=skips
        )
        assert "de01k" in scenarios
        assert "de01n" in scenarios
        assert scenarios["de01k"]["relevant_pages"] == [82]
        # semantic_xref includes the referrer page: the query text is
        # lifted verbatim from p.88, so a model retrieving p.88 is not
        # wrong. target_pages keeps the norm-only page for stricter scoring.
        assert scenarios["de01n"]["relevant_pages"] == [82, 88]
        assert scenarios["de01n"]["target_pages"] == [82]
        assert scenarios["de01n"]["referrer_page"] == 88
        assert scenarios["de01k"]["arm"] == "keyword_control"
        assert scenarios["de01n"]["arm"] == "semantic_xref"
        assert "§" not in scenarios["de01n"]["query"]

    def test_falls_back_to_keyword_only_when_no_referrer(self):
        # e.g. a force-included norm (like § 611a) that no other page
        # cites: still emit the keyword_control arm rather than dropping
        # the norm from the output entirely.
        norms = {"1": ggt.Norm(number="1", rubric="X", start_page=1, end_page=1)}
        skips = ggt.SkipLog()
        scenarios = ggt.build_scenarios(["1"], norms, {}, {}, seed=1, skips=skips)
        assert list(scenarios.keys()) == ["de01k"]
        assert scenarios["de01k"]["arm"] == "keyword_control"
        assert skips.counts["no_referrer"] == 1

    def test_falls_back_to_keyword_only_when_referrer_too_close(self):
        # Referrer page falls INSIDE the norm's own 2-page extent
        # (10..11) -- exactly what "too close" now means: tied to the
        # norm's actual extent, not an arbitrary +/-1 buffer.
        norms = {"1": ggt.Norm(number="1", rubric="X", start_page=10, end_page=11)}
        referrers = {"1": [ggt.Referrer(page=11, norm="1", char_start=0, char_end=5)]}
        skips = ggt.SkipLog()
        scenarios = ggt.build_scenarios(
            ["1"], norms, referrers, {11: "text"}, seed=1, skips=skips
        )
        assert list(scenarios.keys()) == ["de01k"]
        assert skips.counts["referrer_too_close"] == 1

    def test_referrer_one_page_after_extent_is_not_too_close(self):
        # A single-page norm's own extent is exactly [start_page]; a
        # referrer the very next page is a legitimate candidate now (the
        # old +/-1 buffer would have rejected it for no protective reason).
        norms = {"1": ggt.Norm(number="1", rubric="X", start_page=10, end_page=10)}
        referrers = {"1": [ggt.Referrer(page=11, norm="1", char_start=20, char_end=25)]}
        page_texts = {11: "Ein einleitender Satz hier. " + "wort " * 8 + "§ 1"}
        skips = ggt.SkipLog()
        ggt.build_scenarios(["1"], norms, referrers, page_texts, seed=1, skips=skips)
        assert "referrer_too_close" not in skips.counts


class TestGroundTruthShapeCompatibility:
    """The generated JSON must be directly consumable by
    scripts/benchmark_embedding_models.py's existing scenario_k / arm
    handling (added in this same change)."""

    def test_scenarios_carry_required_keys(self):
        norms = {
            "355": ggt.Norm(
                number="355", rubric="Widerrufsrecht", start_page=82, end_page=82
            )
        }
        referrers = {
            "355": [ggt.Referrer(page=88, norm="355", char_start=40, char_end=45)]
        }
        page_texts = {
            88: (
                "Der Verbraucher hat bei außerhalb von Geschäftsräumen "
                "geschlossenen Verträgen ein Widerrufsrecht gemäß § 355."
            )
        }
        skips = ggt.SkipLog()
        scenarios = ggt.build_scenarios(
            ["355"], norms, referrers, page_texts, seed=1, skips=skips
        )
        for sid, s in scenarios.items():
            assert "query" in s
            assert "relevant_pages" in s
            assert isinstance(s["relevant_pages"], list)
            assert "k" in s
            assert "arm" in s


class TestCommittedProvenance:
    """The provenance file ships in the repo, unlike the gitignored
    benchmark_results/ payloads the other benchmark scripts write."""

    PROVENANCE = (
        Path(__file__).parent.parent
        / "benchmark_data"
        / "german_ground_truth_provenance.json"
    )
    GROUND_TRUTH = (
        Path(__file__).parent.parent / "benchmark_data" / "german_ground_truth.json"
    )

    def test_records_the_generating_environment(self):
        env = json.loads(self.PROVENANCE.read_text(encoding="utf-8"))["environment"]
        assert env["python"]
        assert env["packages"]["fastembed"]

    def test_does_not_leak_the_generating_interpreter_path(self):
        env = json.loads(self.PROVENANCE.read_text(encoding="utf-8"))["environment"]
        assert "executable" not in env

    def test_answer_key_pin_matches_provenance(self):
        # jztan's review: the sha256 pin lives on pdfs.bgb.sha256 in the
        # answer key itself (not just the sibling provenance file) so
        # gen_german_ground_truth.py can check it before doing any real
        # work. Both files must agree.
        gt_sha = json.loads(self.GROUND_TRUTH.read_text(encoding="utf-8"))["pdfs"][
            "bgb"
        ]["sha256"]
        prov_sha = json.loads(self.PROVENANCE.read_text(encoding="utf-8"))["sha256"]
        assert gt_sha == prov_sha
        # Regression guard: if this ever changes, the BGB was regenerated
        # against a different PDF than the one every number in
        # benchmark_data/german_embedding_results.md was measured against.
        # A deliberate regeneration must update that doc in the same PR.
        assert (
            gt_sha == "d75a31513293eaf31c166e0985fe1f8586158ceaf56614f3dfd50da183935786"
        )

    def test_provenance_script_version_is_2(self):
        assert (
            json.loads(self.PROVENANCE.read_text(encoding="utf-8"))["script_version"]
            == 2
        )


class TestPinnedSha256:
    def test_reads_pin_from_out_arg(self, tmp_path):
        out = tmp_path / "gt.json"
        out.write_text(
            json.dumps({"pdfs": {"bgb": {"sha256": "abc123"}}}), encoding="utf-8"
        )
        assert ggt._pinned_sha256(str(out)) == "abc123"

    def test_missing_file_returns_none(self, tmp_path):
        assert ggt._pinned_sha256(str(tmp_path / "nope.json")) is None

    def test_dash_falls_back_to_default_out_path(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        default = tmp_path / ggt._DEFAULT_OUT
        default.parent.mkdir(parents=True)
        default.write_text(
            json.dumps({"pdfs": {"bgb": {"sha256": "xyz789"}}}), encoding="utf-8"
        )
        assert ggt._pinned_sha256("-") == "xyz789"

    def test_malformed_json_raises_instead_of_silently_skipping(self, tmp_path):
        # A corrupt answer key must not be treated as "no pin committed
        # yet" -- that would defeat the point of pinning at all.
        out = tmp_path / "gt.json"
        out.write_text("not json", encoding="utf-8")
        with pytest.raises(ValueError, match="not valid JSON"):
            ggt._pinned_sha256(str(out))


class TestMainExitsOnShaMismatch:
    def _run_main(self, monkeypatch, argv):
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit) as exc:
            ggt.main()
        return exc.value.code

    def test_exits_2_on_mismatch(self, monkeypatch, tmp_path):
        pdf = tmp_path / "BGB.pdf"
        pdf.write_bytes(b"actual bytes")
        out = tmp_path / "gt.json"
        out.write_text(
            json.dumps({"pdfs": {"bgb": {"sha256": "not-the-real-hash"}}}),
            encoding="utf-8",
        )
        code = self._run_main(
            monkeypatch,
            [
                "gen_german_ground_truth.py",
                "--pdf",
                str(pdf),
                "--out",
                str(out),
            ],
        )
        assert code == 2

    def test_download_failure_still_exits_1_not_2(self, monkeypatch, tmp_path):
        # Exit 2 is reserved for "the source PDF moved" (a sha256
        # mismatch); a download failure is a different, unrelated failure
        # mode and must keep exit 1.
        monkeypatch.setattr(
            ggt, "_resolve_path", lambda url: (None, {"error": "network down"})
        )
        code = self._run_main(
            monkeypatch,
            ["gen_german_ground_truth.py"],
        )
        assert code == 1

    def test_exits_1_on_malformed_out_file(self, monkeypatch, tmp_path):
        pdf = tmp_path / "BGB.pdf"
        pdf.write_bytes(b"actual bytes")
        out = tmp_path / "gt.json"
        out.write_text("not json", encoding="utf-8")
        code = self._run_main(
            monkeypatch,
            [
                "gen_german_ground_truth.py",
                "--pdf",
                str(pdf),
                "--out",
                str(out),
            ],
        )
        assert code == 1

    def test_force_overrides_mismatch(self, monkeypatch, tmp_path):
        # --force should get past the pin check and proceed into generate(),
        # which will fail for an unrelated reason (not a real BGB PDF) --
        # that's fine, this only asserts the pin check itself didn't fire.
        pdf = tmp_path / "BGB.pdf"
        pdf.write_bytes(b"not a real pdf")
        out = tmp_path / "gt.json"
        out.write_text(
            json.dumps({"pdfs": {"bgb": {"sha256": "not-the-real-hash"}}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "gen_german_ground_truth.py",
                "--pdf",
                str(pdf),
                "--out",
                str(out),
                "--force",
            ],
        )
        with pytest.raises(Exception) as exc:
            ggt.main()
        # Whatever generate() raised on garbage PDF bytes, it isn't the
        # SystemExit(2) the pin check would have raised.
        assert not (isinstance(exc.value, SystemExit) and exc.value.code == 2)

    def test_no_prior_out_file_proceeds_past_the_check(self, monkeypatch, tmp_path):
        pdf = tmp_path / "BGB.pdf"
        pdf.write_bytes(b"not a real pdf")
        out = tmp_path / "does_not_exist_yet.json"
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "gen_german_ground_truth.py",
                "--pdf",
                str(pdf),
                "--out",
                str(out),
            ],
        )
        with pytest.raises(Exception) as exc:
            ggt.main()
        assert not (isinstance(exc.value, SystemExit) and exc.value.code == 2)
