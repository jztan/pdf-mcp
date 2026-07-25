"""Tests for the corpus-modes benchmark runner (pure logic only)."""

from scripts.benchmark_corpus_modes import (
    agg,
    class_names,
    grade_query,
    nonlatin_ids,
    validate_queries,
)


class TestClassNames:
    def test_sorted_unique_classes_from_queries(self):
        queries = {
            "queries": [
                {"id": "a", "class": "trap"},
                {"id": "b", "class": "needle"},
                {"id": "c", "class": "trap"},
            ]
        }
        assert class_names(queries) == ["needle", "trap"]

    def test_reproduces_the_legacy_hardcoded_order(self):
        queries = {
            "queries": [
                {"id": "a", "class": "spread"},
                {"id": "b", "class": "needle"},
                {"id": "c", "class": "trap"},
            ]
        }
        assert class_names(queries) == ["needle", "spread", "trap"]


class TestNonlatinIds:
    def test_selects_docs_whose_lang_is_not_en(self):
        manifest = {
            "docs": [
                {"id": "x", "lang": "en"},
                {"id": "y", "lang": "cjk"},
                {"id": "z"},
            ]
        }
        assert nonlatin_ids(manifest) == {"y"}


class TestAgg:
    def test_means_and_count(self):
        rows = {
            "q1": {"ndcg": 1.0, "doc_ndcg": 0.5, "dochit3": 1, "class": "needle"},
            "q2": {"ndcg": 0.0, "doc_ndcg": 0.5, "dochit3": 0, "class": "trap"},
        }
        assert agg(rows) == {"ndcg": 0.5, "doc_ndcg": 0.5, "dochit3": 0.5, "n": 2}

    def test_filtered_selection(self):
        rows = {
            "q1": {"ndcg": 1.0, "doc_ndcg": 1.0, "dochit3": 1, "class": "needle"},
            "q2": {"ndcg": 0.0, "doc_ndcg": 0.0, "dochit3": 0, "class": "trap"},
        }
        out = agg(rows, lambda r: r["class"] == "needle")
        assert out["n"] == 1 and out["ndcg"] == 1.0

    def test_empty_selection_is_zeroed(self):
        assert agg({}, lambda r: True) == {
            "ndcg": 0.0,
            "doc_ndcg": 0.0,
            "dochit3": 0.0,
            "n": 0,
        }


class TestGradeQuery:
    def test_page_labels_produce_page_level_ndcg(self):
        q = {
            "id": "n1",
            "class": "needle",
            "labels": [{"doc": "a", "page": 3, "gain": 2}],
        }
        perfect = grade_query(q, [("a", 3), ("b", 1)], 10)
        assert perfect["ndcg"] == 1.0
        assert perfect["dochit3"] == 1
        missed = grade_query(q, [("b", 1), ("c", 2)], 10)
        assert missed["ndcg"] == 0.0
        assert missed["dochit3"] == 0

    def test_route_labels_have_no_page_ndcg(self):
        q = {
            "id": "r1",
            "class": "route",
            "labels": [{"doc": "a", "gain": 2}, {"doc": "b", "gain": 1}],
        }
        out = grade_query(q, [("a", 7), ("a", 9), ("b", 2)], 10)
        assert out["ndcg"] is None
        assert out["doc_ndcg"] == 1.0
        assert out["dochit3"] == 1

    def test_doc_ndcg_dedupes_docs_and_takes_best_gain(self):
        q = {
            "id": "s1",
            "class": "spread",
            "labels": [
                {"doc": "a", "page": 1, "gain": 1},
                {"doc": "a", "page": 5, "gain": 2},
            ],
        }
        out = grade_query(q, [("a", 9), ("a", 1)], 10)
        assert out["doc_ndcg"] == 1.0

    def test_gain_one_docs_are_not_gold_for_dochit3(self):
        q = {"id": "r2", "class": "route", "labels": [{"doc": "b", "gain": 1}]}
        assert grade_query(q, [("b", 1)], 10)["dochit3"] == 0


class TestAggSkipsRouteQueries:
    def test_none_ndcg_rows_are_excluded_from_the_page_mean(self):
        rows = {
            "q1": {"ndcg": 1.0, "doc_ndcg": 1.0, "dochit3": 1, "class": "needle"},
            "q2": {"ndcg": None, "doc_ndcg": 0.0, "dochit3": 0, "class": "route"},
        }
        out = agg(rows)
        assert out["ndcg"] == 1.0
        assert out["doc_ndcg"] == 0.5
        assert out["n"] == 2


class TestValidateQueries:
    manifest = {"docs": [{"id": "a", "lang": "en"}, {"id": "b", "lang": "en"}]}

    def test_accepts_matching_evidence(self):
        queries = {
            "queries": [
                {
                    "id": "n1",
                    "class": "needle",
                    "labels": [
                        {"doc": "a", "page": 2, "gain": 2, "evidence": "Total  revenue"}
                    ],
                }
            ]
        }
        errors = validate_queries(
            self.manifest, queries, lambda d, p: "TOTAL REVENUE increased"
        )
        assert errors == []

    def test_rejects_missing_evidence(self):
        queries = {
            "queries": [
                {
                    "id": "n1",
                    "class": "needle",
                    "labels": [{"doc": "a", "page": 2, "gain": 2, "evidence": "nope"}],
                }
            ]
        }
        errors = validate_queries(self.manifest, queries, lambda d, p: "other text")
        assert len(errors) == 1 and "evidence not found" in errors[0]

    def test_rejects_unknown_doc(self):
        queries = {
            "queries": [
                {"id": "r1", "class": "route", "labels": [{"doc": "zz", "gain": 2}]}
            ]
        }
        errors = validate_queries(self.manifest, queries, lambda d, p: "")
        assert len(errors) == 1 and "unknown doc" in errors[0]

    def test_route_labels_need_no_evidence(self):
        queries = {
            "queries": [
                {"id": "r1", "class": "route", "labels": [{"doc": "a", "gain": 2}]}
            ]
        }
        assert validate_queries(self.manifest, queries, lambda d, p: "") == []
