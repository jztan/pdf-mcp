import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import _retrieval_metrics as m  # noqa: E402


def test_dcg_rewards_earlier_gains():
    # gain 3 at rank 1 contributes 3/log2(2)=3; at rank 2 it is 3/log2(3)
    assert m.dcg_at_k([3, 0], 2) > m.dcg_at_k([0, 3], 2)


def test_ndcg_perfect_ranking_is_one():
    assert m.ndcg_at_k([3, 2, 1], [3, 2, 1], 10) == 1.0


def test_ndcg_empty_ranking_is_zero():
    assert m.ndcg_at_k([], [3, 2, 1], 10) == 0.0


def test_ndcg_all_zero_ideal_is_zero():
    assert m.ndcg_at_k([0, 0], [0, 0], 10) == 0.0


def test_ndcg_worse_ranking_scores_lower():
    perfect = m.ndcg_at_k([3, 2, 1], [3, 2, 1], 10)
    worse = m.ndcg_at_k([1, 2, 3], [3, 2, 1], 10)
    assert worse < perfect


def test_mrr_first_gold_at_rank_2():
    assert m.mrr(["x", "a"], {"a"}) == 0.5


def test_recall_at_k_truncates():
    assert m.recall_at_k(["a", "b", "x"], {"a", "b", "c"}, 2) == 2 / 3
