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


def test_recall_at_k_full():
    from benchmark_hybrid_sections import recall_at_k

    assert recall_at_k(ranked=["a", "b", "c"], gold={"a", "b"}, k=5) == 1.0


def test_recall_at_k_partial():
    from benchmark_hybrid_sections import recall_at_k

    assert abs(recall_at_k(["a", "b", "x"], {"a", "b", "c"}, k=2) - 2 / 3) < 1e-9


def test_recall_at_k_truncates_below_k():
    from benchmark_hybrid_sections import recall_at_k

    assert recall_at_k(["x", "y", "z", "a"], {"a"}, k=3) == 0.0


def test_recall_at_k_empty_gold_raises():
    from benchmark_hybrid_sections import recall_at_k
    import pytest

    with pytest.raises(ValueError):
        recall_at_k(["a"], set(), k=5)
