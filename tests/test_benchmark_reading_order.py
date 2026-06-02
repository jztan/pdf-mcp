"""Tests for the reading-order benchmark's pure scoring/classification core.

The benchmark measures how faithfully pdf-mcp's text extraction preserves
document reading order on multi-column PDFs, scored against READoc ground
truth. These tests cover the deterministic helpers; corpus fetching and the
PyMuPDF4LLM reference column are exercised only in the (network-bound) run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from benchmark_reading_order import (  # noqa: E402
    classify_columns,
    normalize_tokens,
    reading_order_score,
)


def test_normalize_tokens_lowercases_and_keeps_alnum():
    assert normalize_tokens("Hello, World! 42") == ["hello", "world", "42"]


def test_normalize_tokens_strips_latex_commands():
    assert normalize_tokens(r"see \alpha and \beta here") == [
        "see",
        "and",
        "here",
    ]


def test_normalize_tokens_caps_length():
    assert normalize_tokens("a b c d e", cap=3) == ["a", "b", "c"]


def test_reading_order_score_identical_is_one():
    assert reading_order_score("alpha beta gamma", "alpha beta gamma") == 1.0


def test_reading_order_score_empty_prediction_is_zero():
    assert reading_order_score("", "alpha beta gamma") == 0.0


def test_reading_order_score_partial_between_zero_and_one():
    s = reading_order_score("alpha beta", "alpha beta gamma delta")
    assert 0.0 < s < 1.0


_BODY = " ".join(["body text words filling the column region"] * 8)


def test_classify_columns_single_column():
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_textbox(pymupdf.Rect(50, 100, 550, 400), _BODY)
    assert classify_columns(doc) == 1
    doc.close()


def test_classify_columns_two_column():
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_textbox(pymupdf.Rect(50, 100, 290, 400), _BODY)
    page.insert_textbox(pymupdf.Rect(340, 100, 560, 400), _BODY)
    assert classify_columns(doc) == 2
    doc.close()
