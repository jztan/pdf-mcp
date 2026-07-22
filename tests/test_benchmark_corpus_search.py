"""Tests for the corpus-search ranking spike harness (pure logic only)."""

from scripts._corpus_ranking import (
    evaluate_decision,
    grade_ranking,
    rrf_fuse_doc_rankings,
)


class TestRrfFuseDocRankings:
    def test_interleaves_by_within_doc_rank(self):
        # Two docs, two pages each: all rank-1 pages precede rank-2 pages.
        lists = [
            [("alpha", 3), ("alpha", 7)],
            [("bravo", 1), ("bravo", 2)],
        ]
        fused = rrf_fuse_doc_rankings(lists)
        assert fused[:2] == [("alpha", 3), ("bravo", 1)]
        assert fused[2:] == [("alpha", 7), ("bravo", 2)]

    def test_tie_break_is_deterministic_by_doc_then_page(self):
        lists = [[("zulu", 5)], [("alpha", 9)]]
        fused = rrf_fuse_doc_rankings(lists)
        # Equal RRF scores: alphabetical doc id wins the tie.
        assert fused == [("alpha", 9), ("zulu", 5)]

    def test_top_k_truncates(self):
        lists = [[("a", 1), ("a", 2)], [("b", 1)]]
        assert len(rrf_fuse_doc_rankings(lists, top_k=2)) == 2

    def test_empty_lists(self):
        assert rrf_fuse_doc_rankings([]) == []
        assert rrf_fuse_doc_rankings([[], []]) == []


class TestGradeRanking:
    def test_maps_gains_in_rank_order(self):
        labels = {("a", 1): 2.0, ("b", 4): 1.0}
        ranked = [("b", 4), ("x", 9), ("a", 1)]
        assert grade_ranking(ranked, labels) == [1.0, 0.0, 2.0]


class TestEvaluateDecision:
    BASE_B = {"needle": 0.8, "spread": 0.7, "trap": 0.5}

    def test_temp_fts_wins_on_trap_margin(self):
        a = {"needle": 0.8, "spread": 0.7, "trap": 0.56}
        out = evaluate_decision(a, self.BASE_B, 0.4)
        assert out["winner"] == "temp-fts"

    def test_rrf_wins_when_trap_margin_too_small(self):
        a = {"needle": 0.8, "spread": 0.7, "trap": 0.54}
        out = evaluate_decision(a, self.BASE_B, 0.4)
        assert out["winner"] == "rrf-fusion"

    def test_rrf_wins_when_other_class_regresses(self):
        a = {"needle": 0.77, "spread": 0.7, "trap": 0.60}
        out = evaluate_decision(a, self.BASE_B, 0.4)
        assert out["winner"] == "rrf-fusion"
        assert any("regress" in r for r in out["reasons"])

    def test_rrf_wins_when_arm_a_too_slow(self):
        a = {"needle": 0.8, "spread": 0.7, "trap": 0.60}
        out = evaluate_decision(a, self.BASE_B, 1.2)
        assert out["winner"] == "rrf-fusion"
        assert any("cost" in r or "1.0" in r for r in out["reasons"])

    def test_boundary_exact_margin_wins_and_exact_regress_allowed(self):
        a = {"needle": 0.78, "spread": 0.7, "trap": 0.55}
        out = evaluate_decision(a, self.BASE_B, 0.4)
        # trap delta exactly 0.05 (>=) and needle regress exactly 0.02 (<=)
        assert out["winner"] == "temp-fts"

    def test_near_threshold_deltas_are_not_rounded_up(self):
        # True trap delta 0.0497 must NOT win (rounding to 3dp would
        # wrongly promote it to 0.050).
        a = {"needle": 0.8, "spread": 0.7, "trap": 0.5497}
        out = evaluate_decision(a, self.BASE_B, 0.4)
        assert out["winner"] == "rrf-fusion"

    def test_near_threshold_regression_is_not_rounded_down(self):
        # True needle regression 0.0201 must trigger the regression gate.
        a = {"needle": 0.7799, "spread": 0.7, "trap": 0.60}
        out = evaluate_decision(a, self.BASE_B, 0.4)
        assert out["winner"] == "rrf-fusion"
        assert any("regress" in r for r in out["reasons"])
