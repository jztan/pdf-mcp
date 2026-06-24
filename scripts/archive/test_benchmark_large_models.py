"""Unit tests for scripts/benchmark_large_models.py report building."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))  # scripts/archive (this dir)
sys.path.insert(0, str(Path(__file__).parent.parent))  # scripts/ (active modules)

import benchmark_large_models as blm  # noqa: E402


def test_build_report_flags_gate_winner_and_deltas():
    """build_report computes deltas vs baseline and flags only gate-clearing models."""
    results = [
        {"model": "BAAI/bge-small-en-v1.5", "mrr": 0.616, "p50_query_ms": 50.0},
        {"model": "thenlper/gte-large", "mrr": 0.700, "p50_query_ms": 200.0},
        {
            "model": "mixedbread-ai/mxbai-embed-large-v1",
            "mrr": 0.620,
            "p50_query_ms": 180.0,
        },
    ]
    rep = blm.build_report(results, "BAAI/bge-small-en-v1.5", mrr_gate=0.05)
    rows = {r["model"]: r for r in rep["models"]}

    assert rows["thenlper/gte-large"]["delta_vs_baseline"] == 0.084
    assert rows["thenlper/gte-large"]["beats_gate"] is True
    # +0.004 is below the 0.05 gate
    assert rows["mixedbread-ai/mxbai-embed-large-v1"]["beats_gate"] is False
    # baseline is never its own challenger
    assert rows["BAAI/bge-small-en-v1.5"]["beats_gate"] is False
    assert rep["winner"] == "thenlper/gte-large"
    assert rep["baseline"] == "BAAI/bge-small-en-v1.5"


def test_build_report_no_winner_when_none_beat_gate():
    """winner is None when no challenger clears the MRR gate."""
    results = [
        {"model": "base", "mrr": 0.616, "p50_query_ms": 50.0},
        {"model": "cand", "mrr": 0.640, "p50_query_ms": 100.0},  # +0.024 < gate
    ]
    rep = blm.build_report(results, "base", mrr_gate=0.05)
    assert rep["winner"] is None
