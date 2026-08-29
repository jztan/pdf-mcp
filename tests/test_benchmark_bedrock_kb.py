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
