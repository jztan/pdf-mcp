"""Unit tests for chart_extractor — the pure-logic chart extraction module."""

from pathlib import Path

import pymupdf
import pytest

from pdf_mcp import chart_extractor

SYN = (
    Path(__file__).parent.parent / "benchmark_data" / "chart_extraction" / "syn_corpus"
)


@pytest.fixture(scope="module")
def line_doc():
    doc = pymupdf.open(SYN / "line_color_linear.pdf")
    yield doc
    doc.close()


@pytest.fixture(scope="module")
def dual_doc():
    doc = pymupdf.open(SYN / "line_dual_axis.pdf")
    yield doc
    doc.close()


def test_extract_charts_takes_open_doc_zero_indexed(line_doc):
    result = chart_extractor.extract_charts(line_doc, 0)
    assert result["status"] == "ok"
    assert result["charts"][0]["chart_id"] == "p0"
    curve = result["charts"][0]["curves"][0]
    assert curve["points"], "line curve must emit points"


def test_question_ids_are_positional(dual_doc):
    result = chart_extractor.extract_charts(dual_doc, 0)
    assert result["status"] == "needs_hint"
    ids = [q["id"] for q in result["questions"]]
    # positional: p{panel}.s{series}.axis — never style-derived
    assert all(i.startswith("p0.s") and i.endswith(".axis") for i in ids)
    # style still present for display
    assert all("series_style" in q for q in result["questions"])


def test_hints_resolve_dual_axis(dual_doc):
    r1 = chart_extractor.extract_charts(dual_doc, 0)
    hints = {q["id"]: "left" for q in r1["questions"]}
    # blue is left in this fixture; red right — answer red correctly.
    # (fixture uses matplotlib "tab:red" = rgb(0.84, 0.15, 0.16), not pure
    # red, so the red-channel threshold is 0.5, not 0.9 — tab:blue's red
    # channel is 0.12, so 0.5 cleanly discriminates the two series.)
    for q in r1["questions"]:
        if q["series_style"]["color"] and q["series_style"]["color"][0] > 0.5:
            hints[q["id"]] = "right"
    r2 = chart_extractor.extract_charts(dual_doc, 0, hints=hints)
    assert r2["status"] == "ok"
    axes = {c.get("axis") for ch in r2["charts"] for c in ch["curves"]}
    assert axes == {"left", "right"}


def test_detect_charts_signal(line_doc):
    n = chart_extractor.detect_charts_signal(line_doc[0])
    assert n == 1


def test_detect_charts_signal_budget_returns_none(line_doc):
    assert chart_extractor.detect_charts_signal(line_doc[0], budget_ms=0) is None


def test_version_constant():
    assert isinstance(chart_extractor.CHART_EXTRACTION_VERSION, int)
