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


def test_annotated_hint_render(tmp_path, dual_doc):
    result = chart_extractor.extract_charts(dual_doc, 0)
    assert result["status"] == "needs_hint"
    chart_extractor.annotate_questions(dual_doc, 0, result, tmp_path, "testhash")
    q = result["questions"][0]
    assert Path(q["render_path"]).exists()
    assert q["highlight"] in chart_extractor._HALO_NAMES
    # halo hue must genuinely contrast with the series' own color — not just
    # differ in name — so channel-wise distance must clear a real threshold.
    halo_rgb = chart_extractor._HALOS[q["highlight"]]
    series_rgb = q["series_style"]["color"] or (0, 0, 0)
    dist = sum(abs(a - b) for a, b in zip(halo_rgb, series_rgb))
    assert dist >= 0.8


def test_detect_charts_signal(line_doc):
    n = chart_extractor.detect_charts_signal(line_doc[0])
    assert n == 1


def test_detect_charts_signal_budget_returns_none(line_doc):
    assert chart_extractor.detect_charts_signal(line_doc[0], budget_ms=0) is None


def test_version_constant():
    assert isinstance(chart_extractor.CHART_EXTRACTION_VERSION, int)


def test_sharp_peak_survives_sampling():
    doc = pymupdf.open(SYN / "line_sharp_peak.pdf")
    result = chart_extractor.extract_charts(doc, 0, max_points=24)
    doc.close()
    assert result["status"] == "ok"
    pts = result["charts"][0]["curves"][0]["points"]
    ys = [p[1] for p in pts]
    # ground truth peak is y=100 at x=5.03 (between uniform sample slots)
    assert max(ys) > 95, f"peak lost: max emitted y={max(ys)}"


def test_extrema_overflow_self_reports():
    doc = pymupdf.open(SYN / "line_sharp_peak.pdf")
    # budget of 6 points cannot hold the jagged section's extrema
    result = chart_extractor.extract_charts(doc, 0, max_points=6)
    doc.close()
    notes = result["charts"][0]["diagnostics"].get("notes", [])
    assert any("extrema exceeded max_points" in n for n in notes)


def test_global_max_survives_adversarial_prominence():
    import numpy as np

    from pdf_mcp.chart_extractor import _select_sample_indices

    # gentle global hill (apex 100) plus four sharper, higher-local-
    # prominence spikes (amplitude 40): local prominence ranking alone
    # would fill the budget with spikes and drop the true global max.
    xs = np.linspace(0.0, 10.0, 401)
    ys = 100.0 * np.exp(-((xs - 5.0) ** 2) / 8.0)
    for cx in (1.0, 2.0, 8.0, 9.0):
        ys = ys + 40.0 * (np.abs(xs - cx) < 0.03)
    sel = _select_sample_indices(ys, 6)
    assert int(np.argmax(ys)) in sel, "global max dropped by sampler"
    assert 0 in sel and len(ys) - 1 in sel


def test_max_points_floor():
    doc = pymupdf.open(SYN / "line_sharp_peak.pdf")
    result = chart_extractor.extract_charts(doc, 0, max_points=1)
    doc.close()
    pts = result["charts"][0]["curves"][0]["points"]
    assert len(pts) <= 4


REAL = Path(__file__).parent.parent / "benchmark_data" / ".reading_order_pdfs"


@pytest.mark.skipif(
    not (REAL / "1807.11632.pdf").exists(), reason="real corpus not fetched"
)
def test_1807_dual_axis_resolves_via_text_no_hints():
    doc = pymupdf.open(REAL / "1807.11632.pdf")
    result = chart_extractor.extract_charts(doc, 3)  # page 4, 0-indexed
    doc.close()
    # legend "MCD"/"F0 RMSE" + right-axis title "F0 RMSE (Hz)" resolve the
    # axis assignment with ZERO questions
    assert result["status"] == "ok", result.get("questions")
    reds = [
        c
        for ch in result["charts"]
        for c in ch["curves"]
        if c["style"][0] and c["style"][0][0] == 1.0
    ]
    assert reds and reds[0]["axis"] == "right"
    assert reds[0]["resolved_by"] == "text"
    assert reds[0].get("label") == "F0 RMSE"


def test_style_collision_disables_text_answer(dual_doc):
    # the dual-axis synthetic has NO legend at all -> text tier cannot fire,
    # questions must remain
    result = chart_extractor.extract_charts(dual_doc, 0)
    assert result["status"] == "needs_hint"


def test_legend_style_collision_drops_both_entries(monkeypatch):
    """Two legend entries sharing a stroke color identify nothing: the
    collision-drop path must leave the question open (no text answer)."""
    from pdf_mcp import chart_extractor as ce

    blue = (0.1, 0.2, 0.8)
    monkeypatch.setattr(
        ce,
        "_legend_entries",
        lambda page, panel: [
            ((blue, None, 1.0), "series alpha"),
            ((blue, None, 1.0), "series beta"),
        ],
    )
    monkeypatch.setattr(
        ce,
        "_axis_titles",
        lambda page, panel: {"left": "alpha (units)", "right": "beta (units)"},
    )
    curves = [{"style": (blue, None, 1.0), "points": [[0, 0], [1, 1]]}]
    questions = [{"id": "p0.s0.axis", "kind": "y_axis_for_curve"}]
    answers, labels = ce.resolve_semantics(None, None, curves, questions)
    assert answers == {}, "collision must disable text self-answer"
    assert labels == {}


def test_legend_unique_match_answers(monkeypatch):
    from pdf_mcp import chart_extractor as ce

    blue = (0.1, 0.2, 0.8)
    red = (0.9, 0.1, 0.1)
    monkeypatch.setattr(
        ce,
        "_legend_entries",
        lambda page, panel: [
            ((blue, None, 1.0), "series alpha"),
            ((red, None, 1.0), "series beta"),
        ],
    )
    monkeypatch.setattr(
        ce,
        "_axis_titles",
        lambda page, panel: {"left": "alpha (units)", "right": "beta (units)"},
    )
    curves = [
        {"style": (blue, None, 1.0), "points": [[0, 0], [1, 1]]},
        {"style": (red, None, 1.0), "points": [[0, 0], [1, 1]]},
    ]
    questions = [
        {"id": "p0.s0.axis", "kind": "y_axis_for_curve"},
        {"id": "p0.s1.axis", "kind": "y_axis_for_curve"},
    ]
    answers, labels = ce.resolve_semantics(None, None, curves, questions)
    assert answers == {"p0.s0.axis": "left", "p0.s1.axis": "right"}
    assert labels == {0: "series alpha", 1: "series beta"}
