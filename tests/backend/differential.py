"""Differential comparison helpers for backend equivalence tests.

Tolerances are declared per call with a reason, never a blanket default.
"""

from __future__ import annotations

from typing import Any


def assert_non_empty(value: Any, label: str) -> None:
    """Fail if value carries no information.

    Guards the failure mode that produced a false 14/14 pass in the spike:
    a harness read keys that did not exist, both engines returned None, and
    the comparison agreed on nothing.
    """
    if value is None or (hasattr(value, "__len__") and len(value) == 0):
        raise AssertionError(f"{label}: value is empty, comparison proves nothing")


def assert_equivalent(
    expected: Any, actual: Any, *, tolerance: float = 0.0, label: str = ""
) -> None:
    """Compare two values, allowing numeric drift up to tolerance."""
    if isinstance(expected, bool) or isinstance(actual, bool):
        if expected != actual:
            raise AssertionError(f"{label}: {expected!r} != {actual!r}")
        return
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        if abs(float(expected) - float(actual)) > tolerance:
            raise AssertionError(
                f"{label}: {expected!r} != {actual!r} (tol {tolerance})"
            )
        return
    if isinstance(expected, (list, tuple)) and isinstance(actual, (list, tuple)):
        if len(expected) != len(actual):
            raise AssertionError(f"{label}: length {len(expected)} != {len(actual)}")
        for i, (exp, act) in enumerate(zip(expected, actual)):
            assert_equivalent(exp, act, tolerance=tolerance, label=f"{label}[{i}]")
        return
    if isinstance(expected, dict) and isinstance(actual, dict):
        if set(expected) != set(actual):
            raise AssertionError(f"{label}: keys {set(expected)} != {set(actual)}")
        for key in expected:
            assert_equivalent(
                expected[key], actual[key], tolerance=tolerance, label=f"{label}.{key}"
            )
        return
    if expected != actual:
        raise AssertionError(f"{label}: {expected!r} != {actual!r}")
