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


def test_needs_hint_curves_carry_no_points(dual_doc):
    """Trust-contract regression: a curve whose axis question is still OPEN
    must never carry a numeric points table. Pre-fix, extract_charts left
    the curve's geometry-extracted "points" (calibrated against the default
    left axis) on the curve alongside resolved_by="geometry" — a wrong-table
    escape path, since the real axis is unknown until the caller answers.
    """
    result = chart_extractor.extract_charts(dual_doc, 0)
    assert result["status"] == "needs_hint"
    pending_ids = {q["id"] for q in result["questions"]}
    assert pending_ids
    for ch in result["charts"]:
        for ci, c in enumerate(ch.get("curves", [])):
            akey = f"{ch['chart_id']}.s{ci}.axis"
            if akey in pending_ids:
                assert "points" not in c, f"{akey} must not carry a points table"
                assert c.get("axis") is None
                assert c.get("pending_question") == akey
                # style/label survive so callers can still correlate the
                # question to a visual series
                assert "style" in c
                assert "label" in c


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


def test_declined_chart_carries_reason_in_diagnostics_notes():
    doc = pymupdf.open(SYN / "line_mono_crossing.pdf")
    try:
        result = chart_extractor.extract_charts(doc, 0)
    finally:
        doc.close()
    assert result["status"] == "declined"
    declined = [c for c in result["charts"] if c["chart_type"] == "declined"]
    assert declined, "expected at least one declined chart"
    chart = declined[0]
    assert chart["decline_reason"]
    notes = chart["diagnostics"]["notes"]
    assert any("multivalued" in n for n in notes)


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


def _pixels(pix):
    import numpy as np

    arr = np.frombuffer(pix.samples, dtype=np.uint8)
    return arr.reshape(pix.height, pix.width, pix.n)[..., :3].astype(int)


def test_annotated_hint_render_pixels_show_halo(tmp_path, dual_doc):
    """The halos must be VISIBLE in the saved PNG, not silently buried
    (regression: overlay=False hid them under the opaque plot background).
    Compares annotated vs unannotated renders of the same clip pixel-wise."""
    import numpy as np

    result = chart_extractor.extract_charts(dual_doc, 0)
    chart_extractor.annotate_questions(dual_doc, 0, result, tmp_path, "pxhash")
    annotated = _pixels(pymupdf.Pixmap(result["questions"][0]["render_path"]))
    # baseline: identical clip + dpi rendered straight from the source page
    page = dual_doc[0]
    panel = chart_extractor.find_panels(page)[0]
    clip = pymupdf.Rect(
        panel["rx0"] - 5, panel["ry0"] - 5, panel["rx1"] + 5, panel["ry1"] + 5
    )
    base = _pixels(page.get_pixmap(dpi=200, clip=clip))
    assert annotated.shape == base.shape
    changed = np.abs(annotated - base).sum(axis=2) > 30
    # a real halo band repaints a substantial share of the panel
    assert changed.mean() > 0.005, f"only {changed.mean():.4%} of pixels changed"
    # ...and among the changed pixels, each queried series must have at least
    # one pixel whose color is closer to its halo RGB than to white or to the
    # series' own color (pure background/stroke shifts would fail this)
    cpx = annotated[changed]
    for q in result["questions"]:
        halo = np.array(chart_extractor._HALOS[q["highlight"]]) * 255
        series = np.array(q["series_style"]["color"] or (0, 0, 0)) * 255
        d_halo = np.abs(cpx - halo).sum(axis=1)
        d_white = np.abs(cpx - 255).sum(axis=1)
        d_series = np.abs(cpx - series).sum(axis=1)
        assert (
            (d_halo < d_white) & (d_halo < d_series)
        ).any(), f"no halo-colored pixels for {q['id']} ({q['highlight']})"


def test_detect_charts_signal(line_doc):
    n = chart_extractor.detect_charts_signal(line_doc[0])
    assert n == 1


def test_detect_charts_signal_budget_returns_none(line_doc):
    assert chart_extractor.detect_charts_signal(line_doc[0], budget_ms=0) is None


def test_version_constant():
    assert isinstance(chart_extractor.CHART_EXTRACTION_VERSION, int)


def test_hints_hash_order_independent():
    a = chart_extractor.hints_hash({"p0.type": "bar", "p0.s1.axis": "right"})
    b = chart_extractor.hints_hash({"p0.s1.axis": "right", "p0.type": "bar"})
    assert a == b


def test_hints_hash_none_equals_empty_dict():
    assert chart_extractor.hints_hash(None) == chart_extractor.hints_hash({})


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
        if c["style"]["color"] and c["style"]["color"][0] == 1.0
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
    curves = [{"_style_key": (blue, None, 1.0), "points": [[0, 0], [1, 1]]}]
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
        {"_style_key": (blue, None, 1.0), "points": [[0, 0], [1, 1]]},
        {"_style_key": (red, None, 1.0), "points": [[0, 0], [1, 1]]},
    ]
    questions = [
        {"id": "p0.s0.axis", "kind": "y_axis_for_curve"},
        {"id": "p0.s1.axis", "kind": "y_axis_for_curve"},
    ]
    answers, labels = ce.resolve_semantics(None, None, curves, questions)
    assert answers == {"p0.s0.axis": "left", "p0.s1.axis": "right"}


