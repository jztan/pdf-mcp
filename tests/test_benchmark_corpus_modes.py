"""Tests for the corpus-modes benchmark runner (pure logic only)."""

from scripts.benchmark_corpus_modes import (
    MIN_DESCRIBED_TOKENS,
    agg,
    class_names,
    content_tokens,
    grade_query,
    nonlatin_ids,
    stem,
    validate_described_queries,
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


class TestToolContracts:
    """The single-doc arm calls the production tools directly. Assert the
    keyword arguments it sends actually exist -- a rename would otherwise
    only surface as a crash minutes into a benchmark run."""

    def test_pdf_search_accepts_the_kwargs_the_single_doc_arm_sends(self):
        import inspect

        from pdf_mcp.server import pdf_search

        params = inspect.signature(pdf_search).parameters
        for kw in ("path", "query", "mode", "max_results"):
            assert kw in params, f"pdf_search lost the {kw} parameter"

    def test_pdf_corpus_search_accepts_the_kwargs_the_corpus_arm_sends(self):
        import inspect

        from pdf_mcp.server import pdf_corpus_search

        params = inspect.signature(pdf_corpus_search).parameters
        for kw in ("paths", "query", "mode", "top_k"):
            assert kw in params, f"pdf_corpus_search lost the {kw} parameter"


class TestAggPageLevelNotApplicable:
    def test_all_route_selection_reports_none_not_zero(self):
        rows = {
            "r1": {"ndcg": None, "doc_ndcg": 1.0, "dochit3": 1, "class": "route"},
            "r2": {"ndcg": None, "doc_ndcg": 0.8, "dochit3": 1, "class": "route"},
        }
        out = agg(rows)
        assert out["ndcg"] is None, "page-level score is n/a, not a zero score"
        assert out["doc_ndcg"] == 0.9
        assert out["n"] == 2

    def test_fmt_renders_none_as_na(self):
        from scripts.benchmark_corpus_modes import fmt

        assert fmt(None) == "n/a"
        assert fmt(0.6741) == "0.674"


class TestContentTokens:
    def test_drops_short_tokens_and_stopwords(self):
        assert content_tokens("does this method need labeled data") == [
            "method",
            "need",
            "labeled",
            "data",
        ]

    def test_casefolds_and_splits_on_punctuation(self):
        assert content_tokens("Noetherian-type, splitting!") == [
            "noetherian",
            "type",
            "splitting",
        ]


class TestStem:
    def test_inflections_of_one_word_collapse_together(self):
        # The ONLY property that matters: all forms must agree with each
        # other. The stem itself is not required to be a real word.
        forms = {stem(w) for w in ("decline", "declines", "declined", "declining")}
        assert len(forms) == 1

    def test_collapses_plural_with_singular(self):
        assert stem("label") == stem("labels")
        assert stem("time") == stem("times")

    def test_leaves_short_tokens_alone(self):
        assert stem("gas") == "gas"

    def test_does_not_collapse_unrelated_words(self):
        assert stem("decline") != stem("decrease")


class TestValidateDescribedQueries:
    def _lookup(self, text):
        return lambda doc, page: text

    def test_accepts_query_with_an_absent_content_token(self):
        queries = {
            "queries": [
                {
                    "id": "described-01",
                    "class": "described",
                    "query": "does this method need labeled data at inference",
                    "labels": [{"doc": "d1", "page": 4, "gain": 2}],
                }
            ]
        }
        lookup = self._lookup("the method needs labeled data at test time")
        assert validate_described_queries(queries, lookup) == []

    def test_rejects_query_whose_every_token_is_present(self):
        queries = {
            "queries": [
                {
                    "id": "described-02",
                    "class": "described",
                    "query": "does this method need labeled data at inference",
                    "labels": [{"doc": "d1", "page": 4, "gain": 2}],
                }
            ]
        }
        lookup = self._lookup("method need labeled data inference")
        errors = validate_described_queries(queries, lookup)
        assert len(errors) == 1
        assert "lifted" in errors[0]

    def test_absence_is_checked_after_stemming(self):
        queries = {
            "queries": [
                {
                    "id": "described-03",
                    "class": "described",
                    "query": "why did revenue declines follow supplier changes",
                    "labels": [{"doc": "d1", "page": 1, "gain": 2}],
                }
            ]
        }
        lookup = self._lookup("revenue decline followed supplier change")
        errors = validate_described_queries(queries, lookup)
        assert len(errors) == 1
        assert "lifted" in errors[0]

    def test_rejects_query_under_the_token_floor(self):
        queries = {
            "queries": [
                {
                    "id": "described-04",
                    "class": "described",
                    "query": "splitting families noetherian",
                    "labels": [{"doc": "d1", "page": 1, "gain": 2}],
                }
            ]
        }
        errors = validate_described_queries(queries, self._lookup("unrelated"))
        assert any(str(MIN_DESCRIBED_TOKENS) in e for e in errors)

    def test_rejects_multi_document_described_query(self):
        queries = {
            "queries": [
                {
                    "id": "described-05",
                    "class": "described",
                    "query": "does this method need labeled data at inference",
                    "labels": [
                        {"doc": "d1", "page": 1, "gain": 2},
                        {"doc": "d2", "page": 1, "gain": 2},
                    ],
                }
            ]
        }
        errors = validate_described_queries(queries, self._lookup("unrelated"))
        assert any("single-gold-document" in e for e in errors)

    def test_ignores_non_described_classes(self):
        queries = {
            "queries": [
                {
                    "id": "needle-01",
                    "class": "needle",
                    "query": "short lifted phrase",
                    "labels": [{"doc": "d1", "page": 1, "gain": 2}],
                }
            ]
        }
        assert validate_described_queries(queries, self._lookup("short lifted")) == []
