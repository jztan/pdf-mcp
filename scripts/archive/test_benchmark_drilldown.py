"""Unit tests for scripts/benchmark_drilldown.py scenario pairing."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))  # scripts/archive (this dir)
sys.path.insert(0, str(Path(__file__).parent.parent))  # scripts/ (active modules)

import benchmark_drilldown as bd  # noqa: E402


def test_compare_scenarios_pairs_by_id_and_signs_delta():
    """Pairs scenarios by id; delta>0 means the baseline ranked the gold higher."""
    base = [
        {
            "id": "1a",
            "pdf": "x",
            "query": "q1",
            "relevant_pages": [8],
            "rr": 1.0,
            "top_pages": [8, 1],
        },
        {
            "id": "2a",
            "pdf": "y",
            "query": "q2",
            "relevant_pages": [12],
            "rr": 0.0,
            "top_pages": [2, 3],
        },
    ]
    chal = [
        {"id": "1a", "rr": 0.5, "top_pages": [1, 8]},
        {"id": "2a", "rr": 0.5, "top_pages": [12, 1]},
    ]
    rows = bd.compare_scenarios(base, chal)
    by = {r["id"]: r for r in rows}

    assert by["1a"]["baseline_rr"] == 1.0
    assert by["1a"]["challenger_rr"] == 0.5
    assert by["1a"]["delta"] == 0.5  # baseline better
    assert by["2a"]["delta"] == -0.5  # challenger better
    assert by["1a"]["query"] == "q1"


def test_compare_scenarios_summary_counts():
    """summarize() counts baseline wins / challenger wins / ties."""
    rows = [
        {"id": "a", "delta": 0.5},
        {"id": "b", "delta": -0.25},
        {"id": "c", "delta": 0.0},
    ]
    s = bd.summarize(rows)
    assert s["baseline_wins"] == 1
    assert s["challenger_wins"] == 1
    assert s["ties"] == 1
