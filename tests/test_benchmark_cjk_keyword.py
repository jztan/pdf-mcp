import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import benchmark_cjk_keyword as bench  # noqa: E402


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
