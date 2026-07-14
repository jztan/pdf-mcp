"""Synthetic chart benchmark as a fast regression test (wrong-emit gate)."""

import importlib.util
import os
import sys

import pytest


def test_synthetic_benchmark_zero_wrong_emit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test that synthetic chart benchmark has zero wrong-emit cases.

    This test runs the benchmark_data/chart_extraction/bench_synthetic.py
    benchmark at module level to gate on:
    - WRONG-EMIT count is 0
    - All decline_expected cases correctly-declined

    Not marked slow because it runs offline (~2s) on the committed syn_corpus/.
    """
    BENCH = os.path.join(
        os.path.dirname(__file__), "..", "benchmark_data", "chart_extraction"
    )

    # Use a unique sys.modules key to avoid interference from multiple imports
    spec = importlib.util.spec_from_file_location(
        "bench_synthetic_test_case", os.path.join(BENCH, "bench_synthetic.py")
    )
    assert spec is not None, "Failed to create module spec"
    assert spec.loader is not None, "Module spec has no loader"

    mod = importlib.util.module_from_spec(spec)
    sys.modules["bench_synthetic_test_case"] = mod

    # Execute the module (runs the benchmark at import time)
    spec.loader.exec_module(mod)

    # Capture the printed output
    out = capsys.readouterr().out

    # Verify WRONG-EMIT count is 0
    assert (
        "WRONG-EMIT count: 0 /" in out
    ), f"Expected WRONG-EMIT count to be 0, but output was:\n{out}"

    # Verify all decline_expected cases were correctly-declined
    assert (
        "correctly-declined" in out
    ), f"Expected 'correctly-declined' to appear in output, but output was:\n{out}"
