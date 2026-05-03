import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


def test_mrr_perfect_rank():
    from benchmark_hybrid_sections import mrr

    assert mrr(ranked=["a", "c", "x"], gold={"a", "c"}) == 1.0


def test_mrr_first_gold_at_rank_2():
    from benchmark_hybrid_sections import mrr

    assert mrr(ranked=["x", "a", "c"], gold={"a", "c"}) == 0.5


def test_mrr_no_gold_in_results():
    from benchmark_hybrid_sections import mrr

    assert mrr(ranked=["x", "y", "z"], gold={"a"}) == 0.0


def test_mrr_empty_results():
    from benchmark_hybrid_sections import mrr

    assert mrr(ranked=[], gold={"a"}) == 0.0
