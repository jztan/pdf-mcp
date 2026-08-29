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
