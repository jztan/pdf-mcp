"""Tests for the corpus-search ranking spike harness (pure logic only)."""

import json
import sqlite3

import pytest

from scripts import benchmark_corpus_search as bcs
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

    def test_validate_queries_raises_when_a_page_is_not_cached(self):
        # A lookup returning None means the corpus is not warm. That is a
        # setup error, not a bad label, so it must not read as "evidence
        # not found".
        with pytest.raises(bcs.CorpusNotWarm, match="d1 p2"):
            validate_queries(_MANIFEST, _queries("anything"), lambda d, p: None)

    def test_ligature_label_fails_against_cached_text(self):
        # The harnesses score against cached text, where extraction writes
        # "ff" for the ligature. A label quoting the ligature can never score.
        errors = validate_queries(
            _MANIFEST,
            _queries("Delocalization Eﬀects"),
            lambda d, p: "Static Screening and Delocalization Effects",
        )
        assert len(errors) == 1 and "d1 p2" in errors[0]


_MANIFEST = {"docs": [{"id": "d1", "path": "x.pdf", "lang": "en"}]}


def _queries(evidence: str) -> dict:
    return {
        "queries": [
            {
                "id": "q1",
                "class": "needle",
                "query": "anything",
                "labels": [{"doc": "d1", "page": 2, "gain": 2, "evidence": evidence}],
            }
        ]
    }


class TestCachedLookup:
    def _warm(self, tmp_path, text="We analyze the IO\ncomplexity here."):
        from pdf_mcp.cache import PDFCache

        pdf = tmp_path / "x.pdf"
        pdf.write_bytes(b"%PDF-1.4 stub")
        cache_dir = tmp_path / "cache"
        cache = PDFCache(cache_dir=cache_dir)
        cache.save_metadata(str(pdf), 2, {}, [])
        cache.save_page_text(str(pdf), 1, text)
        return pdf, cache_dir

    def test_reads_the_cached_page_not_the_pdf(self, tmp_path):
        # The stub is not a parseable PDF: any re-extraction would fail.
        pdf, cache_dir = self._warm(tmp_path)
        lookup = bcs.cached_page_text_lookup(
            {"d1": str(pdf)}, bcs.open_validation_cache(cache_dir)
        )
        assert lookup("d1", 2) == "We analyze the IO\ncomplexity here."
        assert lookup("d1", 1) is None

    def test_opening_the_cache_does_not_purge_old_rows(self, tmp_path):
        # PDFCache purges rows older than its TTL (24 h by default) on open.
        # Validating a warm cache from an earlier day must not empty it.
        pdf, cache_dir = self._warm(tmp_path)
        with sqlite3.connect(cache_dir / "cache.db") as conn:
            conn.execute("UPDATE pdf_metadata SET accessed_at = '2020-01-01 00:00:00'")
        lookup = bcs.cached_page_text_lookup(
            {"d1": str(pdf)}, bcs.open_validation_cache(cache_dir)
        )
        assert lookup("d1", 2) is not None


class TestDebrisWarnings:
    def test_flags_table_and_number_run_evidence(self):
        q = _queries("Table 2: Wilcoxon Signed-Rank Test Results")
        q["queries"][0]["labels"].append(
            {"doc": "d1", "page": 3, "gain": 1, "evidence": "Loss 6 5 3 108 1010 Par"}
        )
        warnings = bcs.label_debris_warnings(q)
        assert len(warnings) == 2
        assert "d1 p2" in warnings[0] and "d1 p3" in warnings[1]

    def test_prose_with_a_number_is_not_debris(self):
        q = _queries("There are five sentiment labels in SST, 0 to 4.")
        assert bcs.label_debris_warnings(q) == []


class TestValidateExitCodes:
    def _setup(self, tmp_path, monkeypatch, evidence, warm=True):
        pdf = tmp_path / "x.pdf"
        pdf.write_bytes(b"%PDF-1.4 stub")
        out = tmp_path / "corpus_search"
        out.mkdir()
        manifest = {"docs": [{"id": "d1", "path": str(pdf), "lang": "en"}]}
        (out / "manifest.json").write_text(json.dumps(manifest))
        (out / "queries.json").write_text(json.dumps(_queries(evidence)))
        monkeypatch.setattr(bcs, "OUT_DIR", out)
        cache_dir = tmp_path / "cache"
        monkeypatch.setenv("PDF_MCP_CACHE_DIR", str(cache_dir))
        if warm:
            from pdf_mcp.cache import PDFCache

            cache = PDFCache(cache_dir=cache_dir)
            cache.save_metadata(str(pdf), 2, {}, [])
            cache.save_page_text(str(pdf), 1, "We analyze the IO complexity.")

    def test_exit_0_when_every_label_is_in_the_cache(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch, "IO complexity")
        assert bcs.main(["--validate"]) == 0

    def test_exit_1_on_a_failing_label(self, tmp_path, monkeypatch, capsys):
        self._setup(tmp_path, monkeypatch, "not on the page")
        assert bcs.main(["--validate"]) == 1
        assert "INVALID: q1" in capsys.readouterr().out

    def test_exit_2_when_the_corpus_is_not_warm(self, tmp_path, monkeypatch, capsys):
        self._setup(tmp_path, monkeypatch, "IO complexity", warm=False)
        assert bcs.main(["--validate"]) == 2
        assert "not warm" in capsys.readouterr().out

    def test_debris_warns_without_failing(self, tmp_path, monkeypatch, capsys):
        self._setup(tmp_path, monkeypatch, "IO complexity")
        monkeypatch.setattr(bcs, "label_debris_warnings", lambda q: ["WARN x"])
        assert bcs.main(["--validate"]) == 0
        assert "WARN x" in capsys.readouterr().out


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
        assert out.read_text(encoding="utf-8") == self.GENERATED

    def test_refuses_to_clobber_hand_written_sections(self, monkeypatch, tmp_path):
        out = self._patch_out_dir(monkeypatch, tmp_path)
        edited = self.GENERATED + "\n## Described queries\n\nhand-written\n"
        out.write_text(edited, encoding="utf-8")
        write_results_md(self.GENERATED)
        assert (
            out.read_text(encoding="utf-8") == edited
        ), "guard let a hand-written section die"

    def test_force_overwrites(self, monkeypatch, tmp_path):
        out = self._patch_out_dir(monkeypatch, tmp_path)
        out.write_text(
            self.GENERATED + "\n## Described queries\n\nhand-written\n",
            encoding="utf-8",
        )
        write_results_md(self.GENERATED, force=True)
        assert out.read_text(encoding="utf-8") == self.GENERATED

    def test_regenerates_an_untouched_file(self, monkeypatch, tmp_path):
        out = self._patch_out_dir(monkeypatch, tmp_path)
        out.write_text(self.GENERATED, encoding="utf-8")
        write_results_md(self.GENERATED.replace("body", "newer body"))
        assert "newer body" in out.read_text(encoding="utf-8")
