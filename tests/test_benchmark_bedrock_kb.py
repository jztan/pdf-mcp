"""Pure-logic tests for the Bedrock KB anchor benchmark harness."""

from pathlib import Path

from scripts.benchmark_bedrock_kb import check_corpus_quota


class TestCheckCorpusQuota:
    def test_passes_when_every_file_is_under_limit(self, tmp_path: Path):
        (tmp_path / "a.pdf").write_bytes(b"x" * 10)
        manifest = {"docs": [{"id": "a", "path": "a.pdf"}]}
        assert check_corpus_quota(manifest, tmp_path, limit_bytes=100) == []

    def test_reports_each_file_over_limit_with_size(self, tmp_path: Path):
        (tmp_path / "big.pdf").write_bytes(b"x" * 200)
        (tmp_path / "ok.pdf").write_bytes(b"x" * 10)
        manifest = {
            "docs": [
                {"id": "big", "path": "big.pdf"},
                {"id": "ok", "path": "ok.pdf"},
            ]
        }
        errors = check_corpus_quota(manifest, tmp_path, limit_bytes=100)
        assert len(errors) == 1
        assert "big" in errors[0] and "200" in errors[0]

    def test_reports_missing_file(self, tmp_path: Path):
        manifest = {"docs": [{"id": "gone", "path": "gone.pdf"}]}
        errors = check_corpus_quota(manifest, tmp_path, limit_bytes=100)
        assert errors == ["gone: missing at gone.pdf"]


from scripts.benchmark_bedrock_kb import cap_to_budget, estimate_tokens  # noqa: E402


class TestEstimateTokens:
    def test_four_chars_per_token_floor(self):
        assert estimate_tokens("") == 0
        assert estimate_tokens("abc") == 0
        assert estimate_tokens("abcd") == 1
        assert estimate_tokens("a" * 8000) == 2000


class TestCapToBudget:
    def _u(self, i: int, chars: int):
        return (f"d{i}", i, "x" * chars)

    def test_keeps_units_in_order_until_budget(self):
        units = [self._u(1, 4000), self._u(2, 4000), self._u(3, 4000)]
        kept, k = cap_to_budget(units, budget_tokens=2000)
        assert kept == units[:2]
        assert k == 2

    def test_first_unit_always_kept_even_if_oversized(self):
        units = [self._u(1, 40000), self._u(2, 40)]
        kept, k = cap_to_budget(units, budget_tokens=2000)
        assert kept == units[:1]
        assert k == 1

    def test_stops_at_first_unit_that_does_not_fit(self):
        # unit 2 does not fit, unit 3 would, but order is rank order
        units = [self._u(1, 4000), self._u(2, 8000), self._u(3, 40)]
        kept, k = cap_to_budget(units, budget_tokens=2000)
        assert kept == units[:1]
        assert k == 1

    def test_empty(self):
        assert cap_to_budget([], 2000) == ([], 0)


from scripts.benchmark_bedrock_kb import contain, grade_containment  # noqa: E402


class TestContain:
    def test_exact_substring(self):
        assert contain("the Noetherian type of X", "Noetherian type") == "exact"

    def test_normalized_only_when_whitespace_or_case_differs(self):
        ctx = "SPLITTING\n  FAMILIES and\tthe noetherian TYPE"
        assert contain(ctx, "Splitting families and the Noetherian type") == (
            "normalized"
        )

    def test_missing(self):
        assert contain("unrelated text", "Noetherian type") == "missing"

    def test_exact_wins_over_normalized(self):
        assert contain("a b", "a b") == "exact"


