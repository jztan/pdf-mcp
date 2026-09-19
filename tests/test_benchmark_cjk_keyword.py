import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import benchmark_cjk_keyword as bench  # noqa: E402

from pdf_mcp import _core  # noqa: E402


def test_run_benchmark_restores_the_server_cache(tmp_path, monkeypatch):
    # run_benchmark() swaps _core.cache onto a temp dir. Left in place, the
    # swap leaks into whichever test runs next (url_fetcher still points at
    # the old cache root), so it must be undone even with no corpus present.
    queries = tmp_path / "queries.json"
    queries.write_text('{"queries": []}', encoding="utf-8")
    monkeypatch.setattr(bench, "QUERIES_PATH", queries)
    before = _core.cache

    results = bench.run_benchmark()

    assert results == {"mean_recall": 0.0}
    assert _core.cache is before


def test_run_benchmark_restores_the_server_cache_on_error(tmp_path, monkeypatch):
    queries = tmp_path / "queries.json"
    queries.write_text('{"queries": [{"pdf": "x.pdf"}]}', encoding="utf-8")
    monkeypatch.setattr(bench, "QUERIES_PATH", queries)
    monkeypatch.setattr(bench, "CORPUS_DIR", tmp_path)
    (tmp_path / "x.pdf").write_bytes(b"not a pdf")
    before = _core.cache

    with pytest.raises(Exception):
        bench.run_benchmark()

    assert _core.cache is before


@pytest.mark.slow
@pytest.mark.skipif(
    not bench.corpus_available(), reason="local vertical-jp corpus absent"
)
def test_cjk_keyword_recovers_embedded_terms():
    results = bench.run_benchmark()
    # The verified failing case must now return hits.
    assert results["厚木基地"]["hits"] > 0
    # No regression on the term that already worked.
    assert results["終活"]["hits"] > 0
    # Recall floor across the graded set. Measured 1.00 on first run (the
    # char-split phrase index + literal-substring post-filter make recall
    # structurally complete); floored at 0.95 for a small extraction margin.
    assert results["mean_recall"] >= 0.95
