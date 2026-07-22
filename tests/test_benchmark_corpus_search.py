"""Tests for the corpus-search ranking spike harness (pure logic only)."""

import sqlite3

from scripts._corpus_ranking import (
    evaluate_decision,
    grade_ranking,
    rrf_fuse_doc_rankings,
)
from scripts.benchmark_corpus_search import (
    build_corpus_index,
    build_per_doc_indexes,
    search_corpus,
    search_per_doc_rrf,
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


# Trap fixture: "budget" is boilerplate on every page of alpha and bravo;
# "shortfall" also appears once on alpha/bravo page 2 (padded with filler
# so each page is longer than zulu's), so all three docs match the query
# under FTS5's AND-per-token semantics — the per-doc index cannot skip
# alpha/bravo the way it would if they lacked "shortfall" entirely.
# Global IDF (arm A) still ranks zulu page 2 first: BM25 rewards zulu's
# short, concentrated page over alpha/bravo's longer, padded ones.
# Rank-only fusion (arm B) sees all three docs tie at within-doc rank 1,
# and its tie-break prefers alphabetical doc ids, surfacing alpha first.
TRAP_PAGES = [
    ("alpha", 1, "annual budget overview for the fiscal year budget"),
    (
        "alpha",
        2,
        "budget tables and appendix shortfall listings extra padding words here",
    ),
    ("bravo", 1, "budget summary and budget notes for departments"),
    (
        "bravo",
        2,
        "departmental budget planning shortfall review extra padding words here",
    ),
    ("zulu", 1, "unrelated prose about municipal parks and events"),
    ("zulu", 2, "the projected budget shortfall requires council action"),
]


def _conn():
    return sqlite3.connect(":memory:")


class TestCorpusFtsArm:
    def test_trap_query_ranks_meaningful_page_first(self):
        conn = _conn()
        build_corpus_index(conn, TRAP_PAGES)
        ranked = search_corpus(conn, "budget shortfall", top_k=5)
        assert ranked[0] == ("zulu", 2)

    def test_cjk_query_routes_to_char_split_table(self):
        conn = _conn()
        pages = TRAP_PAGES + [("kanji", 1, "厚木基地の周辺整備について")]
        build_corpus_index(conn, pages)
        ranked = search_corpus(conn, "厚木基地", top_k=3)
        assert ranked[0] == ("kanji", 1)


class TestPerDocRrfArm:
    def test_trap_query_shows_rank_fusion_weakness(self):
        conn = _conn()
        doc_ids = build_per_doc_indexes(conn, TRAP_PAGES)
        assert doc_ids == ["alpha", "bravo", "zulu"]
        ranked = search_per_doc_rrf(
            conn, doc_ids, "budget shortfall", per_doc_k=10, top_k=5
        )
        # Every doc's within-doc best hit fuses at the same RRF score;
        # alphabetical tie-break puts a boilerplate page first. This is
        # the structural weakness the trap class measures.
        assert ranked[0][0] == "alpha"
        assert ("zulu", 2) in ranked

    def test_needle_query_found_by_both_arms(self):
        conn = _conn()
        build_corpus_index(conn, TRAP_PAGES)
        doc_ids = build_per_doc_indexes(conn, TRAP_PAGES)
        a = search_corpus(conn, "municipal parks", top_k=3)
        b = search_per_doc_rrf(conn, doc_ids, "municipal parks", per_doc_k=10, top_k=3)
        assert a[0] == ("zulu", 1)
        assert b[0] == ("zulu", 1)