class TestGradeContainment:
    def _q(self, *evidence: str):
        return {
            "id": "q",
            "class": "spread",
            "labels": [
                {"doc": f"d{i}", "page": 1, "gain": 2, "evidence": e}
                for i, e in enumerate(evidence)
            ],
        }

    def test_exact_hit_is_recall_one_no_gap(self):
        kept = [("d0", 1, "... Noetherian type ...")]
        g = grade_containment(self._q("Noetherian type"), kept)
        assert g == {"span_recall": 1.0, "fidelity_gap": 0.0, "status": "exact"}

    def test_normalized_hit_is_recall_one_with_gap(self):
        kept = [("d0", 1, "... noetherian\n type ...")]
        g = grade_containment(self._q("Noetherian type"), kept)
        assert g == {"span_recall": 1.0, "fidelity_gap": 1.0, "status": "normalized"}

    def test_any_label_suffices_for_spread(self):
        kept = [("d1", 1, "second span here")]
        g = grade_containment(self._q("first span", "second span"), kept)
        assert g["span_recall"] == 1.0

    def test_missing(self):
        g = grade_containment(self._q("nope"), [("d0", 1, "x")])
        assert g == {"span_recall": 0.0, "fidelity_gap": 0.0, "status": "missing"}

    def test_labels_without_page_are_ignored(self):
        q = {"id": "q", "class": "route", "labels": [{"doc": "d", "gain": 2}]}
        g = grade_containment(q, [("d", 1, "anything")])
        assert g["status"] == "missing"

    def test_context_is_concatenation_of_all_kept_units(self):
        kept = [("d0", 1, "Noether"), ("d0", 2, "ian type")]
        # split across units must NOT match; containment is per unit
        g = grade_containment(self._q("Noetherian type"), kept)
        assert g["span_recall"] == 0.0


from scripts.benchmark_bedrock_kb import bootstrap_diff_ci, no_arm_found  # noqa: E402


class TestBootstrapDiffCi:
    def test_identical_arms_give_zero_diff_and_ci_includes_zero(self):
        a = [1.0, 0.0, 1.0, 1.0, 0.0, 1.0, 0.0, 1.0]
        r = bootstrap_diff_ci(a, list(a), n_boot=500, seed=1)
        assert r["mean_diff"] == 0.0
        assert r["lo"] <= 0.0 <= r["hi"]
        assert r["includes_zero"] is True
        assert r["n"] == 8

    def test_clear_gap_excludes_zero(self):
        a = [1.0] * 20
        b = [0.0] * 20
        r = bootstrap_diff_ci(a, b, n_boot=500, seed=1)
        assert r["mean_diff"] == 1.0
        assert r["includes_zero"] is False

    def test_is_deterministic_for_a_seed(self):
        a = [1.0, 0.0, 1.0, 0.0, 1.0]
        b = [0.0, 0.0, 1.0, 0.0, 1.0]
        assert bootstrap_diff_ci(a, b, seed=7) == bootstrap_diff_ci(a, b, seed=7)

    def test_rejects_unpaired_lengths(self):
        import pytest

        with pytest.raises(ValueError):
            bootstrap_diff_ci([1.0], [1.0, 0.0])


class TestNoArmFound:
    def test_only_queries_missing_everywhere_are_flagged(self):
        status = {
            "P": {"q1": "missing", "q2": "exact", "q3": "missing"},
            "B0": {"q1": "missing", "q2": "missing", "q3": "normalized"},
        }
        assert no_arm_found(status) == ["q1"]

    def test_empty(self):
        assert no_arm_found({}) == []


from scripts.benchmark_bedrock_kb import matches_to_units  # noqa: E402


class TestMatchesToUnits:
    def test_maps_path_to_doc_id_and_keeps_rank_order(self):
        matches = [
            {"path": "/abs/a.pdf", "page": 3, "excerpt": "AAA"},
            {"path": "/abs/b.pdf", "page": 1, "excerpt": "BBB"},
        ]
        units = matches_to_units(matches, {"/abs/a.pdf": "a", "/abs/b.pdf": "b"})
        assert units == [("a", 3, "AAA"), ("b", 1, "BBB")]

    def test_unknown_path_keeps_path_as_id(self):
        units = matches_to_units([{"path": "/x.pdf", "page": 1, "excerpt": ""}], {})
        assert units == [("/x.pdf", 1, "")]
