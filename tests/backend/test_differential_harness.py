import pytest

from tests.backend.differential import assert_equivalent, assert_non_empty


def test_non_empty_guard_rejects_empty_values():
    """The spike's content_trust harness compared two None values and
    reported a clean pass on 10 attack fixtures. Comparing nothing to
    nothing must be an error, not a pass."""
    for empty in (None, [], {}, ""):
        with pytest.raises(AssertionError, match="empty"):
            assert_non_empty(empty, "sample")


def test_non_empty_guard_accepts_real_values():
    assert_non_empty([1], "sample")
    assert_non_empty({"a": 1}, "sample")


def test_equivalent_exact_match():
    assert_equivalent([1, 2], [1, 2], label="exact")


def test_equivalent_rejects_mismatch():
    with pytest.raises(AssertionError):
        assert_equivalent([1, 2], [1, 3], label="exact")


def test_equivalent_respects_float_tolerance():
    """pdfium's C API returns float32 where PyMuPDF uses double, so
    coordinates differ in the 5th significant figure."""
    assert_equivalent([1.00001], [1.00003], tolerance=0.05, label="coords")
    with pytest.raises(AssertionError):
        assert_equivalent([1.0], [1.5], tolerance=0.05, label="coords")