def test_colorbar_never_wins_as_y_axis():
    """Regression for the arXiv 2001.08361 p24 Fig18 wrong-emit: a compact
    3-tick panel y-axis (100/300/500, pixel span < 60) sits next to a taller
    ScalarMappable colorbar (0..10, "Test Loss"). Pre-fix, the compact real
    axis was rejected by the 60pt span filter and the colorbar's own tick
    column won as the right-side y-axis by default, calibrating the two real
    line series (ground truth y in 150..480) against the colorbar's 0..10
    scale instead — a ~100x-smaller chimera range, easily detectable.

    Both fix layers are exercised: (1) the lowered 45pt span threshold admits
    the real compact axis so it out-competes the colorbar as a `lefts`
    candidate, and (2) `_looks_like_colorbar` rejects the colorbar strip
    outright so it can never become a `rights` candidate even if the real
    axis were absent.
    """
    doc = pymupdf.open(SYN / "line_colorbar.pdf")
    result = chart_extractor.extract_charts(doc, 0, max_points=12)
    doc.close()
    assert result["status"] == "ok"
    chart = result["charts"][0]
    assert chart["y_axis"]["side"] == "left", "must calibrate off the panel's own axis"
    curves = chart["curves"]
    assert len(curves) == 2, "both real line series must be emitted"
    for curve in curves:
        ys = [p[1] for p in curve["points"]]
        # ground-truth range is 150..480; colorbar range is 0..10 — a
        # chimera would land entirely below 10, so this range check alone
        # distinguishes a correct emission from the wrong-emit.
        assert min(ys) > 50, f"y-values too low, looks colorbar-calibrated: {ys}"
        assert max(ys) < 600, f"y-values out of the real axis range: {ys}"


def test_style_is_uniform_dict_shape_across_kinds():
    """#1: every kind (curve/bar/scatter) emits the SAME style shape:
    {"color": [r,g,b]|None, "width": float} — never a raw tuple/string."""

    def check_style(style):
        assert isinstance(style, dict)
        assert set(style) == {"color", "width"}
        assert style["color"] is None or (
            isinstance(style["color"], list) and len(style["color"]) == 3
        )
        assert isinstance(style["width"], float)

    doc = pymupdf.open(SYN / "line_color_linear.pdf")
    r = chart_extractor.extract_charts(doc, 0)
    doc.close()
    for c in r["charts"][0]["curves"]:
        check_style(c["style"])

    doc = pymupdf.open(SYN / "bar_simple.pdf")
    r = chart_extractor.extract_charts(doc, 0)
    doc.close()
    bar_charts = [ch for ch in r["charts"] if ch.get("bars")]
    assert bar_charts
    for s in bar_charts[0]["bars"]:
        check_style(s["style"])

    doc = pymupdf.open(SYN / "scatter_simple.pdf")
    r = chart_extractor.extract_charts(doc, 0)
    doc.close()
    scatter_charts = [ch for ch in r["charts"] if ch.get("points")]
    assert scatter_charts
    for s in scatter_charts[0]["points"]:
        check_style(s["style"])


def test_series_fields_present_with_null_across_kinds():
    """#5: every series carries the same optional fields, present-with-null,
    regardless of kind — bars/scatter get multivalued=False,
    downsampled=False, n_extrema_dropped=0 rather than omitting them."""
    common = {
        "style",
        "label",
        "axis",
        "resolved_by",
        "multivalued",
        "downsampled",
        "n_extrema_dropped",
    }

    doc = pymupdf.open(SYN / "bar_simple.pdf")
    r = chart_extractor.extract_charts(doc, 0)
    doc.close()
    bar_charts = [ch for ch in r["charts"] if ch.get("bars")]
    for s in bar_charts[0]["bars"]:
        assert common <= set(s)
        assert s["multivalued"] is False
        assert s["downsampled"] is False
        assert s["n_extrema_dropped"] == 0
        assert "bars" in s

    doc = pymupdf.open(SYN / "scatter_simple.pdf")
    r = chart_extractor.extract_charts(doc, 0)
    doc.close()
    scatter_charts = [ch for ch in r["charts"] if ch.get("points")]
    for s in scatter_charts[0]["points"]:
        assert common <= set(s)
        assert s["multivalued"] is False
        assert s["downsampled"] is False
        assert s["n_extrema_dropped"] == 0
        assert "points" in s


def test_diagnostics_always_carries_notes_key():
    """#5/#6: diagnostics.notes is always present (possibly empty) — it
    should never be missing regardless of chart kind."""
    for fname in ("bar_simple.pdf", "scatter_simple.pdf", "line_color_linear.pdf"):
        doc = pymupdf.open(SYN / fname)
        r = chart_extractor.extract_charts(doc, 0)
        doc.close()
        for ch in r["charts"]:
            assert "notes" in ch["diagnostics"]
            assert isinstance(ch["diagnostics"]["notes"], list)


