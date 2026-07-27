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
    normalize,
    search_corpus,
    search_per_doc_rrf,
    validate_queries,
    write_results_md,
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


# Trap fixture: this synthetic corpus proves the two arms are
# distinguishable, not that either one is "smarter". Arm A ranks
# cross-doc via corpus-wide BM25 (here dominated by length
# normalization, not IDF); arm B fuses within-doc ranks, so pages
# tied at equal within-doc rank tie regardless of content. The real
# IDF-vs-fusion question is answered by the trap-class queries on the
# real corpus in the benchmark run, not by this fixture.
#
# "budget" is boilerplate on every page of alpha and bravo; "shortfall"
# also appears once on alpha/bravo page 2, buried in heavy filler so
# those pages are much longer than zulu's short, concentrated page, so
# all three docs match the query under FTS5's AND-per-token semantics.
# The decoy pages (page 3/4 on each doc, none containing "budget" or
# "shortfall") exist only to keep both terms' corpus-wide document
# frequency away from exactly half the corpus: at exactly N/2, SQLite's
# BM25 IDF term evaluates to ln(1) == 0, collapsing all three matching
# pages' scores to a ~1e-6 sliver that any BM25 rounding change or
# one-word fixture edit could flip. With the decoys, arm A's margin
# between zulu page 2 and the alpha/bravo runner-up is a comfortable
# ~0.78 (measured: zulu -1.122 vs alpha/bravo -0.343).
# Rank-only fusion (arm B) sees all three docs tie at within-doc rank 1,
# and its tie-break prefers alphabetical doc ids, surfacing alpha first.
TRAP_PAGES = [
    ("alpha", 1, "annual budget overview for the fiscal year budget budget budget"),
    (
        "alpha",
        2,
        ("budget " * 8)
        + (" ".join(["padding"] * 60))
        + " shortfall "
        + (" ".join(["padding"] * 60)),
    ),
    ("bravo", 1, "budget summary and budget notes for departments budget budget"),
    (
        "bravo",
        2,
        ("budget " * 8)
        + (" ".join(["padding"] * 60))
        + " shortfall "
        + (" ".join(["padding"] * 60)),
    ),
    ("zulu", 1, "unrelated prose about municipal parks and events"),
    ("zulu", 2, "the projected budget shortfall requires council action"),
    ("alpha", 3, "quarterly report on staffing and logistics for the office"),
    ("bravo", 3, "meeting minutes regarding facilities and travel policy"),
    ("zulu", 3, "park maintenance schedule and volunteer sign up sheet"),
    ("alpha", 4, "training materials for new hires in the finance office"),
]


def _conn():
    return sqlite3.connect(":memory:")


class TestCorpusFtsArm:
    def test_corpus_arm_discriminates_across_docs(self):
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
    def test_rrf_arm_cannot_discriminate_across_docs(self):
        conn = _conn()
        doc_ids = build_per_doc_indexes(conn, TRAP_PAGES)
        assert doc_ids == ["alpha", "bravo", "zulu"]
        ranked = search_per_doc_rrf(
            conn, doc_ids, "budget shortfall", per_doc_k=10, top_k=5
        )
        # Every doc's within-doc best hit fuses at the same RRF score;
        # alphabetical tie-break puts a boilerplate page first. This is
        # the structural limitation of rank-only fusion the trap class
        # measures: it cannot discriminate across docs by content, only
        # by within-doc rank.
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


class TestValidation:
    def test_normalize_collapses_whitespace_and_case(self):
        assert normalize("Flash\nAttention  IO") == "flash attention io"

    def test_validate_queries_reports_missing_doc_and_bad_evidence(self):
        manifest = {"docs": [{"id": "d1", "path": "x.pdf", "lang": "en"}]}
        queries = {
            "queries": [
                {
                    "id": "q1",
                    "class": "needle",
                    "query": "anything",
                    "labels": [{"doc": "ghost", "page": 1, "gain": 2, "evidence": "e"}],
                }
            ]
        }
        errors = validate_queries(manifest, queries, page_text_lookup=lambda d, p: "")
        assert any("ghost" in e for e in errors)

    def test_validate_queries_passes_when_evidence_found(self):
        manifest = {"docs": [{"id": "d1", "path": "x.pdf", "lang": "en"}]}
        queries = {
            "queries": [
                {
                    "id": "q1",
                    "class": "needle",
                    "query": "anything",
                    "labels": [
                        {
                            "doc": "d1",
                            "page": 2,
                            "gain": 2,
                            "evidence": "IO complexity",
                        }
                    ],
                }
            ]
        }
        errors = validate_queries(
            manifest,
            queries,
            page_text_lookup=lambda d, p: "We analyze the IO\ncomplexity here.",
        )
        assert errors == []


class TestWriteResultsMdGuard:
    """RESULTS.md carries hand-written interpretation and a second
    benchmark arm appended by another script. A blind --run overwrite
    silently destroyed all of it before this guard existed."""

    GENERATED = "# Cross-Doc Keyword Ranking Spike: Results\n\nbody\n"

    def _patch_out_dir(self, monkeypatch, tmp_path):
        import scripts.benchmark_corpus_search as bcs

        monkeypatch.setattr(bcs, "OUT_DIR", tmp_path)
        return tmp_path / "RESULTS.md"

    def test_writes_when_no_file_exists(self, monkeypatch, tmp_path):
        out = self._patch_out_dir(monkeypatch, tmp_path)
        write_results_md(self.GENERATED)
        assert out.read_text() == self.GENERATED

    def test_refuses_to_clobber_hand_written_sections(self, monkeypatch, tmp_path):
        out = self._patch_out_dir(monkeypatch, tmp_path)
        edited = self.GENERATED + "\n## Described queries\n\nhand-written\n"
        out.write_text(edited)
        write_results_md(self.GENERATED)
        assert out.read_text() == edited, "guard let a hand-written section die"

    def test_force_overwrites(self, monkeypatch, tmp_path):
        out = self._patch_out_dir(monkeypatch, tmp_path)
        out.write_text(self.GENERATED + "\n## Described queries\n\nhand-written\n")
        write_results_md(self.GENERATED, force=True)
        assert out.read_text() == self.GENERATED

    def test_regenerates_an_untouched_file(self, monkeypatch, tmp_path):
        out = self._patch_out_dir(monkeypatch, tmp_path)
        out.write_text(self.GENERATED)
        write_results_md(self.GENERATED.replace("body", "newer body"))
        assert "newer body" in out.read_text()