def test_axis_titles_and_range_populated(line_doc):
    """#2: x_axis/y_axis carry title (str|None) and range ([min,max])."""
    r = chart_extractor.extract_charts(line_doc, 0)
    chart = r["charts"][0]
    for axis_key in ("x_axis", "y_axis"):
        axis = chart[axis_key]
        assert "title" in axis
        assert axis["title"] is None or isinstance(axis["title"], str)
        assert "range" in axis
        lo, hi = axis["range"]
        assert isinstance(lo, float) and isinstance(hi, float)
        assert lo < hi


def test_curve_label_populated_from_legend_without_dual_axis():
    """#2: label matching runs for every curve, not only when a dual-axis
    question was asked. line_two_legend_dashed.pdf has a legend and a
    single y-axis (no dual-axis ambiguity)."""
    doc = pymupdf.open(SYN / "line_two_legend_dashed.pdf")
    r = chart_extractor.extract_charts(doc, 0)
    doc.close()
    assert r["status"] == "ok"
    labels = [c.get("label") for ch in r["charts"] for c in ch.get("curves", [])]
    assert any(lab is not None for lab in labels), "expected a legend-matched label"


def test_points_rounded_to_four_sig_figs(line_doc):
    """#3: emitted point values are rounded to 4 significant figures — no
    fictional 15-digit float precision."""
    r = chart_extractor.extract_charts(line_doc, 0)
    for c in r["charts"][0]["curves"]:
        for x, y in c["points"]:
            for v in (x, y):
                mantissa = f"{abs(v):.10e}".split("e")[0]
                digits = mantissa.replace(".", "").rstrip("0") or "0"
                assert len(digits) <= 4, f"value {v} exceeds 4 sig figs ({digits})"


def test_sig_helper():
    assert chart_extractor._sig(1361412345678901.0) == 1.361e15
    assert chart_extractor._sig(0.000123456) == 0.0001235
    assert chart_extractor._sig(5.0) == 5.0


def test_looks_like_colorbar_detects_raster_strip(dual_doc):
    """`_looks_like_colorbar` is a standalone geometry check: a raster image
    (or dense stack of thin filled rects) sitting in a narrow band between
    the x-axis span's right edge and a candidate y-axis column reads as a
    colorbar. Exercise it directly against a page with no such raster band
    (the ordinary dual-axis synthetic fixture) to confirm it returns False
    absent colorbar-shaped content, so the helper isn't a rubber stamp."""
    page = dual_doc[0]
    panel = chart_extractor.find_panels(page)[0]
    x1 = panel["xa"]["px"].max()
    ya = panel["ya"]
    assert chart_extractor._looks_like_colorbar(page, x1, ya) is False


def test_looks_like_axis_title():
    good = [
        "size",
        "Superposition Size",
        "Diode Reverse Voltage (V)",
        "Compute (PF-days)",
        "Number of d electrons",
        "iter. (1e4)",
    ]
    bad = [
        "as follows. In the multi-speaker task, we used en.base and",
        "Figure 2 | Training curve envelope for a range of model sizes.",
    ]
    for t in good:
        assert chart_extractor._looks_like_axis_title(t) is True, t
    for t in bad:
        assert chart_extractor._looks_like_axis_title(t) is False, t


def test_axis_title_rejects_body_text():
    """Ship-blocker regression: _x_axis_title must never launder body text
    or a figure caption into x_axis.title. 1807.11632 p4 (0-indexed 3) is a
    real sample where the nearest horizontal line under the panel used to be
    a sentence fragment, not the true axis title ("size")."""
    doc = pymupdf.open(REAL / "1807.11632.pdf")
    result = chart_extractor.extract_charts(doc, 3)
    doc.close()
    for ch in result["charts"]:
        title = ch["x_axis"]["title"]
        assert title is None or title == "size"
        assert title is None or " task," not in title
        assert len(title or "") <= 45


def test_dual_axis_exposes_right_axis():
    doc = pymupdf.open(SYN / "line_dual_axis.pdf")
    r1 = chart_extractor.extract_charts(doc, 0)
    hints = {q["id"]: "left" for q in r1["questions"]}
    for q in r1["questions"]:
        if q["series_style"]["color"] and q["series_style"]["color"][0] > 0.5:
            hints[q["id"]] = "right"
    result = chart_extractor.extract_charts(doc, 0, hints=hints)
    doc.close()
    assert result["status"] == "ok"
    chart = result["charts"][0]
    assert "y_axis_right" in chart
    yar = chart["y_axis_right"]
    assert yar["side"] == "right"
    assert len(yar["range"]) == 2
    assert isinstance(yar["range"][0], float) and isinstance(yar["range"][1], float)

    doc2 = pymupdf.open(SYN / "line_color_linear.pdf")
    result2 = chart_extractor.extract_charts(doc2, 0)
    doc2.close()
    assert "y_axis_right" not in result2["charts"][0]
